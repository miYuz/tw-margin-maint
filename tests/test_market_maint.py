# -*- coding: utf-8 -*-
"""market_maint.py 的手算驗證 (不連網)。答案都是人工算好的，不是拿程式輸出回填。
執行: python -m unittest discover -s tests -v   (在 margin_breadth 目錄)"""
import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_maint as mm  # noqa: E402

D = [pd.Timestamp(x) for x in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")]


def frame(dates, **cols):
    return pd.DataFrame({"date": dates, **cols})


class MergeTests(unittest.TestCase):
    def test_new_overrides_overlap_and_old_only_rows_are_kept(self):
        old = frame(D[:3], maint=[1.0, 2.0, 3.0])
        new = frame(D[1:], maint=[2.0, 3.5, 4.0])
        out, st = mm.merge(old, new, ["maint"], [0.05])
        self.assertEqual(out["maint"].tolist(), [1.0, 2.0, 3.5, 4.0])  # D1 only in old -> kept; D3 overridden by the newer pull
        self.assertEqual((st["old"], st["new"], st["overlap"], st["only_old"], st["merged"]), (3, 3, 2, 1, 4))
        self.assertEqual(st["changed"], 1)  # only D3 moved by more than the 0.05 tolerance
        self.assertEqual(st["changed_dates"], ["2026-01-07"])

    def test_rolling_window_never_loses_rows(self):
        # the feeds are rolling windows: today's pull no longer contains the oldest day, the archive must
        old = frame(D[:2], maint=[10.0, 11.0])
        new = frame(D[2:], maint=[12.0, 13.0])  # no overlap at all
        out, st = mm.merge(old, new, ["maint"], [0.05])
        self.assertEqual(len(out), 4)
        self.assertEqual(st["overlap"], 0)
        self.assertTrue(out["date"].is_monotonic_increasing and out["date"].is_unique)

    def test_empty_archive(self):
        out, st = mm.merge(pd.DataFrame(columns=["date"]), frame(D[:2], maint=[1.0, 2.0]), ["maint"], [0.05])
        self.assertEqual(len(out), 2)
        self.assertEqual(st["only_old"], 0)


class FetchWantgooTests(unittest.TestCase):
    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def rows(self, ratio, n=1000, base_ms=1631721600000):
        return [{"date": base_ms + i * 86400000, "lendingBalance": 1000 + i, "lastLendingBalance": 0, "marginOffset": 0, "limit": 0, "marginRatio": ratio} for i in range(n)]

    def test_date_is_taipei_trade_date_and_ratio_is_percent(self):
        # API date = trade date 00:00 Taipei expressed in UTC ms, i.e. 16:00 UTC of the previous day -> add 8 hours
        # 1631721600000 ms = 2021-09-15 16:00 UTC = 2021-09-16 00:00 Taipei
        with mock.patch.object(mm.requests, "get", return_value=self.Resp(self.rows(1.5))):
            df = mm.fetch_wantgoo("0000A")
        self.assertEqual(df["date"].min(), pd.Timestamp("2021-09-16"))
        self.assertEqual(df["date"].iloc[1], pd.Timestamp("2021-09-17"))
        self.assertTrue((df["maint"] == 150.0).all())
        self.assertEqual(df["margin_thousand"].iloc[0], 1000)

    def test_implausible_ratio_is_rejected(self):
        with mock.patch.object(mm.requests, "get", return_value=self.Resp(self.rows(0.5))):  # 50% is not a plausible maintenance ratio
            with self.assertRaises(RuntimeError):
                mm.fetch_wantgoo("0000A")

    def test_short_payload_is_rejected(self):
        with mock.patch.object(mm.requests, "get", return_value=self.Resp(self.rows(1.5, n=10))):
            with self.assertRaises(RuntimeError):
                mm.fetch_wantgoo("0000A")


class CalcTests(unittest.TestCase):
    def write_day(self, root, src, d, m, p, loan_k):
        os.makedirs(os.path.join(root, src), exist_ok=True)
        rec = {"d": d, "u": [0, 0, 0, 0, 0], "a": [0, 0, 0, loan_k, loan_k], "m": m, "p": p}
        with gzip.open(os.path.join(root, src, d.replace("-", "") + ".json.gz"), "wt", encoding="utf-8") as f:
            json.dump(rec, f)

    def test_market_value_over_loan_with_carry_forward_and_missing_price(self):
        with tempfile.TemporaryDirectory() as root:
            # day 1: A 10 張 @100, B 5 張 @50 -> mv = 10*1000*100 + 5*1000*50 = 1,250,000 ; loan = 800 仟元 = 800,000 -> 156.25%
            self.write_day(root, "twse", "2026-01-05", {"A": [0, 0, 0, 10, 10], "B": [0, 0, 0, 5, 5]}, {"A": [100, 100, 100, 100, 1, 1], "B": [50, 50, 50, 50, 1, 1]}, 800)
            # day 2: A did not trade (carry the 100 forward), B @60, C 2 張 has never had a price
            # mv = 10*1000*100 + 5*1000*60 = 1,300,000 ; 2 of the 17 張 have no price
            self.write_day(root, "twse", "2026-01-06", {"A": [0, 0, 0, 10, 10], "B": [0, 0, 0, 5, 5], "C": [0, 0, 0, 2, 2]}, {"B": [60, 60, 60, 60, 1, 1]}, 800)
            with mock.patch.object(mm, "RAW", root):
                out = mm.calc_one("twse")
        self.assertEqual(out["mv"].tolist(), [1250000.0, 1300000.0])
        self.assertEqual(out["loan"].tolist(), [800000.0, 800000.0])
        self.assertAlmostEqual(out["mv"].iloc[0] / out["loan"].iloc[0] * 100, 156.25)
        self.assertAlmostEqual(out["miss"].iloc[1], 2 / 17)
        self.assertEqual(out["miss"].iloc[0], 0.0)

    def test_unreadable_file_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_day(root, "tpex", "2026-01-05", {"A": [0, 0, 0, 1, 1]}, {"A": [10, 10, 10, 10, 1, 1]}, 10)
            with open(os.path.join(root, "tpex", "20260106.json.gz"), "wb") as f:
                f.write(b"not a gzip")  # a file the background fetcher is still writing
            with mock.patch.object(mm, "RAW", root):
                out = mm.calc_one("tpex")
        self.assertEqual(len(out), 1)


class BuildFrameTests(unittest.TestCase):
    def test_splice_priority_official_then_calc_then_wantgoo(self):
        with tempfile.TemporaryDirectory() as root:
            files = {k: os.path.join(root, k + ".csv") for k in ("listed", "otc", "official", "calc")}
            frame(D, margin_thousand=[1000] * 4, maint=[150.0, 160.0, 170.0, 180.0]).to_csv(files["listed"], index=False)
            frame(D, margin_thousand=[1000] * 4, maint=[100.0, 110.0, 120.0, 130.0]).to_csv(files["otc"], index=False)  # equal loans -> wg_all is the plain average
            frame(D[2:], keep_rate=[175.0, 185.0]).to_csv(files["official"], index=False)
            frame(D[1:3], calc_listed=[155.0, 165.0], calc_otc=[1.0, 1.0], calc_all=[140.0, 150.0]).to_csv(files["calc"], index=False)
            with mock.patch.object(mm, "FILES", files):
                df = mm.build_frame()
        self.assertEqual(df["wg_all"].tolist(), [125.0, 135.0, 145.0, 155.0])  # still computed, for comparison only
        # listed: D1 only wantgoo has it -> wantgoo; D2 recomputation beats wantgoo (155, not 160); D3-D4 official (recomputed 165 on D3 loses)
        self.assertEqual(df["maint_listed"].tolist(), [150.0, 155.0, 175.0, 185.0])
        self.assertEqual(df["source"].tolist(), ["wantgoo", "calc", "twse", "twse"])
        # all-market never falls back to wantgoo (its OTC leg is a day early since 2026-06): D1 has no value
        self.assertTrue(pd.isna(df["maint_all"].iloc[0]))
        self.assertEqual(df["maint_all"].iloc[1:].tolist(), [140.0, 175.0, 185.0])
        self.assertEqual(df["source_all"].tolist(), ["", "calc", "twse", "twse"])

    def test_without_calc_file_listed_falls_back_to_wantgoo_but_all_stays_empty(self):
        with tempfile.TemporaryDirectory() as root:
            files = {k: os.path.join(root, k + ".csv") for k in ("listed", "otc", "official", "calc")}  # calc file never written
            frame(D[:2], margin_thousand=[1000] * 2, maint=[150.0, 160.0]).to_csv(files["listed"], index=False)
            frame(D[:2], margin_thousand=[3000] * 2, maint=[110.0, 120.0]).to_csv(files["otc"], index=False)
            frame(D[1:2], keep_rate=[170.0]).to_csv(files["official"], index=False)
            with mock.patch.object(mm, "FILES", files):
                df = mm.build_frame()
        # loan-weighted: (150*1000 + 110*3000) / 4000 = 120
        self.assertAlmostEqual(df["wg_all"].iloc[0], 120.0)
        self.assertEqual(df["maint_listed"].tolist(), [150.0, 170.0])
        self.assertEqual(df["source"].tolist(), ["wantgoo", "twse"])
        self.assertTrue(pd.isna(df["maint_all"].iloc[0]) and df["maint_all"].iloc[1] == 170.0)
        self.assertEqual(df["source_all"].tolist(), ["", "twse"])
        self.assertTrue(df["calc_all"].isna().all())

    def test_calc_rows_before_wantgoo_start_are_kept(self):
        # the recomputation reaches back further than 玩股網 (2019 vs 2021-08): those early days must survive the merge
        with tempfile.TemporaryDirectory() as root:
            files = {k: os.path.join(root, k + ".csv") for k in ("listed", "otc", "official", "calc")}
            frame(D[2:], margin_thousand=[1000] * 2, maint=[170.0, 180.0]).to_csv(files["listed"], index=False)
            frame(D[2:], margin_thousand=[1000] * 2, maint=[120.0, 130.0]).to_csv(files["otc"], index=False)
            frame(D[3:], keep_rate=[185.0]).to_csv(files["official"], index=False)
            frame(D[:3], calc_listed=[151.0, 155.0, 171.0], calc_otc=[1.0] * 3, calc_all=[141.0, 145.0, 161.0]).to_csv(files["calc"], index=False)
            with mock.patch.object(mm, "FILES", files):
                df = mm.build_frame()
        self.assertEqual(len(df), 4)
        self.assertEqual(df["maint_listed"].tolist(), [151.0, 155.0, 171.0, 185.0])
        self.assertEqual(df["source"].tolist(), ["calc", "calc", "calc", "twse"])


if __name__ == "__main__":
    unittest.main()
