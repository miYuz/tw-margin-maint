# -*- coding: utf-8 -*-
"""每日更新：抓缺的日子 -> 重算 -> 重建儀表板 (docs/index.html)。GitHub Actions 排程與手動都跑這一支。

    python update.py                  # 全部步驟
    python update.py --skip-fetch     # 不上網抓逐日資料與官方維持率，只用現有檔案重算、重建
    python update.py --no-dashboard   # 做到估算為止，不重建儀表板

每一步都是獨立的子行程。某一步失敗 (結束碼非 0) 會記下來、後面的步驟照跑 (已抓到的資料不浪費)，
最後只要有失敗就回傳 1 (排程會顯示紅燈)，沒有就回傳 0。

  1. fetch_flows.py twse、tpex       只抓還沒有的交易日；已抓的與休市日 (.empty) 不重抓；最近 3 天沒資料不存 (可能還沒公布)
  2. market_maint.py refresh --only official   證交所官方全市場維持率 (視窗只有 60 個交易日，與 data/market/ 合併存檔)
  3. market_maint.py calc、build      由逐檔資料重算大盤維持率、合成 out/market_maint_daily.csv
  4. estimator.py final --end END     逐檔維持率與廣度。END = 上市 raw、上櫃 raw、官方維持率「三者都有資料的最後一天」，
                                      所以所有輸出停在同一天；哪一邊還沒公布，就等下一次排程
  5. make_dashboard.py --end END      -> out/margin_dashboard.html，並複製成 docs/index.html (GitHub Pages 發布的就是它)

排程時間與設定見 .github/workflows/daily.yml。
"""
import argparse
import csv
import datetime as dt
import glob
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def log(msg):
    print(f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def last_raw_day(src, root=HERE):
    """data/raw/<src>/ 裡最後一個有資料 (.json.gz) 的日期 YYYY-MM-DD；沒有就回傳 None。"""
    files = sorted(glob.glob(os.path.join(root, "data", "raw", src, "*.json.gz")))
    if not files:
        return None
    d = os.path.basename(files[-1])[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def last_official_day(root=HERE):
    """data/market/twse_keeprate.csv 裡最後一個有官方維持率的日期；沒有就回傳 None。"""
    path = os.path.join(root, "data", "market", "twse_keeprate.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as f:
        days = [r["date"] for r in csv.DictReader(f) if r.get("keep_rate")]
    return max(days) if days else None


def latest_common_day(root=HERE):
    """上市 raw、上櫃 raw、官方維持率三者都有資料的最後一天；任何一個還沒有資料就回傳 None。"""
    days = [last_raw_day("twse", root), last_raw_day("tpex", root), last_official_day(root)]
    return None if any(d is None for d in days) else min(days)


def run(label, args, failures):
    """用同一個 Python 執行本資料夾的一支程式。失敗就記進 failures。"""
    t0 = time.time()
    log(f"== {label}: {' '.join(args)}")
    r = subprocess.run([sys.executable] + args, cwd=HERE, env=ENV)
    log(f"   {label}: exit {r.returncode} ({time.time() - t0:.0f}s)")
    if r.returncode != 0:
        failures.append(f"{label} (exit {r.returncode})")
    return r.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-fetch", action="store_true", help="不上網抓逐日資料與官方維持率")
    ap.add_argument("--no-dashboard", action="store_true", help="不重建儀表板")
    a = ap.parse_args(argv)
    failures = []

    if not a.skip_fetch:
        run("fetch twse", ["fetch_flows.py", "twse", "--order", "asc"], failures)
        run("fetch tpex", ["fetch_flows.py", "tpex", "--order", "asc"], failures)
        run("official keep rate", ["market_maint.py", "refresh", "--only", "official"], failures)

    ok = run("recompute market level", ["market_maint.py", "calc"], failures)
    ok = ok and run("build market series", ["market_maint.py", "build", "--no-chart"], failures)

    end = latest_common_day()
    if end is None:
        failures.append("no data yet (need twse raw, tpex raw and the official keep rate)")
    else:
        log(f"latest day available from every source (END): {end}  "
            f"[twse raw {last_raw_day('twse')}, tpex raw {last_raw_day('tpex')}, official {last_official_day()}]")
        ok = ok and run("per-stock estimate", ["estimator.py", "final", "--removal", "avg", "--price", "vwap", "--dt-frac", "1.0",
                                               "--burn", "150", "--end", end, "--no-charts"], failures)
        if ok and not a.no_dashboard:
            if run("dashboard", ["make_dashboard.py", "--end", end], failures):
                os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
                shutil.copyfile(os.path.join(HERE, "out", "margin_dashboard.html"), os.path.join(HERE, "docs", "index.html"))
                log("copied out/margin_dashboard.html -> docs/index.html")

    if failures:
        log("FAILED steps: " + "; ".join(failures))
        return 1
    log("all steps ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
