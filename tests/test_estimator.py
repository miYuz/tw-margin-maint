# -*- coding: utf-8 -*-
"""estimator.simulate 的手算驗證。每個例子的答案都是人工算好的，不是拿程式輸出回填。
執行: python -m unittest discover -s tests -v   (在 margin_breadth 目錄)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from estimator import Params, implied_ratio_by_proxy, price_proxy, simulate  # noqa: E402


def px(c, o=None, h=None, l=None, q=1000, v=None):
    """價格列 [開,高,低,收,成交股數,成交金額]；沒給的用收盤價。"""
    return [o or c, h or c, l or c, c, q, v if v is not None else c * q]


def day(d, m, p, a):
    return {"d": d, "u": [0, 0, 0, 0, 0], "a": a, "m": m, "p": p}


def last(res, code="X"):
    row = next(r for r in res["last"] if r[0] == code)
    return dict(maint=row[1], loan=row[2], val=row[3])


def three_days(removal, daytrade=True):
    # 第1天: 庫存 10 張 @100 (第一天會自動對齊官方總額)；第2天: 新增 5 張 @120；第3天: 賣 8 張 @130
    d1 = day("2026-01-02", {"X": [0, 0, 0, 10, 10]}, {"X": px(100)}, [0, 0, 0, 600, 600])
    d2 = day("2026-01-03", {"X": [5, 0, 0, 10, 15]}, {"X": px(120)}, [360, 0, 0, 600, 960])
    d3 = day("2026-01-04", {"X": [0, 8, 0, 15, 7]}, {"X": px(130)}, [0, 480, 0, 960, 480])
    return simulate([d1, d2, d3], Params(removal=removal, daytrade=daytrade, ratio=0.6))


class LotRules(unittest.TestCase):
    def test_fifo_removes_oldest_first(self):
        # 剩 2 張@100 + 5 張@120: 總成本 800, 借款 480, 市值 7x130=910 -> 189.583%
        res = three_days("fifo")
        r = last(res)
        self.assertAlmostEqual(r["loan"], 480.0, places=6)
        self.assertAlmostEqual(r["val"], 910.0, places=6)
        self.assertAlmostEqual(r["maint"], 910 / 480 * 100, places=6)

    def test_lifo_removes_newest_first(self):
        # 先扣 5 張@120 再扣 3 張@100，剩 7 張@100: 借款 420 -> 216.667%
        r = last(three_days("lifo"))
        self.assertAlmostEqual(r["loan"], 420.0, places=6)
        self.assertAlmostEqual(r["maint"], 910 / 420 * 100, places=6)

    def test_average_cost(self):
        # 15 張平均成本 106.667，扣 8 張剩 7 張: 借款 7x106.667x0.6 = 448 -> 203.125%
        r = last(three_days("avg"))
        self.assertAlmostEqual(r["loan"], 448.0, places=6)
        self.assertAlmostEqual(r["maint"], 910 / 448 * 100, places=6)

    def test_daily_reconciliation_numbers(self):
        res = three_days("fifo")
        d2, d3 = res["daily"][1], res["daily"][2]
        self.assertAlmostEqual(d2["est_buy"], 5 * 120 * 0.6)          # 360
        self.assertAlmostEqual(d3["est_rm"], 8 * 100 * 0.6)           # 先進先出扣的是 100 元的批次: 480
        self.assertAlmostEqual(d3["est_end"], 800 * 0.6)              # 剩下批次 (2@100 + 5@120) 的借款 480
        self.assertAlmostEqual(d2["est_end"], (10 * 100 + 5 * 120) * 0.6)   # 960

    def test_first_day_is_anchored_to_official_total(self):
        # 第一天的成本是假設值: 官方總額 1200 vs 估 600 -> 成本全部 x2
        d1 = day("2026-01-02", {"X": [0, 0, 0, 10, 10]}, {"X": px(100)}, [0, 0, 0, 1200, 1200])
        res = simulate([d1], Params(ratio=0.6))
        self.assertAlmostEqual(res["daily"][0]["k"], 2.0)
        self.assertAlmostEqual(last(res)["loan"], 1200.0)


class DayTradeAndAdjustments(unittest.TestCase):
    def base(self):
        return day("2026-01-02", {"X": [0, 0, 0, 10, 10]}, {"X": px(100)}, [0, 0, 0, 600, 600])

    def test_daytrade_netting_keeps_inventory(self):
        d2 = day("2026-01-03", {"X": [4, 4, 0, 10, 10]}, {"X": px(120)}, [288, 288, 0, 600, 600])
        res = simulate([self.base(), d2], Params(daytrade=True, ratio=0.6))
        self.assertAlmostEqual(res["daily"][1]["est_buy"], 4 * 120 * 0.6)
        self.assertAlmostEqual(res["daily"][1]["est_rm"], 4 * 120 * 0.6)       # 當沖的借款以當天價計
        self.assertAlmostEqual(last(res)["loan"], 10 * 100 * 0.6)              # 庫存成本沒變

    def test_without_daytrade_netting_old_lots_are_consumed(self):
        d2 = day("2026-01-03", {"X": [4, 4, 0, 10, 10]}, {"X": px(120)}, [288, 240, 0, 600, 600])
        res = simulate([self.base(), d2], Params(daytrade=False, ratio=0.6))
        self.assertAlmostEqual(res["daily"][1]["est_rm"], 4 * 100 * 0.6)       # 扣的是舊的 100 元批次
        self.assertAlmostEqual(last(res)["loan"], (6 * 100 + 4 * 120) * 0.6)   # 6@100 + 4@120

    def test_stock_dividend_changes_shares_not_total_cost(self):
        d2 = day("2026-01-03", {"X": [0, 0, 0, 10, 11]}, {"X": px(100)}, [0, 0, 0, 600, 600])   # 配股 10%
        res = simulate([self.base(), d2], Params(ratio=0.6))
        r = last(res)
        self.assertAlmostEqual(r["loan"], 600.0)                   # 總借款不變
        self.assertAlmostEqual(r["val"], 11 * 100)                 # 市值 = 11 張 x 100
        self.assertAlmostEqual(r["maint"], 1100 / 600 * 100)

    def test_split_adjusted_previous_balance_keeps_total_cost(self):
        # 1 拆 5：官方前日餘額從 10 張調成 50 張，股價從 100 變 20 (市值不變) -> 每股成本縮成 20，借款還是 600
        d2 = day("2026-01-03", {"X": [0, 0, 0, 50, 50]}, {"X": px(20)}, [0, 0, 0, 600, 600])
        res = simulate([self.base(), d2], Params(ratio=0.6))
        r = last(res)
        self.assertAlmostEqual(r["loan"], 600.0)
        self.assertAlmostEqual(r["val"], 50 * 20)
        self.assertAlmostEqual(r["maint"], 1000 / 600 * 100)

    def test_reverse_split_adjusted_previous_balance_keeps_total_cost(self):
        # 5 合 1：前日餘額 10 張 -> 2 張，股價 100 -> 500
        d2 = day("2026-01-03", {"X": [0, 0, 0, 2, 2]}, {"X": px(500)}, [0, 0, 0, 600, 600])
        r = last(simulate([self.base(), d2], Params(ratio=0.6)))
        self.assertAlmostEqual(r["loan"], 600.0)
        self.assertAlmostEqual(r["maint"], 1000 / 600 * 100)

    def test_split_where_share_and_price_ratios_do_not_match_exactly(self):
        # 復牌那天張數放大 20 倍、價格只跌到 1/10 (倍數對不上)：仍是公司行動，總借款不變 (不能憑空多出借款)
        d2 = day("2026-01-03", {"X": [0, 0, 0, 200, 200]}, {"X": px(10)}, [0, 0, 0, 600, 600])
        r = last(simulate([self.base(), d2], Params(ratio=0.6)))
        self.assertAlmostEqual(r["loan"], 600.0)
        self.assertAlmostEqual(r["val"], 200 * 10)

    def test_ordinary_price_drop_with_balance_change_is_not_a_split(self):
        # 價格 -15% (一般大跌)、前日餘額 +8% (資料更正)：市值不守恆、價格也沒大到 -25%，不是公司行動，成本不能被縮小
        d2 = day("2026-01-03", {"X": [0, 0, 0, 10.8, 10.8]}, {"X": px(85)}, [0, 0, 0, 648, 648])
        r = last(simulate([self.base(), d2], Params(ratio=0.6)))
        self.assertAlmostEqual(r["loan"], 10.8 * 100 * 0.6)

    def test_balance_correction_without_price_move_is_not_a_corporate_action(self):
        # 前日餘額被更正 10 -> 12 張，股價沒變 -> 視為資料更正：多出來的 2 張以原成本計，借款變 720
        d2 = day("2026-01-03", {"X": [0, 0, 0, 12, 12]}, {"X": px(100)}, [0, 0, 0, 720, 720])
        r = last(simulate([self.base(), d2], Params(ratio=0.6)))
        self.assertAlmostEqual(r["loan"], 12 * 100 * 0.6)

    def test_inventory_is_realigned_to_official_previous_balance(self):
        d2 = day("2026-01-03", {"X": [0, 0, 0, 20, 20]}, {"X": px(100)}, [0, 0, 0, 1200, 1200])  # 官方前日餘額 20，我們只有 10
        res = simulate([self.base(), d2], Params(ratio=0.6))
        self.assertAlmostEqual(last(res)["val"], 20 * 100)
        self.assertAlmostEqual(last(res)["loan"], 20 * 100 * 0.6)

    def test_removal_larger_than_inventory_does_not_go_negative(self):
        d2 = day("2026-01-03", {"X": [0, 15, 0, 10, 0]}, {"X": px(100)}, [0, 600, 0, 600, 0])
        res = simulate([self.base(), d2], Params(ratio=0.6))
        self.assertEqual([r for r in res["last"] if r[0] == "X"], [])          # 餘額 0 -> 不在廣度名單

    def test_stock_without_price_uses_last_close(self):
        d2 = day("2026-01-03", {"X": [0, 0, 0, 10, 10]}, {}, [0, 0, 0, 600, 600])   # 當天沒成交 -> 沒價格列
        res = simulate([self.base(), d2], Params(ratio=0.6))
        self.assertAlmostEqual(last(res)["maint"], 10 * 100 / 600 * 100)       # 用前一天收盤 100: 市值 1000 / 借款 600

    def test_anchor_mode_forces_total_to_official_each_day(self):
        d2 = day("2026-01-03", {"X": [0, 0, 0, 10, 10]}, {"X": px(100)}, [0, 0, 0, 600, 660])   # 官方餘額 660
        res = simulate([self.base(), d2], Params(ratio=0.6, anchor=True))
        self.assertAlmostEqual(res["daily"][1]["est_end"], 660.0)
        self.assertAlmostEqual(last(res)["loan"], 660.0)


class Breadth(unittest.TestCase):
    def test_threshold_counts_and_loan_weights(self):
        # 起始成本都是 100 元：A 10 張、B 30 張，借款合計 (10+30) x 100 x 0.6 = 2400 (官方總額同此，k = 1)
        total = (10 + 30) * 100 * 0.6
        d1 = day("2026-01-02", {"A": [0, 0, 0, 10, 10], "B": [0, 0, 0, 30, 30]},
                 {"A": px(100), "B": px(100)}, [0, 0, 0, total, total])
        # 第二天 A 漲到 200 -> 維持率 200/(100x0.6) = 333%；B 跌到 75 -> 125%
        d2 = day("2026-01-03", {"A": [0, 0, 0, 10, 10], "B": [0, 0, 0, 30, 30]},
                 {"A": px(200), "B": px(75)}, [0, 0, 0, total, total])
        res = simulate([d1, d2], Params(ratio=0.6))
        row = res["daily"][1]
        self.assertEqual(row["cnt130"], 1)                         # 只有 B 低於 130
        self.assertEqual(row["cnt120"], 0)
        self.assertEqual(row["cnt150"], 1)
        self.assertEqual(row["cnt160"], 1)
        self.assertAlmostEqual(row["w130"], 30 / 40)               # B 的借款占全部借款 3/4
        self.assertAlmostEqual(row["w120"], 0.0)
        self.assertEqual([r[0] for r in res["last"]], ["B", "A"])  # 依維持率由低到高
        self.assertAlmostEqual(row["mkt"], (10 * 200 + 30 * 75) / (40 * 100 * 0.6) * 100)


class ProxyAndRatio(unittest.TestCase):
    def test_price_proxies(self):
        pv = [10, 14, 8, 12, 1000, 11000]
        self.assertEqual(price_proxy(pv, "close"), 12)
        self.assertEqual(price_proxy(pv, "open"), 10)
        self.assertAlmostEqual(price_proxy(pv, "vwap"), 11.0)
        self.assertAlmostEqual(price_proxy(pv, "typ"), (14 + 8 + 12) / 3)
        self.assertAlmostEqual(price_proxy(pv, "mid"), 11.0)
        self.assertIsNone(price_proxy(None, "close"))

    def test_implied_ratio_prefers_the_true_price(self):
        # 官方買進金額 = 0.6 x Σ 買進張數 x 收盤價 -> close 的隱含成數剛好 0.6、變異 0；其他代理不會更穩
        days = []
        for i, (c, v) in enumerate([(100, 98), (110, 118), (90, 85)]):
            m = {"X": [10, 0, 0, 0, 10]}
            p = {"X": [c, c + 5, c - 5, c, 1000, v * 1000]}
            days.append(day(f"2026-01-0{i + 2}", m, p, [10 * c * 0.6, 0, 0, 0, 0]))
        res = implied_ratio_by_proxy(days, kinds=("close", "vwap"))
        self.assertAlmostEqual(res["close"]["mean"], 0.6)
        self.assertAlmostEqual(res["close"]["cv"], 0.0, places=9)
        self.assertGreater(res["vwap"]["cv"], 0.01)


if __name__ == "__main__":
    unittest.main()
