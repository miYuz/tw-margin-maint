# -*- coding: utf-8 -*-
"""把 out/ 裡的維持率資料做成一頁 HTML 儀表板 (out/margin_dashboard.html)。

    python make_dashboard.py                       # 重新產生 out/margin_dashboard.html
    python make_dashboard.py --end 2026-11-12      # 只放到某一天 (排程用，見 update.py)
    python make_dashboard.py --fragment PATH.html  # 另外寫一份「沒有 doctype/head/body」的版本 (給 Artifact 發佈用)

版面：最上面 加權指數(左軸) + 大盤融資維持率(右軸，有證交所官方值的日子用官方，之前用上市重算)；中間 低於 130% / 140% / 150% 的股票占比 (130% 最深、150% 最淺)；
最下面 目前維持率最低的 20 檔 (可改排序與最小融資餘額)。只放上市。風格比照 taifex-vix / taifex-gex 儀表板。
資料來源 (都是 out/ 裡已經算好的)：market_maint_daily.csv、breadth_daily_twse_core.csv、latest_stock_maint.csv。
加權指數收盤：證交所 FMTQIK (市場成交資訊，逐月)；存在 data/market/taiex_close.csv，只補沒抓過的月份 (合併，不覆蓋)。
第一次跑會用 kairos_txf_v3/margin_leverage_indicator/data/taiex.csv 當種子，再補到最新。

前提：先跑過 market_maint.py (refresh / calc / build) 與 estimator.py final。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
DATA = os.path.join(HERE, "data", "market")
TAIEX_FILE = os.path.join(DATA, "taiex_close.csv")
SEED = os.path.join(os.path.dirname(HERE), "kairos_txf_v3", "margin_leverage_indicator", "data", "taiex.csv")
TEMPLATE = os.path.join(HERE, "dashboard_template.html")
FMTQIK = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
H = {"User-Agent": "Mozilla/5.0"}


def log(msg):
    print(f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch_month(first_day):
    """FMTQIK：一個月的每日加權指數收盤。"""
    r = requests.get(FMTQIK, params={"date": first_day.strftime("%Y%m01"), "response": "json"}, headers=H, timeout=40)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK":
        return pd.Series(dtype=float)
    i = j["fields"].index("發行量加權股價指數")
    rows = {}
    for row in j["data"]:
        y, m, d = row[0].split("/")
        rows[pd.Timestamp(int(y) + 1911, int(m), int(d))] = float(row[i].replace(",", ""))
    return pd.Series(rows).sort_index()


def load_taiex(need_dates):
    """合併式存檔：舊的列保留、同日用新值；回傳涵蓋 need_dates 的收盤序列。"""
    if os.path.exists(TAIEX_FILE):
        arc = pd.read_csv(TAIEX_FILE, parse_dates=["date"]).set_index("date")["close"]
    elif os.path.exists(SEED):
        arc = pd.read_csv(SEED, parse_dates=["date"]).set_index("date")["close"]
        arc = arc[arc.index >= "2018-12-01"]
        log(f"taiex: seeded {len(arc)} rows from {SEED}")
    else:
        arc = pd.Series(dtype=float)
    last = arc.index.max() if len(arc) else pd.Timestamp("2018-12-01")
    month = pd.Timestamp(last.year, last.month, 1)
    today = pd.Timestamp(dt.date.today())
    got = []
    while month <= today:
        try:
            s = fetch_month(month)
        except Exception as e:  # noqa: BLE001
            log(f"taiex: {month:%Y-%m} fetch failed ({e}); keeping what is archived")
            s = pd.Series(dtype=float)
        got.append(s)
        month = (month + pd.offsets.MonthBegin(1))
        time.sleep(2.0)
    if got:
        new = pd.concat(got)
        both = arc.index.intersection(new.index)
        if len(both) and float((arc[both] - new[both]).abs().max()) > 0.01:
            log(f"WARNING taiex: {int(((arc[both] - new[both]).abs() > 0.01).sum())} archived closes differ from the new pull; using the new values")
        arc = pd.concat([arc[~arc.index.isin(new.index)], new]).sort_index()
    os.makedirs(DATA, exist_ok=True)
    tmp = TAIEX_FILE + ".tmp"
    arc.rename("close").to_csv(tmp, index_label="date", date_format="%Y-%m-%d")
    os.replace(tmp, TAIEX_FILE)
    miss = pd.DatetimeIndex(need_dates).difference(arc.index)
    if len(miss):
        raise RuntimeError(f"no TAIEX close for {len(miss)} dates, e.g. {[d.date().isoformat() for d in miss[:5]]}")
    return arc


def nul(a, nd):
    """numpy -> list with None for NaN (JSON null)."""
    return [None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(float(v), nd) for v in a]


def trim_and_fill_official(m, max_gap=3):
    """有證交所官方維持率的日子用官方，所以：
    * 最新幾天官方值還沒公布 -> 這幾天先不放 (圖、表停在同一天，等官方出來下一次排程再補上)；
    * 官方起始日之後、最後官方日之前的缺口 (官方沒有這天) -> 用前後兩個官方值線性內插，最多 max_gap 天，再多就丟錯。
    m: index 為日期、有 official 欄的 DataFrame。回傳 (m, 被內插的日期 list)；完全沒有官方值就原樣回傳。"""
    if not m["official"].notna().any():
        return m, []
    m = m[m.index <= m["official"].last_valid_index()].copy()
    first = m["official"].first_valid_index()
    gaps = m.index[(m.index > first) & m["official"].isna()]
    if len(gaps) > max_gap:
        raise RuntimeError(f"official maintenance is missing on {len(gaps)} days after its first day, e.g. "
                           f"{[d.date().isoformat() for d in gaps[:5]]}; run market_maint.py refresh --only official first "
                           f"(the official window is only 60 trading days)")
    if len(gaps):
        m["official"] = m["official"].interpolate(limit_area="inside")
    return m, [d.date().isoformat() for d in gaps]


def build_data(src=OUT, end=None):
    """只放上市。維持率：有證交所官方值的日子 (2026-08-03 起，全市場整戶口徑) 用官方，之前用上市重算；
    廣度與個股表來自 estimator.py final 的上市部分 (src 資料夾裡的 breadth_daily_twse_core.csv、latest_stock_maint.csv 的上市)。
    src 預設是 out/；比較其他扣帳設定時可指到 out/sens/<名稱>。
    end (YYYY-MM-DD)：只放到這一天為止；排程用它讓圖、表都停在「各來源都有資料的最後一天」(見 update.py)。"""
    m = pd.read_csv(os.path.join(OUT, "market_maint_daily.csv"), parse_dates=["date"]).set_index("date")
    m = m[m["calc_listed"].notna()]
    if end:
        m = m[m.index <= pd.Timestamp(end)]
    m, filled = trim_and_fill_official(m)
    if filled:
        log(f"WARNING: official maintenance missing on {filled}; linearly interpolated between its neighbours")
    dates = m.index
    taiex = load_taiex(dates).reindex(dates)
    official = m["official"].notna().to_numpy()
    off_from = int(np.argmax(official)) if official.any() else len(dates)
    if official.any() and not official[off_from:].all():
        raise RuntimeError("official maintenance has gaps after its first day; run market_maint.py refresh/build first")
    maint = m["official"].where(m["official"].notna(), m["calc_listed"])

    core = pd.read_csv(os.path.join(src, "breadth_daily_twse_core.csv"), encoding="utf-8-sig", index_col=0, parse_dates=True)
    n = core["有融資餘額檔數"].reindex(dates)
    cnt = {t: core[f"低於{t}%檔數"].reindex(dates) for t in (130, 140, 150)}
    first_b = int(np.argmax(n.notna().to_numpy()))

    s = pd.read_csv(os.path.join(src, "latest_stock_maint.csv"), encoding="utf-8-sig")
    s = s[(s["市場"] == "上市") & s["代號"].astype(str).str.fullmatch(r"\d{4}")]
    stocks = [[str(r["代號"]), str(r["名稱"]), round(float(r["估計維持率%"]), 1), round(float(r["估計借款(仟元)"]) / 1e5, 3),
               int(round(float(r["融資餘額(張)"]))), float(r["收盤價"]), round(float(r["估計平均融資成本"]), 1)] for _, r in s.iterrows()]
    stocks.sort(key=lambda x: x[2])
    data = {
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "taiex": nul(taiex.to_numpy(), 2),
        "m": nul(maint.to_numpy(), 2), "offFrom": off_from,
        "n": nul(n.to_numpy(), 0), "firstB": first_b, "stocks": stocks,
    }
    for t, c in cnt.items():
        data[f"c{t}"] = nul(c.to_numpy(), 0)
        data[f"b{t}"] = nul((c / n * 100).to_numpy(), 2)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fragment", help="另寫一份沒有 doctype/head/body 的 HTML (Artifact 發佈用)")
    ap.add_argument("--src", default=OUT, help="廣度與個股表的來源資料夾 (預設 out/；例如 out/sens/net 是只用餘額淨變動的版本)")
    ap.add_argument("--end", help="只放到這一天 (YYYY-MM-DD)；預設放到 market_maint_daily.csv 的最後一個官方日")
    a = ap.parse_args(argv)
    data = build_data(os.path.abspath(a.src), a.end)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tpl = open(TEMPLATE, encoding="utf-8").read()
    head, body = tpl.split("<!--BODY-->", 1)
    fragment = (head + body).replace("/*__DATA__*/null", payload)
    standalone = ('<!doctype html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
                  '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                  + head.replace("/*__DATA__*/null", payload) + "</head>\n<body>" + body.replace("/*__DATA__*/null", payload) + "\n</body>\n</html>\n")
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "margin_dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(standalone)
    log(f"wrote {path} ({os.path.getsize(path) / 1024:.0f} KB): {len(data['dates'])} days {data['dates'][0]}..{data['dates'][-1]}, breadth from {data['dates'][data['firstB']]}, {len(data['stocks'])} stocks")
    if a.fragment:
        with open(a.fragment, "w", encoding="utf-8") as f:
            f.write(fragment)
        log(f"wrote fragment {a.fragment}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
