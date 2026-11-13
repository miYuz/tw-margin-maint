# tw-margin-maint

用**證交所、櫃買中心的公開資料**看台股的**融資維持率**：大盤整體的維持率，加上逐檔估計值與「維持率低於 130 / 140 / 150% 的股票占比」。
免帳號、免券商 API，每個交易日晚上自動更新。

📊 **[互動儀表板](https://miyuz.github.io/tw-margin-maint/)** — 加權指數與大盤融資維持率 (雙軸)、低於 130/140/150% 的股票占比、維持率低於 130% 且估計借款最大的 20 檔 (上市)

> 逐檔維持率官方不公布，這裡的逐檔與占比都是**估計值**；只有大盤那條在 2026-08-03 起是證交所官方值。僅供研究參考，不構成投資建議。

---

## 資料現況

| | |
|---|---|
| 逐日原始資料 | 2019-01-02 起，上市、上櫃各一份 (每個交易日一個檔，見 [`data/raw/`](data/raw/)) |
| 逐檔估計與占比 | 2019-08-20 起 (前 150 個交易日是成本模擬的預熱，不輸出) |
| 大盤維持率 | 2019 起由逐檔資料重算；2026-08-03 起用證交所官方值 |
| 更新 | 每個交易日晚上，見下方「每日更新」 |

主要輸出 (全部在 [`out/`](out/))：

| 檔案 | 內容 |
|---|---|
| `market_maint_daily.csv` | 大盤維持率每日一列：官方 (`official`)、逐檔重算 (`calc_listed` / `calc_otc` / `calc_all`)、合成後的 `maint_listed` / `maint_all`，以及玩股網對照欄 (`wg_*`) |
| `breadth_daily_twse_core.csv` | 上市一般股票 (4 碼代號) 每日低於 120/130/140/145/150/160% 的檔數與借款占比 — 儀表板用的就是它 |
| `breadth_daily_tpex_core.csv` | 同上，上櫃 |
| `breadth_daily_core.csv` / `breadth_daily_all.csv` | 上市+上櫃合計；`core` 只算 4 碼代號的一般股票，`all` 含 ETF、特別股等 |
| `latest_stock_maint.csv` | 最新一天逐檔的估計平均融資成本、維持率、估計借款 |

---

## 怎麼算

**大盤維持率**
- 官方：證交所「台股儀表板」的全市場擔保維持率 (上市+上櫃合併、整戶口徑)。API 只給最近 60 個交易日，所以每天跟 [`data/market/twse_keeprate.csv`](data/market/twse_keeprate.csv) **合併存檔**，列只增不減 (2026-08-03 起)。
- 重算：Σ 逐檔融資餘額 × 收盤價 ÷ 官方融資金額，用來補官方開始之前的歷史。官方是整戶口徑 (含融券擔保品等)，平均比上市重算高約 1.5 個百分點，所以兩段接合處有一個台階，儀表板上有標記。
- 櫃買中心沒有公布維持率；上櫃單獨的維持率只能重算。

**逐檔維持率 (估計)**
- 用兩家交易所每天公布的逐檔融資買進、賣出、現金償還、餘額，加上收盤價，替每檔維護一份融資成本庫存：賣出與現償用平均成本扣帳，當天買進又賣出的量視為當沖 (沖掉當天剛買的那批)，買進以當日成交均價入帳。
- 維持率 = 收盤價 ÷ (平均成本 × 融資成數)，融資成數由官方買進金額反推 (約 0.57~0.58，不是名目的 0.6)；每天把全部成本縮放到官方公布的全市場融資金額餘額；股票分割、減資等公司行動造成的餘額跳動另外處理。
- 占比 = 低於門檻的檔數 ÷ 當天有融資餘額且有價格的一般股票檔數。
- 估計的水準對「當沖怎麼處理」很敏感 (換規則，占比的水準會差好幾個百分點，但逐日起伏方向相近)；請把它當作相對變化與廣度的參考，不是每一檔的確切數字。每天用全部歷史重算，所以過去的數字可能隨新資料有些微修正。

---

## 每日更新

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) 每個交易日排三個時段 (台北 21:47 為主，23:53 備援，隔天 06:23 補漏)，跑 [`update.py`](update.py)：

1. 抓還沒有的交易日 (`fetch_flows.py`，上市與上櫃的逐檔融資 + 價格，已抓的不重抓，休市日存成 `.empty`)
2. 抓證交所官方維持率並合併存檔 (`market_maint.py refresh --only official`)
3. 重算大盤維持率 (`market_maint.py calc` / `build`)
4. 重跑逐檔估計與占比 (`estimator.py final`，約 1 分鐘)
5. 重建儀表板 `docs/index.html`，有新資料才 commit，並發布到 GitHub Pages

證交所與櫃買都是當天晚上才公布融資融券與官方維持率，時間不固定；**三者都到齊的最後一天**才會進儀表板，哪一邊還沒出就停在前一天，下一個時段補上。GitHub 的排程是 best-effort，可能延遲或偶爾漏跑，所以排三個時段；沒有新資料時不會產生 commit。

玩股網的大盤維持率只當對照 (`wg_*` 欄)，排程不抓，只在本機執行 `python market_maint.py refresh` 時更新。

## 本機執行

```bash
pip install -r requirements.txt
python update.py                 # 抓缺的日子 → 重算 → 重建 docs/index.html
python update.py --skip-fetch    # 不上網，只用現有檔案重算、重建
python -m unittest discover -s tests -v
```

第一次完整回補 2019 年以來的逐日資料約需 2 小時 (每個請求間隔 2 秒以上，可中斷、可續跑)：

```bash
python fetch_flows.py twse --start 2019-01-01
python fetch_flows.py tpex --start 2019-01-01
```

## 檔案

```
update.py                      每日更新總程式
fetch_flows.py                 逐日抓逐檔融資與價格 (twse | tpex)，可續跑
market_maint.py                大盤維持率：官方 (合併存檔) + 由逐檔資料重算
estimator.py                   逐檔成本庫存模擬、維持率與廣度 (calibrate 比較規則 / final 輸出)
make_dashboard.py              產生儀表板，連同 dashboard_template.html
data/raw/{twse,tpex}/          原始逐日檔 YYYYMMDD.json.gz；YYYYMMDD.empty = 休市日
data/market/                   大盤層級存檔：官方維持率、加權指數收盤、玩股網對照、重算結果
data/stock_names.csv           代號 → 名稱
out/                           輸出 CSV
docs/index.html                儀表板 (GitHub Pages 發布的就是它)
tests/                         離線單元測試 (答案都是人工算的)
```

## 資料來源 (全部公開、免登入)

| 用途 | 來源 |
|---|---|
| 上市逐檔融資、全市場融資金額 | 證交所 `rwd/zh/marginTrading/MI_MARGN` |
| 上市價格 | 證交所 `rwd/zh/afterTrading/MI_INDEX` |
| 上櫃逐檔融資、融資金額 | 櫃買中心 `www/zh-tw/margin/balance` |
| 上櫃價格 | 櫃買中心 `www/zh-tw/afterTrading/otc` |
| 官方全市場擔保維持率 | 證交所 `rwd/zh/marginTrading/BFIJ3U_TREND` (台股儀表板) |
| 加權指數收盤 | 證交所 `rwd/zh/afterTrading/FMTQIK` |
| 大盤維持率對照 (僅本機) | 玩股網 |

資料版權屬各來源所有；本專案只整理、計算公開資料。
