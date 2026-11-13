# -*- coding: utf-8 -*-
"""大盤融資維持率歷史：以逐檔資料重算 (2019 起) 為底，證交所臺股儀表板的官方「全市場擔保維持率」覆蓋近期，玩股網只留作對照。

    python market_maint.py refresh   # 抓三個來源，和 data/market/ 已存的資料「合併」後存回 (舊列不會被滾動視窗洗掉)
    python market_maint.py refresh --only official   # 只抓證交所官方 (每日排程用，見 update.py)
    python market_maint.py calc      # 用 data/raw 逐檔資料重算上市/上櫃/合計維持率 -> data/market/calc_from_raw.csv
    python market_maint.py build     # 合成 out/market_maint_daily.csv，並畫 out/charts/market_maint.png (沒裝 matplotlib 或加 --no-chart 就不畫)

為什麼要合併：
  玩股網 API 只回最近 1250 個交易日、證交所儀表板只回最近 60 個交易日，兩個視窗每天都往前滾；
  覆蓋式存檔 (如 kairos_txf_v3/margin_leverage_indicator/fetch_wantgoo.py) 會把最舊的列丟掉。
  這裡一律「新值覆蓋同日舊值、只在舊檔的列保留」，列數只增不減。證交所視窗只有 60 日，至少每 60 個交易日要跑一次 refresh。

來源 (都不用登入，三個請求之間隔 >= 2 秒)：
  玩股網 0000A 上市 / twoA 上櫃  維持率(%) = 融資擔保品市值 / 融資金額 (逐日收盤估算，上市、上櫃各自算)
  證交所 rwd/zh/marginTrading/BFIJ3U_TREND  全市場擔保維持率 keepRate(%) (整戶口徑：含融券擔保品、含上櫃；2026-08-03 起)

櫃買中心沒有維持率：OpenAPI 225 個資料集沒有任何維持率/擔保/整戶欄位，官網只有每檔的資使用率與資限額；
  官方維持率只有證交所儀表板「全市場」(上市+上櫃合併，整戶口徑)，儀表板的上市/上櫃分頁不顯示它。
  所以上櫃單獨的維持率只能重算：Σ 逐檔融資餘額 × 收盤價 ÷ 官方融資金額 (和玩股網上市口徑相同)。
  實測 2026-08~11 的 69 個官方日：重算上櫃的日變動與官方相關 0.973，玩股網上櫃只有 0.169 (2026 年 7 月起壞掉)。

輸出欄位 (out/market_maint_daily.csv)：
  calc_listed / calc_otc / calc_all   由 data/raw 逐檔資料重算 (calc_all 需要上市與上櫃兩邊都有)
  official             證交所全市場擔保維持率 (整戶口徑，2026-08-03 起)
  maint_listed         有官方值的日子用官方，其次 calc_listed，再其次 wg_listed   (source: twse / calc / wantgoo)
  maint_all            有官方值的日子用官方，其次 calc_all，沒有就留空            (source_all: twse / calc / 空白)
  wg_listed / wg_otc   玩股網上市 / 上櫃，只供對照；wg_all = 兩者用各自融資金額加權
玩股網上櫃自 2026-06 中起整段早一天 (對得到隔天的重算值)，所以 maint_all 不使用 wg_all 當後備；
玩股網上市在 15 個交易日有壞值 (與重算差超過 3 點)，重算值取代它們。
官方口徑與重算口徑不同 (整戶含融券擔保品、抵繳證券)，接合處會有一個台階 (build 會印出重疊期的差距)。
"""
import argparse
import datetime as dt
import glob
import gzip
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "market")
RAW = os.path.join(HERE, "data", "raw")
OUT = os.path.join(HERE, "out")
CHARTS = os.path.join(OUT, "charts")
GAP = 2.0

WG_URL = "https://www.wantgoo.com/stock/{code}/margin-trading/historical-lending-balance-long-term"
WG_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.wantgoo.com/stock/margin-trading/market-price/taiex"}
TWSE_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/BFIJ3U_TREND"
TWSE_H = {"User-Agent": "Mozilla/5.0"}

FILES = {
    "listed": os.path.join(DATA, "wantgoo_listed.csv"),
    "otc": os.path.join(DATA, "wantgoo_otc.csv"),
    "official": os.path.join(DATA, "twse_keeprate.csv"),
    "calc": os.path.join(DATA, "calc_from_raw.csv"),
}
# 第一次執行時，用 margin_leverage_indicator/data 裡 2026-11-13 備份的舊檔當種子 (那是舊視窗最早的列)
SEED_DIR = os.path.join(os.path.dirname(HERE), "kairos_txf_v3", "margin_leverage_indicator", "data")
SEEDS = {
    "listed": ["_bak_20261113/wantgoo_maint_listed.csv", "wantgoo_maint_listed.csv"],
    "otc": ["_bak_20261113/wantgoo_maint_otc.csv", "wantgoo_maint_otc.csv"],
    "official": ["_bak_20261113/official_keeprate.csv", "official_keeprate.csv"],
}


def log(msg):
    print(f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- IO
def read_csv(path):
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def write_csv(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    df.sort_values("date").to_csv(tmp, index=False, date_format="%Y-%m-%d")
    os.replace(tmp, path)


def load_archive(key):
    """已存的檔；沒有的話用舊專案的備份當種子。"""
    if os.path.exists(FILES[key]):
        return read_csv(FILES[key])
    for rel in SEEDS[key]:
        p = os.path.join(SEED_DIR, rel)
        if os.path.exists(p):
            log(f"{key}: no archive yet, seeding from {p}")
            return read_csv(p)
    return pd.DataFrame(columns=["date"])


def merge(old, new, value_cols, tol):
    """新值覆蓋同日舊值，舊檔獨有的列保留。回傳 (合併結果, 統計)。"""
    stats = {"old": len(old), "new": len(new)}
    if len(old):
        both = old.merge(new, on="date", suffixes=("_o", "_n"))
        stats["overlap"] = len(both)
        ch = np.zeros(len(both), dtype=bool)
        for c, t in zip(value_cols, tol):
            ch |= (both[c + "_o"] - both[c + "_n"]).abs().to_numpy() > t
        stats["changed"] = int(ch.sum())
        stats["changed_dates"] = [d.date().isoformat() for d in both.loc[ch, "date"]][:8]
        stats["only_old"] = int((~old["date"].isin(new["date"])).sum())
    else:
        stats.update(overlap=0, changed=0, changed_dates=[], only_old=0)
    out = pd.concat([old, new], ignore_index=True)
    out = out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    stats["merged"] = len(out)
    assert out["date"].is_unique and len(out) >= len(old), "merge must never lose rows"
    return out, stats


# ---------------------------------------------------------------- fetch
def fetch_wantgoo(code):
    r = requests.get(WG_URL.format(code=code), headers=WG_H, timeout=60)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, list) or len(j) < 1000:
        raise RuntimeError(f"wantgoo {code}: unexpected payload ({type(j).__name__}, {len(j) if hasattr(j, '__len__') else '?'} rows)")
    df = pd.DataFrame(j)
    df["date"] = (pd.to_datetime(df["date"], unit="ms") + pd.Timedelta(hours=8)).dt.normalize()   # API date = trade date 00:00 Taipei in UTC ms
    df = df.rename(columns={"lendingBalance": "margin_thousand", "marginRatio": "maint"})
    df["maint"] = df["maint"] * 100.0
    df = df[["date", "margin_thousand", "maint"]].dropna(subset=["maint"])
    if not df["maint"].between(80, 400).all():
        raise RuntimeError(f"wantgoo {code}: maint outside 80-400%: {df['maint'].min()}..{df['maint'].max()}")
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def fetch_twse_keeprate():
    r = requests.get(TWSE_URL, params={"response": "json", "days": 400}, headers=TWSE_H, timeout=40)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        raise RuntimeError(f"TWSE BFIJ3U_TREND: stat={j.get('stat')!r}")
    df = pd.DataFrame(j["data"])
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    df = df.rename(columns={"keepRate": "keep_rate"})
    df["keep_rate"] = pd.to_numeric(df["keep_rate"], errors="coerce")
    df = df.dropna(subset=["keep_rate"])
    if not df["keep_rate"].between(80, 400).all():
        raise RuntimeError(f"TWSE keepRate outside 80-400%: {df['keep_rate'].min()}..{df['keep_rate'].max()}")
    return df.sort_values("date").reset_index(drop=True)


def cmd_refresh(args):
    os.makedirs(DATA, exist_ok=True)
    jobs = [
        ("listed", lambda: fetch_wantgoo("0000A"), ["maint", "margin_thousand"], [0.05, 1.0]),
        ("otc", lambda: fetch_wantgoo("twoA"), ["maint", "margin_thousand"], [0.05, 1.0]),
        ("official", fetch_twse_keeprate, ["keep_rate"], [0.005]),
    ]
    if getattr(args, "only", None):
        jobs = [j for j in jobs if j[0] == args.only]
    new_frames, results = {}, {}
    for i, (key, fn, cols, tol) in enumerate(jobs):
        if i:
            time.sleep(GAP)
        new = fn()
        new_frames[key] = (new, cols, tol)
        log(f"{key}: fetched {len(new)} rows {new['date'].min().date()}..{new['date'].max().date()}")
    # 三個都抓到、驗證過才寫檔 (任何一個失敗就整批不動)
    for key, (new, cols, tol) in new_frames.items():
        old = load_archive(key)
        merged, st = merge(old, new, cols, tol)
        write_csv(merged, FILES[key])
        results[key] = (merged, st)
        log(
            f"{key}: archive {st['old']} -> {st['merged']} rows ({merged['date'].min().date()}..{merged['date'].max().date()}); "
            f"overlap {st['overlap']}, changed vs saved {st['changed']} {st['changed_dates']}, kept only-in-old {st['only_old']}"
        )
    return 0


# ---------------------------------------------------------------- calc (recompute from the per-stock raw files)
def calc_one(src):
    """逐日 Σ 融資餘額(張) × 1000 × 收盤價 (當天沒成交的檔沿用前一個收盤價) 與官方融資金額 (仟元 -> 元)。
    回傳 DataFrame(index=date, columns=[mv, loan, miss])；miss = 完全沒有價格可用的融資餘額占比。"""
    last, rows, bad = {}, [], 0
    for f in sorted(glob.glob(os.path.join(RAW, src, "*.json.gz"))):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, EOFError, ValueError):
            bad += 1  # 背景抓取中的檔案可能還沒寫完，下次再算
            continue
        px = r["p"]
        for c, v in px.items():
            if v[3]:
                last[c] = v[3]
        mv = miss = tot = 0.0
        for c, v in r["m"].items():
            bal = v[4]
            if not bal:
                continue
            tot += bal
            cl = (px.get(c) or [None] * 4)[3] or last.get(c)
            if cl:
                mv += bal * 1000.0 * cl
            else:
                miss += bal
        rows.append((pd.Timestamp(r["d"]), mv, r["a"][4] * 1000.0, miss / tot if tot else 0.0))
    if bad:
        log(f"{src}: {bad} unreadable file(s) skipped")
    return pd.DataFrame(rows, columns=["date", "mv", "loan", "miss"]).set_index("date").sort_index()


def cmd_calc(_args):
    L, O = calc_one("twse"), calc_one("tpex")
    if L.empty or O.empty:
        raise RuntimeError("no raw per-stock files yet; run fetch_flows.py twse|tpex first")
    df = L.add_suffix("_L").join(O.add_suffix("_O"), how="outer")
    okL, okO = df["loan_L"] > 0, df["loan_O"] > 0
    df["calc_listed"] = np.where(okL, df["mv_L"] / df["loan_L"] * 100.0, np.nan)
    df["calc_otc"] = np.where(okO, df["mv_O"] / df["loan_O"] * 100.0, np.nan)
    df["calc_all"] = np.where(okL & okO, (df["mv_L"] + df["mv_O"]) / (df["loan_L"] + df["loan_O"]) * 100.0, np.nan)
    for c in ("calc_listed", "calc_otc", "calc_all"):
        v = df[c].dropna()
        if not v.between(80, 400).all():
            raise RuntimeError(f"{c} outside 80-400%: {v.min():.1f}..{v.max():.1f}; check the raw files")
    out = df[["calc_listed", "calc_otc", "calc_all", "miss_L", "miss_O"]].reset_index()
    write_csv(out, FILES["calc"])
    log(
        f"calc: listed {int(okL.sum())} days {df.index[okL].min().date()}..{df.index[okL].max().date()}; "
        f"otc {int(okO.sum())} days {df.index[okO].min().date()}..{df.index[okO].max().date()}; "
        f"all {int(df['calc_all'].notna().sum())} days; max share of balance without a price: "
        f"listed {df['miss_L'].max():.4f}, otc {df['miss_O'].max():.4f}"
    )
    return 0


# ---------------------------------------------------------------- build
def build_frame():
    L = read_csv(FILES["listed"]).rename(columns={"maint": "wg_listed", "margin_thousand": "loan_listed_k"})
    O = read_csv(FILES["otc"]).rename(columns={"maint": "wg_otc", "margin_thousand": "loan_otc_k"})
    K = read_csv(FILES["official"])[["date", "keep_rate"]].rename(columns={"keep_rate": "official"})
    df = L.merge(O, on="date", how="outer").merge(K, on="date", how="outer")
    if os.path.exists(FILES["calc"]):
        df = df.merge(read_csv(FILES["calc"])[["date", "calc_listed", "calc_otc", "calc_all"]], on="date", how="outer")
    else:
        for c in ("calc_listed", "calc_otc", "calc_all"):
            df[c] = np.nan
    df = df.sort_values("date").set_index("date")
    w = df["loan_listed_k"] + df["loan_otc_k"]
    df["wg_all"] = (df["wg_listed"] * df["loan_listed_k"] + df["wg_otc"] * df["loan_otc_k"]) / w
    has_off, has_cl, has_ca = df["official"].notna(), df["calc_listed"].notna(), df["calc_all"].notna()
    # 順序：官方 -> 重算 -> (只有上市) 玩股網。上櫃/合計不退回玩股網，因為它的上櫃自 2026-06 中起早了一天。
    df["maint_listed"] = df["official"].where(has_off, df["calc_listed"].where(has_cl, df["wg_listed"]))
    df["source"] = np.where(has_off, "twse", np.where(has_cl, "calc", np.where(df["wg_listed"].notna(), "wantgoo", "")))
    df["maint_all"] = df["official"].where(has_off, df["calc_all"])
    df["source_all"] = np.where(has_off, "twse", np.where(has_ca, "calc", ""))
    return df


def overlap_report(df):
    o = df["official"].dropna()
    if o.empty:
        return
    lines = [f"overlap with official: {len(o)} days {o.index.min().date()}..{o.index.max().date()}"]
    for c in ("wg_listed", "calc_listed", "wg_otc", "calc_otc", "wg_all", "calc_all"):
        x = pd.concat([o.rename("official"), df.loc[o.index, c]], axis=1).dropna()
        if len(x) < 5:
            continue
        g = x["official"] - x[c]
        dc = np.corrcoef(x["official"].diff().dropna(), x[c].diff().dropna())[0, 1]
        lines.append(
            f"  official - {c:11s}: mean {g.mean():+6.2f}  sd {g.std():5.2f}  min {g.min():+6.2f}  max {g.max():+6.2f}"
            f"  level corr {np.corrcoef(x['official'], x[c])[0, 1]:.4f}  daily-change corr {dc:.3f}"
        )
    first = o.index.min()
    lines.append(f"  step at the join ({first.date()}): official {o.loc[first]:.2f} vs wantgoo listed {df.loc[first, 'wg_listed']:.2f}")
    print("\n".join(lines))


def make_chart(df, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(12, 10), gridspec_kw={"height_ratios": [3, 1.5, 1.7]})
    a1.plot(df.index, df["calc_listed"], color="#1f4e79", lw=1.0, label="重算 上市")
    a1.plot(df.index, df["calc_all"], color="#7fa6cf", lw=0.8, alpha=0.8, label="重算 上市+上櫃")
    off = df["official"].dropna()
    a1.plot(off.index, off.values, color="#d62728", lw=1.8, label="證交所 官方全市場 (整戶)")
    a1.axhline(130, color="#aaaaaa", lw=0.8, ls="--")
    first = df["calc_listed"].first_valid_index()
    if first is not None:
        a1.text(first, 131, "130% 追繳線", fontsize=8, color="#777777")
    a1.set_ylabel("維持率 %")
    a1.set_title("大盤融資維持率：逐檔資料重算 + 證交所官方 (玩股網僅供對照)", loc="left")
    a1.legend(loc="upper left", fontsize=8, ncol=3)
    a1.grid(alpha=0.25)
    ov = df.dropna(subset=["official", "calc_listed", "calc_all"])
    if len(ov):
        a2.plot(ov.index, ov["official"] - ov["calc_listed"], color="#1f4e79", lw=1.2, label="官方 - 重算上市")
        a2.plot(ov.index, ov["official"] - ov["calc_all"], color="#7fa6cf", lw=1.2, label="官方 - 重算上市+上櫃")
        a2.axhline(0, color="#aaaaaa", lw=0.8)
        a2.legend(loc="upper left", fontsize=8)
    a2.set_ylabel("差距 (點)")
    a2.set_title("重疊期：官方與各口徑的差距", loc="left", fontsize=9)
    a2.grid(alpha=0.25)
    ot = df.loc[df.index >= "2026-01-01", ["wg_otc", "calc_otc"]]
    if ot["calc_otc"].notna().any():
        a3.plot(ot.index, ot["wg_otc"], color="#8a8f98", lw=1.1, label="玩股網 上櫃")
        a3.plot(ot.index, ot["calc_otc"], color="#4c78a8", lw=1.1, label="逐檔重算 上櫃")
        a3.legend(loc="upper left", fontsize=8)
    a3.set_ylabel("維持率 %")
    a3.set_title("上櫃 (櫃買中心沒有維持率，只能重算)：玩股網 vs 重算，2026 年", loc="left", fontsize=9)
    a3.grid(alpha=0.25)
    fig.tight_layout()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)


def cmd_build(args):
    df = build_frame()
    overlap_report(df)
    os.makedirs(OUT, exist_ok=True)
    cols = ["wg_listed", "wg_otc", "wg_all", "calc_listed", "calc_otc", "calc_all", "official", "maint_listed", "maint_all", "source", "source_all"]
    out = df[cols].round(3)
    out.to_csv(os.path.join(OUT, "market_maint_daily.csv"), index_label="date", date_format="%Y-%m-%d")
    log(f"wrote out/market_maint_daily.csv: {len(out)} rows {out.index.min().date()}..{out.index.max().date()}; official days {int(out['official'].notna().sum())}")
    log(f"maint_listed source: {df['source'].replace('', '(none)').value_counts().to_dict()}; maint_all source: {df['source_all'].replace('', '(none)').value_counts().to_dict()}")
    def inner_gaps(s):  # days after the series starts that still have no value
        f = s.first_valid_index()
        return s.index[s.isna() & (s.index >= f)] if f is not None else s.index[:0]

    gl, ga = inner_gaps(df["maint_listed"]), inner_gaps(df["maint_all"])
    if len(gl) or len(ga):
        log(f"WARNING: days inside the covered range without a value: maint_listed {len(gl)} {[d.date().isoformat() for d in gl[:5]]}, maint_all {len(ga)} {[d.date().isoformat() for d in ga[:5]]}")
    if getattr(args, "no_chart", False):
        return 0
    try:
        make_chart(df, os.path.join(CHARTS, "market_maint.png"))
        log("wrote out/charts/market_maint.png")
    except ImportError:  # 沒裝 matplotlib (例如排程環境)：圖只是附帶的，不影響資料
        log("matplotlib not installed; chart skipped")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--only", choices=["official"], help="只抓這個來源 (排程用：玩股網只是對照、可能擋雲端 IP，所以排程只抓證交所官方)")
    sub.add_parser("calc")
    b = sub.add_parser("build")
    b.add_argument("--no-chart", action="store_true", help="不畫 out/charts/market_maint.png")
    a = ap.parse_args(argv)
    return {"refresh": cmd_refresh, "calc": cmd_calc, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
