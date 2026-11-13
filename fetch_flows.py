# -*- coding: utf-8 -*-
"""逐日抓「個股融資買進/賣出/現償/餘額」與「個股價格」，存成 data/raw/{src}/YYYYMMDD.json.gz。可中斷、可續跑。

    python fetch_flows.py twse|tpex [--start 2019-01-01] [--end 2026-11-13] [--order desc|asc] [--gap 2.0]

來源 (全部是公開資料，節流比照 margin_leverage_indicator/fetch_stock_level.py：每個請求間隔 >= 2 秒)：
  twse  證交所 MI_MARGN (融資融券彙總，含全市場融資金額) + MI_INDEX (每日收盤行情)
  tpex  櫃買   融資融券餘額 (含融資金合計)             + 上櫃股票每日收盤行情 (新版網址；舊版行情網址不認日期)

檔案內容 (compact JSON, gzip)：
  d  交易日 YYYY-MM-DD
  u  全市場融資 (張):       [買進, 賣出, 現金償還, 前日餘額, 今日餘額]
  a  全市場融資金額 (仟元):  同上順序  (官方彙總，可拿來對帳)
  m  {代號: [買進, 賣出, 現金償還, 前日餘額, 今日餘額]}   逐檔融資 (張)，只留有任何數字的檔
  p  {代號: [開, 高, 低, 收, 成交股數, 成交金額(元)]}      逐檔價格，只留 m 裡有的代號；當天沒成交的檔沒有
休市日存成 YYYYMMDD.empty，下次不再請求；最近 3 天內的「沒資料」不存 (可能只是還沒公布)。

結束碼：0 正常跑完；3 網路/封鎖問題停止 (已存的不會掉，重跑會從沒抓過的日子繼續)；4 連續多天資料格式異常停止。
"""
import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw")
H = {"User-Agent": "Mozilla/5.0"}
MAX_CONSECUTIVE_ANOMALIES = 5


class FetchError(RuntimeError):
    """網路錯誤、回傳不是 JSON：可能被擋，要停下來。"""


class NoData(Exception):
    """該日沒有資料 (休市或尚未公布)。"""


class DataError(RuntimeError):
    """回傳是 JSON 但內容不是預期格式：記錄後略過這一天。"""


def log(msg):
    print(f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def num(s):
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def tidy(v):
    """None 保持 None；整數值存成 int，檔案小一點。"""
    if v is None:
        return None
    return int(v) if float(v).is_integer() else v


class Client:
    def __init__(self, gap):
        self.s = requests.Session()
        self.s.headers.update(H)
        self.gap = gap
        self.last = 0.0

    def get_json(self, url, tries=5):
        for k in range(tries):
            wait = self.gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=40)
                self.last = time.time()
                return r.json()
            except Exception as e:  # noqa: BLE001
                self.last = time.time()
                backoff = 30 * (k + 1)
                log(f"  retry {k + 1}/{tries} in {backoff}s: {type(e).__name__} ...{url[-60:]}")
                time.sleep(backoff)
        raise FetchError(url)


# ------------------------------------------------------------------ 上市
def fetch_twse(c, d):
    ds = d.strftime("%Y%m%d")
    m = c.get_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ds}&selectType=ALL&response=json")
    if m.get("stat") != "OK":
        if "沒有符合條件" in str(m.get("stat")):
            raise NoData()
        raise DataError(f"MI_MARGN stat={m.get('stat')}")
    try:
        summ = next(t for t in m["tables"] if "信用交易統計" in t["title"])
        det = next(t for t in m["tables"] if "融資融券彙總" in t["title"])
        srow = {r[0].strip(): [num(x) for x in r[1:6]] for r in summ["data"]}
        u = srow["融資(交易單位)"]
        a = next(v for k, v in srow.items() if k.startswith("融資金額"))
        rows = {}
        for r in det["data"]:
            vals = [num(r[i]) or 0 for i in range(2, 7)]          # 買, 賣, 現償, 前日, 今日
            if any(vals):
                rows[r[0].strip()] = [tidy(v) for v in vals]
    except (StopIteration, KeyError, IndexError, TypeError) as e:
        raise DataError(f"MI_MARGN 格式不符: {type(e).__name__} {e}")

    x = c.get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ds}&type=ALLBUT0999&response=json")
    if x.get("stat") != "OK":
        raise DataError(f"MI_INDEX stat={x.get('stat')}")
    try:
        pt = next(t for t in x["tables"] if t.get("fields") and "證券代號" in t["fields"] and "收盤價" in t["fields"])
        f = pt["fields"]
        ix = [f.index(k) for k in ("開盤價", "最高價", "最低價", "收盤價", "成交股數", "成交金額")]
    except (StopIteration, ValueError, KeyError, TypeError) as e:
        raise DataError(f"MI_INDEX 格式不符: {type(e).__name__} {e}")
    px = {}
    for r in pt["data"]:
        code = r[0].strip()
        if code in rows:
            v = [num(r[i]) for i in ix]
            if v[3]:                                              # 有收盤價才算有成交
                px[code] = [tidy(t) for t in v]
    return {"d": d.strftime("%Y-%m-%d"), "u": [tidy(v) for v in u], "a": [tidy(v) for v in a], "m": rows, "p": px}


# ------------------------------------------------------------------ 上櫃
def fetch_tpex(c, d):
    slash = d.strftime("%Y/%m/%d")
    j = c.get_json(f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={slash}&id=&response=json")
    tabs = j.get("tables") or []
    if not tabs or not tabs[0].get("data"):
        raise NoData()
    try:
        t = tabs[0]
        f = [x.strip() for x in t["fields"]]
        ix = [f.index(k) for k in ("資買", "資賣", "現償", "前資餘額(張)", "資餘額")]
        rows = {}
        for r in t["data"]:
            vals = [num(r[i]) or 0 for i in ix]
            if any(vals):
                rows[r[0].strip()] = [tidy(v) for v in vals]
        srow = {s[1].strip(): s for s in t["summary"]}
        us, as_ = srow["合計(張)"], next(v for k, v in srow.items() if k.startswith("融資金"))
        # 彙總列的順序是 [前日, 買, 賣, 現償, 今日] (第 2~6 欄)，換成跟上市一樣的 [買, 賣, 現償, 前日, 今日]
        def reorder(row):
            p = [num(row[i]) for i in range(2, 7)]
            return [tidy(p[1]), tidy(p[2]), tidy(p[3]), tidy(p[0]), tidy(p[4])]
        u, a = reorder(us), reorder(as_)
    except (StopIteration, ValueError, KeyError, IndexError, TypeError) as e:
        raise DataError(f"TPEx 融資餘額格式不符: {type(e).__name__} {e}")

    q = c.get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=EW&id=&response=json")
    qt = next((t for t in (q.get("tables") or []) if "每日收盤行情" in t.get("title", "")), None)
    if qt is None or not qt.get("data"):
        raise DataError("TPEx 行情沒有資料表")
    try:
        qf = [x.strip() for x in qt["fields"]]
        qi = [qf.index(k) for k in ("開盤", "最高", "最低", "收盤", "成交股數", "成交金額(元)")]
    except ValueError as e:
        raise DataError(f"TPEx 行情欄位不符: {e}")
    px = {}
    for r in qt["data"]:
        code = r[0].strip()
        if code in rows:
            v = [num(r[i]) for i in qi]
            if v[3]:
                px[code] = [tidy(t) for t in v]
    return {"d": d.strftime("%Y-%m-%d"), "u": u, "a": a, "m": rows, "p": px}


FETCH = {"twse": fetch_twse, "tpex": fetch_tpex}


def sanity(rec):
    """逐檔加總 vs 官方彙總、逐檔恆等式。只回報，不擋。"""
    m, u = rec["m"], rec["u"]
    sums = [sum(v[i] for v in m.values()) for i in range(5)]
    diff = [round(s - (t or 0)) for s, t in zip(sums, u)]
    bad = sum(1 for v in m.values() if abs(v[3] + v[0] - v[1] - v[2] - v[4]) > 0.5)
    ident = (u[3] or 0) + (u[0] or 0) - (u[1] or 0) - (u[2] or 0) - (u[4] or 0)
    return diff, bad, ident


def save(src, d, rec):
    folder = os.path.join(RAW, src)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, d.strftime("%Y%m%d") + ".json.gz")
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", choices=["twse", "tpex"])
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--order", choices=["desc", "asc"], default="desc")
    ap.add_argument("--gap", type=float, default=2.0)
    a = ap.parse_args()
    start, end = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)

    folder = os.path.join(RAW, a.src)
    os.makedirs(folder, exist_ok=True)
    days = list(weekdays(start, end))
    days.sort(reverse=(a.order == "desc"))
    todo = [d for d in days
            if not os.path.exists(os.path.join(folder, d.strftime("%Y%m%d") + ".json.gz"))
            and not os.path.exists(os.path.join(folder, d.strftime("%Y%m%d") + ".empty"))]
    log(f"{a.src}: {len(days)} weekdays in {start}..{end}, {len(todo)} to fetch (gap {a.gap}s, {a.order})")

    c = Client(a.gap)
    fetch = FETCH[a.src]
    recent = dt.date.today() - dt.timedelta(days=3)
    t0, saved, empty, anomalies, consecutive = time.time(), 0, 0, 0, 0
    warn_days = 0
    for i, d in enumerate(todo, 1):
        try:
            rec = fetch(c, d)
            consecutive = 0
        except NoData:
            consecutive = 0
            if d <= recent:
                open(os.path.join(folder, d.strftime("%Y%m%d") + ".empty"), "w").close()
                empty += 1
            continue
        except DataError as e:
            anomalies += 1
            consecutive += 1
            log(f"  skip {d}: {e}")
            if consecutive >= MAX_CONSECUTIVE_ANOMALIES:
                log(f"  STOP: {consecutive} 天連續格式異常，可能網站改版或被擋；已存 {saved} 天")
                return 4
            continue
        except FetchError as e:
            log(f"  STOP: 連線失敗 (可能被擋)，已存 {saved} 天；稍後重跑會從沒抓的日子繼續。{e}")
            return 3
        diff, bad, ident = sanity(rec)
        if any(diff) or ident:
            warn_days += 1
            log(f"  check {d}: 逐檔加總-彙總 {diff}  恆等式殘差(彙總) {ident}")
        save(a.src, d, rec)
        saved += 1
        if saved % 25 == 0 or i == len(todo):
            el = time.time() - t0
            eta = el / i * (len(todo) - i)
            log(f"  {i}/{len(todo)}  saved {saved} empty {empty} anomalies {anomalies}  last {d}  elapsed {el / 60:.0f}m  eta {eta / 60:.0f}m")
    log(f"DONE {a.src}: saved {saved}, empty {empty}, anomalies {anomalies}, 逐檔加總與彙總不符的日子 {warn_days}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
