# -*- coding: utf-8 -*-
"""個股融資維持率「推估器」。

資料：fetch_flows.py 存的 data/raw/{twse,tpex}/YYYYMMDD.json.gz
方法：每檔股票維護融資「庫存批次」 lots = [[張數, 每股成本], ...]
      每天：1) 把庫存對齊官方「前日餘額」 2) 扣掉賣出+現金償還 (依規則) 3) 加入當天新增買進 (依價格代理)
            4) 與官方「今日餘額」的差視為配股/減資，股數調整、總成本不變
            5) (anchor) 把全部成本等比縮放到與官方公布的「全市場融資金額餘額」一致
      個股維持率 = 收盤價 / (平均成本 × 融資成數)
對帳：官方每天公布「全市場融資金額」的買進/賣出/現償/餘額 (仟元)，逐日與估計值比較，
      用它挑價格代理與扣帳規則，並校準整體水準。個別檔沒有官方答案可對，所以個股數字是「估計」。

用法：
    python estimator.py calibrate [--src twse|tpex] [--start 2019-01-01] [--burn 120]
    python estimator.py final --removal avg --price vwap --dt-frac 1 [--start 2019-01-01] [--burn 150]
"""
import argparse
import csv
import glob
import gzip
import json
import os
import statistics
import sys
import time
from collections import deque

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw")
OUT = os.path.join(HERE, "out")
THRESHOLDS = (120, 130, 140, 145, 150, 160)
EPS = 1e-9


NAMES_FILE = os.path.join(HERE, "data", "stock_names.csv")


def is_core(code):
    """4 位數字代號 = 一般股票 (排除 ETF 00xxxx、特別股 xxxxA/B、債券 ETF 等)。"""
    return len(code) == 4 and code.isdigit()


def load_names(path=None):
    """代號 -> 名稱 (data/stock_names.csv)。沒有檔就回空的。"""
    names = {}
    path = path or NAMES_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0] != "code":
                    names[row[0]] = row[1]
    return names


def save_names(names, path=None):
    path = path or NAMES_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["code", "name"])
        for k in sorted(names):
            w.writerow([k, names[k]])
    os.replace(tmp, path)


# ------------------------------------------------------------------ 讀檔
def load_days(src, start=None, end=None):
    """依日期排序回傳 [record, ...]。start/end 是 'YYYY-MM-DD'。"""
    lo = start.replace("-", "") if start else "00000000"
    hi = end.replace("-", "") if end else "99999999"
    out = []
    for f in sorted(glob.glob(os.path.join(RAW, src, "*.json.gz"))):
        ds = os.path.basename(f)[:8]
        if lo <= ds <= hi:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# ------------------------------------------------------------------ 參數
class Params:
    def __init__(self, removal="fifo", price="close", daytrade=True, ratio=0.6, anchor=False, dt_frac=None, net=False):
        self.removal = removal      # fifo | lifo | avg   扣帳順序
        self.price = price          # close | vwap | typ | mid | open   當天新增買進的入帳價
        # dt_frac: min(買,賣) 裡視為「當沖、不進庫存」的比例；0 = 賣出全部去扣舊庫存，1 = 能抵銷的都當沖
        self.dt_frac = (1.0 if daytrade else 0.0) if dt_frac is None else dt_frac
        self.ratio = ratio          # 融資成數 (借款 / 買進價金)
        self.anchor = anchor        # True: 每天把全部成本等比縮放到與官方融資金額餘額一致
        # net: 只看每天「餘額的淨變動」(買進 - 賣出 - 現償)：增加的部分以當日價入庫、減少的部分扣舊庫存，
        #      同日買賣與現償全部互相抵銷，所以完全不用另外處理當沖 (對帳用的買進/賣出金額誤差在這個模式下沒有意義)
        self.net = net

    def label(self):
        mode = "net" if self.net else f"dt{self.dt_frac:g}"
        return (f"{self.removal}/{self.price}/{mode}/r{self.ratio:.3f}/"
                f"{'anchor' if self.anchor else 'free'}")


def price_proxy(pv, kind):
    """pv = [開, 高, 低, 收, 成交股數, 成交金額(元)]"""
    if not pv:
        return None
    o, h, l, c, q, v = pv
    if kind == "close" or c is None:
        return c
    if kind == "vwap":
        return (v / q) if (q and v) else c
    if kind == "typ":
        return (h + l + c) / 3.0 if (h and l) else c
    if kind == "mid":
        return (h + l) / 2.0 if (h and l) else c
    if kind == "open":
        return o or c
    raise ValueError(kind)


# ------------------------------------------------------------------ 價格代理的初篩
def implied_ratio_by_proxy(days, kinds=("close", "vwap", "typ", "mid", "open")):
    """每天: 官方融資買進金額 / Σ(買進張數 × 價格代理)。融資成數應該接近常數，
    所以「每天隱含成數」變動最小的價格代理，最接近融資買進的實際成交價。"""
    res = {}
    for kind in kinds:
        ir = []
        for rec in days:
            m, p, a = rec["m"], rec["p"], rec["a"]
            tot = 0.0
            for code, (B, S, R, Pp, Pb) in m.items():
                if B and code in p:
                    px = price_proxy(p[code], kind)
                    if px:
                        tot += B * px
            if tot > 0 and a[0]:
                ir.append(a[0] / tot)
        mean = statistics.fmean(ir)
        sd = statistics.pstdev(ir)
        res[kind] = dict(mean=mean, sd=sd, cv=sd / mean, n=len(ir))
    return res


# ------------------------------------------------------------------ 模擬
def _units(bk):
    return sum(l[0] for l in bk)


def _stats(mats, loans, vals):
    n = len(mats)
    tl = float(loans.sum()) if n else 0.0
    tv = float(vals.sum()) if n else 0.0
    d = dict(n=n, tl=tl, tv=tv, mkt=(tv / tl * 100.0) if tl else None)
    for thr in THRESHOLDS:
        mask = mats < thr
        d[f"cnt{thr}"] = int(mask.sum())
        d[f"w{thr}"] = float(loans[mask].sum() / tl) if tl else 0.0
    return d


def simulate(days, P, keep_last=True, snap_dates=()):
    """回傳 dict: daily=[每日對帳與廣度], last=最後一天逐檔表, snaps={日期: 逐檔表}, skipped, books。
    逐檔表每列 = (代號, 維持率%, 借款仟元, 市值仟元, 張數, 收盤, 平均成本)，依維持率由低到高。"""
    books = {}               # code -> deque([units, cost])
    last_close = {}
    daily, snaps = [], {}
    r = P.ratio
    skipped = 0
    last_rows = None
    snap_dates = set(snap_dates)

    for di, rec in enumerate(days):
        m, p, a = rec["m"], rec["p"], rec["a"]
        est_buy = est_rm = 0.0
        for code in [c for c in books if c not in m]:
            del books[code]
        for code, (B, S, R, Pp, Pb) in m.items():
            if P.net:
                dlt = B - S - R
                B, S, R = (dlt if dlt > 0 else 0.0), (-dlt if dlt < 0 else 0.0), 0.0
            pv = p.get(code)
            prev_c = last_close.get(code)
            if pv:
                last_close[code] = pv[3]
            px = price_proxy(pv, P.price) if pv else last_close.get(code)
            if not px:
                skipped += 1
                continue
            bk = books.get(code)
            if bk is None:
                bk = books[code] = deque()

            # 1) 對齊官方前日餘額。
            #    官方在分割/配股/減資當天會把前日餘額調成新的張數；若「餘額倍數 x 價格倍數 ≈ 1」(市值不變)，
            #    就是公司行動：張數放大、每股成本等比縮小 (總成本不變)。否則視為資料缺漏/更正：只調張數。
            inv = _units(bk)
            if abs(inv - Pp) > 0.5:
                if Pp <= 0:
                    bk.clear()
                elif inv <= EPS:
                    bk.append([float(Pp), px])
                else:
                    f = Pp / inv
                    corp = False
                    if pv and prev_c and abs(f - 1) > 0.03:
                        rho = pv[3] / prev_c
                        # 市值不變 (配股/分割/合併)，或張數與價格大幅反向 (復牌、停牌、面額變更等，倍數不一定剛好對上)
                        corp = abs(rho * f - 1) < 0.05 or (abs(rho - 1) > 0.25 and (f - 1) * (rho - 1) < 0)
                    for l in bk:
                        l[0] *= f
                        if corp:
                            l[1] /= f

            # 2) 扣帳
            dtq = P.dt_frac * min(B, S)
            rem = (S - dtq) + R
            if P.removal == "avg" and len(bk) > 1:
                tu = _units(bk)
                tc = sum(l[0] * l[1] for l in bk)
                bk.clear()
                bk.append([tu, tc / tu])
            removed_cost = 0.0
            from_right = P.removal == "lifo"
            while rem > EPS and bk:
                lot = bk[-1] if from_right else bk[0]
                take = min(lot[0], rem)
                removed_cost += take * lot[1]
                lot[0] -= take
                rem -= take
                if lot[0] <= EPS:
                    bk.pop() if from_right else bk.popleft()
            leftover = min(rem, B - dtq)          # 舊庫存扣不完 -> 視為當天買進又賣出
            # 3) 加入當天新增買進
            b_add = (B - dtq) - leftover
            if b_add > EPS:
                if P.removal == "avg" and bk:
                    lot = bk[0]
                    tot = lot[0] + b_add
                    lot[1] = (lot[0] * lot[1] + b_add * px) / tot
                    lot[0] = tot
                else:
                    bk.append([float(b_add), px])
            est_buy += B * px * r
            est_rm += removed_cost * r + (dtq + leftover) * px * r

            # 4) 與官方今日餘額的差 = 配股/減資/其他調整：股數調整，總成本不變
            inv = _units(bk)
            if abs(inv - Pb) > 0.5:
                if inv > EPS and Pb > 0:
                    g = Pb / inv
                    for l in bk:
                        l[0] *= g
                        l[1] /= g
                elif Pb > 0:
                    bk.append([float(Pb), px])
                else:
                    bk.clear()

        # 5) 整體對帳
        est_end = sum(l[0] * l[1] for bk in books.values() for l in bk) * r
        k = (a[4] / est_end) if est_end > 0 else 1.0
        if di == 0 or P.anchor:                    # 第一天的成本是假設值，一定要先對齊官方總額；anchor 模式則每天對齊
            for bk in books.values():
                for l in bk:
                    l[1] *= k
            est_end = a[4]
        # 6) 廣度
        codes, mats, loans, vals, unit, close, cost = [], [], [], [], [], [], []
        for code, bk in books.items():
            u = _units(bk)
            if u <= EPS:
                continue
            tc = sum(l[0] * l[1] for l in bk)
            c_t = last_close.get(code)
            if not c_t or tc <= 0:
                continue
            loan = tc * r
            codes.append(code)
            mats.append(c_t * u / loan * 100.0)
            loans.append(loan)
            vals.append(c_t * u)
            unit.append(u)
            close.append(c_t)
            cost.append(tc / u)
        mats, loans, vals = np.array(mats), np.array(loans), np.array(vals)
        core = np.array([is_core(c) for c in codes], dtype=bool)
        row = dict(d=rec["d"], est_buy=est_buy, off_buy=a[0], est_rm=est_rm, off_rm=a[1] + a[2],
                   est_end=est_end, off_end=a[4], k=k, skipped_today=0)
        row.update(_stats(mats, loans, vals))
        row["core"] = _stats(mats[core], loans[core], vals[core])
        daily.append(row)
        if rec["d"] in snap_dates or (keep_last and di == len(days) - 1):
            table = sorted(zip(codes, mats.tolist(), loans.tolist(), vals.tolist(), unit, close, cost), key=lambda t: t[1])
            if rec["d"] in snap_dates:
                snaps[rec["d"]] = table
            if di == len(days) - 1:
                last_rows = table
    return dict(daily=daily, last=last_rows, snaps=snaps, skipped=skipped, books=books)


# ------------------------------------------------------------------ 診斷
def diagnose(daily, burn):
    ev = daily[burn:]

    def err(est, off):
        e = [x[est] / x[off] - 1 for x in ev if x[off]]
        return dict(mean=statistics.fmean(e) * 100, mae=statistics.fmean(abs(v) for v in e) * 100, last=e[-1] * 100)
    return dict(buy=err("est_buy", "off_buy"), rm=err("est_rm", "off_rm"), end=err("est_end", "off_end"),
                k_dev=statistics.fmean(abs(x["k"] - 1) for x in ev) * 100, k_last=ev[-1]["k"], n=len(ev))


def cmd_calibrate(a):
    t0 = time.time()
    days = load_days(a.src, a.start, a.end)
    if len(days) < a.burn + 10:
        print(f"資料太少: {len(days)} 天 (burn {a.burn})")
        return
    print(f"{a.src}: {len(days)} 天 {days[0]['d']} ~ {days[-1]['d']}  (前 {a.burn} 天當預熱，不計入誤差)\n")

    print("== 1) 價格代理：每天「官方融資買進金額 ÷ Σ(買進張數×價格代理)」的穩定度 (越穩越接近真實買進價) ==")
    ir = implied_ratio_by_proxy(days)
    for kind, v in ir.items():
        print(f"   {kind:6s} 隱含成數 平均 {v['mean']:.4f}  標準差 {v['sd']:.4f}  變異係數 {100 * v['cv']:.2f}%  ({v['n']} 天)")
    best_px = min(ir, key=lambda k: ir[k]["cv"])
    ratio = ir[best_px]["mean"]
    print(f"   -> 價格代理選 {best_px}，隱含融資成數 {ratio:.4f}\n")

    print("== 2) 扣帳規則 (全部用 anchor：每天把成本對齊官方融資金額餘額)；逐日對帳 估計/官方-1 (%) ==")
    print("   買進/賣出+現償 = mean 系統性偏差 / MAE 平均絕對誤差；校正幅度 = 每天為了對齊官方餘額，成本平均要調整多少 %")
    print(f"   {'規則':42s} {'買進 mean/MAE':>14s} {'賣出+現償 mean/MAE':>20s} {'每日校正幅度':>12s}")
    out = []
    for removal in ("fifo", "lifo", "avg"):
        for dtf in (0.0, 0.5, 1.0):
            P = Params(removal, best_px, ratio=ratio, anchor=True, dt_frac=dtf)
            res = simulate(days, P, keep_last=False)
            d = diagnose(res["daily"], a.burn)
            out.append((P, d))
            print(f"   {P.label():42s} {d['buy']['mean']:+6.2f}/{d['buy']['mae']:5.2f}   {d['rm']['mean']:+7.2f}/{d['rm']['mae']:5.2f}"
                  f"   {d['k_dev']:10.3f}%")
    best = min(out, key=lambda t: t[1]["rm"]["mae"])
    print(f"\n   賣出+現償誤差最小: {best[0].label()}  (MAE {best[1]['rm']['mae']:.2f}%, 每日校正 {best[1]['k_dev']:.3f}%)")
    print(f"\n耗時 {time.time() - t0:.0f} 秒")


# ------------------------------------------------------------------ 最終輸出
KEY_DATES = {  # 新聞上有「家數」的日子，拿來對照 (媒體口徑不明，只能當量級參考)
    "2024-08-05": "8 月崩盤日",
    "2025-04-07": "關稅崩盤 (新聞: 上市櫃 <130% 589 檔、130~140% 441 檔)",
    "2025-04-09": "關稅崩盤低點",
    "2026-07-28": "7 月急殺 (新聞: 929 檔 <145%，引用 CMoney)",
    "2026-11-05": "11 月急殺",
    "2026-11-12": "最新",
}


def common_days(rows_tw, rows_tp):
    """只留上市、上櫃兩邊都有的日子 (各自的每日列，依日期 'd' 對)。"""
    both = {r["d"] for r in rows_tw} & {r["d"] for r in rows_tp}
    return [r for r in rows_tw if r["d"] in both], [r for r in rows_tp if r["d"] in both]


def _merge(rows_tw, rows_tp, which):
    """合併上市、上櫃的每日廣度。which = None (全部) 或 'core'。"""
    by = {}
    for tag, rows in (("tw", rows_tw), ("tp", rows_tp)):
        for r in rows:
            by.setdefault(r["d"], {})[tag] = r if which is None else r[which]
    out = []
    for d in sorted(by):
        parts = list(by[d].values())
        tl = sum(x["tl"] for x in parts)
        tv = sum(x["tv"] for x in parts)
        row = dict(d=d, n=sum(x["n"] for x in parts), tl=tl, tv=tv, mkt=(tv / tl * 100.0) if tl else None)
        for thr in THRESHOLDS:
            row[f"cnt{thr}"] = sum(x[f"cnt{thr}"] for x in parts)
            row[f"w{thr}"] = (sum(x[f"w{thr}"] * x["tl"] for x in parts) / tl) if tl else 0.0
        out.append(row)
    return out


def cmd_final(a):
    global OUT  # --out 讓不同設定的敏感度比較各寫各的資料夾，不互相覆蓋
    if a.out:
        OUT = os.path.abspath(a.out)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.join(OUT, "charts"), exist_ok=True)
    t0 = time.time()
    sims = {}
    for src in ("twse", "tpex"):
        days = load_days(src, a.start, a.end)
        ir = implied_ratio_by_proxy(days, kinds=(a.price,))[a.price]
        P = Params(a.removal, a.price, ratio=ir["mean"], anchor=True, dt_frac=a.dt_frac, net=a.net)
        print(f"{src}: {len(days)} 天 {days[0]['d']}~{days[-1]['d']}  {P.label()}  (隱含成數變異 {100 * ir['cv']:.2f}%)", flush=True)
        sims[src] = simulate(days, P, snap_dates=KEY_DATES)
        d = diagnose(sims[src]["daily"], min(a.burn, len(days) - 10))
        print(f"   對帳(預熱 {a.burn} 天後): 買進 {d['buy']['mean']:+.2f}%/MAE {d['buy']['mae']:.2f}   賣出+現償 {d['rm']['mean']:+.2f}%/MAE {d['rm']['mae']:.2f}"
              f"   每日校正幅度 {d['k_dev']:.3f}%   略過(沒價格) {sims[src]['skipped']} 檔次", flush=True)

    # 預熱期 (前 burn 天) 的成本主要由起始假設決定，不列入輸出
    cut = max(sims[s]["daily"][min(a.burn, len(sims[s]["daily"]) - 1)]["d"] for s in ("twse", "tpex"))
    tw_rows = [r for r in sims["twse"]["daily"] if r["d"] >= cut]
    tp_rows = [r for r in sims["tpex"]["daily"] if r["d"] >= cut]
    print(f"\n輸出從 {cut} 起 (前 {a.burn} 個交易日是預熱，不列入)；共 {len(tw_rows)} 個交易日", flush=True)
    # 合計 (上市+上櫃) 只算兩邊都有資料的日子：一邊還沒公布的最新一天只有單邊，加起來會是殘缺的合計
    tw_both, tp_both = common_days(tw_rows, tp_rows)
    allrows = _merge(tw_both, tp_both, None)
    corerows = _merge(tw_both, tp_both, "core")

    def write_breadth(rows, name):
        cols = ["d", "n", "mkt"] + [f"cnt{t}" for t in THRESHOLDS] + [f"w{t}" for t in THRESHOLDS]
        with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["日期", "有融資餘額檔數", "整體維持率估計%"] + [f"低於{t}%檔數" for t in THRESHOLDS] + [f"低於{t}%借款占比" for t in THRESHOLDS])
            for r in rows:
                w.writerow([r["d"], r["n"], f"{r['mkt']:.2f}" if r["mkt"] else ""] + [r[c] for c in cols[3:3 + len(THRESHOLDS)]] +
                           [f"{r[c]:.4f}" for c in cols[3 + len(THRESHOLDS):]])
    write_breadth(allrows, "breadth_daily_all.csv")
    write_breadth(corerows, "breadth_daily_core.csv")
    # 分市場 (一般股票 4 碼)：儀表板只看上市時用；各市場各自對齊自己的官方融資金額，所以不受另一市場影響
    write_breadth(_merge(tw_rows, [], "core"), "breadth_daily_twse_core.csv")
    write_breadth(_merge([], tp_rows, "core"), "breadth_daily_tpex_core.csv")

    # 最新逐檔表。名稱 raw 裡沒有，另外取最後一天的名單；存在 data/stock_names.csv (合併、只增不減)，
    # 這樣某次取名稱失敗 (網路、被擋) 時沿用舊名稱，不會讓儀表板的名稱欄變空白
    names = load_names()
    try:
        import requests
        H = {"User-Agent": "Mozilla/5.0"}
        got = {}
        last = sims["twse"]["daily"][-1]["d"].replace("-", "")
        j = requests.get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={last}&selectType=ALL&response=json", headers=H, timeout=40).json()
        for t in j["tables"]:
            if "融資融券彙總" in t["title"]:
                got.update({r[0].strip(): r[1].strip() for r in t["data"]})
        time.sleep(2)
        j = requests.get(f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={sims['tpex']['daily'][-1]['d'].replace('-', '/')}&id=&response=json", headers=H, timeout=40).json()
        got.update({r[0].strip(): r[1].strip() for r in j["tables"][0]["data"]})
        names.update(got)
        save_names(names)
    except Exception as e:  # noqa: BLE001
        print("取名稱失敗 (沿用 data/stock_names.csv 的舊名稱，不影響結果):", e)
    with open(os.path.join(OUT, "latest_stock_maint.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["市場", "代號", "名稱", "融資餘額(張)", "收盤價", "估計平均融資成本", "估計維持率%", "估計借款(仟元)", "擔保品市值(仟元)"])
        rows = [("上市", *t) for t in sims["twse"]["last"]] + [("上櫃", *t) for t in sims["tpex"]["last"]]
        for mk, code, mat, loan, val, u, c, cost in sorted(rows, key=lambda t: t[2]):
            w.writerow([mk, code, names.get(code, ""), round(u), c, f"{cost:.2f}", f"{mat:.1f}", round(loan), round(val)])

    # 關鍵日對照
    print("\n== 關鍵日: 估計的低於門檻檔數 (上市+上櫃；全部 / 一般股票4碼) ==")
    print(f"   {'日期':11s}{'事件':44s}{'檔數':>6s}   <130        <140        <145        <150     整體維持率")
    ev_rows = []
    for d, label in KEY_DATES.items():
        if d < cut:
            print(f"   {d:11s}{label[:42]:44s}  (在預熱期內，不列)")
            continue
        parts_all, parts_core = [], []
        for src in ("twse", "tpex"):
            t = sims[src]["snaps"].get(d)
            if t is None:
                continue
            for code, mat, loan, val, u, c, cost in t:
                parts_all.append((mat, loan, val))
                if is_core(code):
                    parts_core.append((mat, loan, val))
        if not parts_all:
            continue
        def cnt(parts, thr):
            return sum(1 for x in parts if x[0] < thr)
        tl = sum(x[1] for x in parts_all)
        tv = sum(x[2] for x in parts_all)
        line = (f"   {d:11s}{label[:42]:44s}{len(parts_all):6d}   " +
                "   ".join(f"{cnt(parts_all, t):4d}/{cnt(parts_core, t):4d}" for t in (130, 140, 145, 150)) + f"   {tv / tl * 100:6.1f}%")
        print(line)
        ev_rows.append((d, label, len(parts_all), [cnt(parts_all, t) for t in (130, 140, 145, 150)], [cnt(parts_core, t) for t in (130, 140, 145, 150)], tv / tl * 100))
    with open(os.path.join(OUT, "event_counts.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "事件", "有融資餘額檔數", "<130全部", "<140全部", "<145全部", "<150全部", "<130一般股", "<140一般股", "<145一般股", "<150一般股", "整體維持率%"])
        for d, label, n, ca, cc, mk in ev_rows:
            w.writerow([d, label, n, *ca, *cc, f"{mk:.1f}"])

    # 圖
    if not a.no_charts:
        try:
            make_charts(allrows, corerows, sims)
        except Exception as e:  # noqa: BLE001
            print("畫圖失敗:", type(e).__name__, e)
    print(f"\n輸出在 {OUT}   耗時 {time.time() - t0:.0f} 秒")


def make_charts(allrows, corerows, sims):
    import datetime as dt
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei", "SimHei"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    x = [dt.date.fromisoformat(r["d"]) for r in allrows]
    xc = [dt.date.fromisoformat(r["d"]) for r in corerows]

    fig, ax = plt.subplots(3, 1, figsize=(12, 10), sharex=True, gridspec_kw={"height_ratios": [1.1, 1.3, 1.1]})
    ax[0].plot(x, [r["mkt"] for r in allrows], color="#2a78d6", lw=1.2, label="估計：上市+上櫃 Σ市值/Σ借款")
    # 對照線：證交所官方「全市場擔保維持率」(整戶口徑，2026-08 起)，由 market_maint.py build 產生；口徑不同，水準會比估計高
    ref = os.path.join(HERE, "out", "market_maint_daily.csv")
    if os.path.exists(ref):
        wx, wy = [], []
        with open(ref, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("official"):
                    wx.append(dt.date.fromisoformat(r["date"][:10]))
                    wy.append(float(r["official"]))
        if wx:
            ax[0].plot(wx, wy, color="#d62728", lw=1.4, label="證交所官方 全市場擔保維持率 (整戶口徑)")
    ax[0].axhline(130, color="#c62828", lw=0.8, ls=":")
    ax[0].set_ylabel("整體維持率 %")
    ax[0].legend(loc="upper left", fontsize=9)
    ax[0].set_title("融資維持率廣度 (逐檔估計)：整體水準、低於門檻的檔數占比、借款占比", fontsize=12)

    for thr, col in ((130, "#c62828"), (140, "#eb6834"), (150, "#2a78d6")):
        ax[1].plot(xc, [100.0 * r[f"cnt{thr}"] / r["n"] if r["n"] else None for r in corerows], color=col, lw=1.1, label=f"低於 {thr}%")
    ax[1].set_ylabel("一般股票 (4碼) 檔數占比 %")
    ax[1].legend(loc="upper left", fontsize=9)
    for thr, col in ((130, "#c62828"), (140, "#eb6834"), (150, "#2a78d6")):
        ax[2].plot(x, [100.0 * r[f"w{thr}"] for r in allrows], color=col, lw=1.1, label=f"低於 {thr}%")
    ax[2].set_ylabel("借款金額占比 %")
    ax[2].legend(loc="upper left", fontsize=9)
    for a_ in ax:
        a_.grid(alpha=0.25)
    ax[2].xaxis.set_major_locator(mdates.YearLocator())
    ax[2].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "charts", "breadth.png"), dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--src", default="twse", choices=["twse", "tpex"])
    c.add_argument("--start", default=None)
    c.add_argument("--end", default=None)
    c.add_argument("--burn", type=int, default=40)
    f = sub.add_parser("final")
    f.add_argument("--removal", default="avg", choices=["fifo", "lifo", "avg"])
    f.add_argument("--price", default="vwap", choices=["close", "vwap", "typ", "mid", "open"])
    # 採用使用者指定的結構：當天買進又賣出 = 當沖，沖掉今天剛買的那批 (後進先出)，其餘賣出才用平均成本扣舊庫存 -> 1.0。
    # 資料對這個比例的鑑別力很弱 (0.75~1.0 的賣出金額誤差差不多)，但廣度檔數對它非常敏感，所以另外做敏感度分析。
    f.add_argument("--dt-frac", dest="dt_frac", type=float, default=1.0)
    f.add_argument("--start", default=None)
    f.add_argument("--end", default=None)
    f.add_argument("--burn", type=int, default=150)
    f.add_argument("--out", default=None, help="輸出資料夾 (預設 margin_breadth/out)")
    f.add_argument("--net", action="store_true", help="只用每天餘額的淨變動推估 (買賣與現償互相抵銷，不另外處理當沖)；--dt-frac 在這個模式下不起作用")
    f.add_argument("--no-charts", dest="no_charts", action="store_true", help="不畫 out/charts/breadth.png (排程環境不需要)")
    a = ap.parse_args()
    {"calibrate": cmd_calibrate, "final": cmd_final}[a.cmd](a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
