# -*- coding: utf-8 -*-
"""每日更新流程用到的小函式 (不連網)：update.py 的「各來源都有資料的最後一天」、estimator 的名稱存檔與合計日期、
make_dashboard 的官方維持率截斷/內插。答案都是人工算好的。
執行: python -m unittest discover -s tests -v   (在 margin_breadth 目錄)"""
import os
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import estimator as es  # noqa: E402
import make_dashboard as md  # noqa: E402
import update as up  # noqa: E402


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class LatestCommonDayTests(unittest.TestCase):
    def make_root(self, twse, tpex, official):
        root = tempfile.mkdtemp()
        for src, days in (("twse", twse), ("tpex", tpex)):
            for d in days:
                touch(os.path.join(root, "data", "raw", src, d.replace("-", "") + ".json.gz"))
        if official is not None:
            rows = "".join(f"{d},1,{150 + i},0\n" for i, d in enumerate(official))
            touch(os.path.join(root, "data", "market", "twse_keeprate.csv"), "date,crdAmt,keep_rate,belowAccNum\n" + rows)
        return root

    def test_min_of_the_three_sources(self):
        root = self.make_root(["2026-11-11", "2026-11-12"], ["2026-11-11", "2026-11-12", "2026-11-13"], ["2026-11-10", "2026-11-11", "2026-11-12"])
        self.assertEqual(up.last_raw_day("twse", root), "2026-11-12")
        self.assertEqual(up.last_raw_day("tpex", root), "2026-11-13")
        self.assertEqual(up.last_official_day(root), "2026-11-12")
        self.assertEqual(up.latest_common_day(root), "2026-11-12")

    def test_official_lagging_holds_everything_back_one_day(self):
        root = self.make_root(["2026-11-12", "2026-11-13"], ["2026-11-12", "2026-11-13"], ["2026-11-11", "2026-11-12"])
        self.assertEqual(up.latest_common_day(root), "2026-11-12")

    def test_empty_markers_do_not_count_as_data(self):
        root = self.make_root(["2026-11-12"], ["2026-11-12"], ["2026-11-12"])
        touch(os.path.join(root, "data", "raw", "twse", "20261113.empty"))  # holiday marker, not a data day
        self.assertEqual(up.last_raw_day("twse", root), "2026-11-12")
        self.assertEqual(up.latest_common_day(root), "2026-11-12")

    def test_missing_source_gives_none(self):
        root = self.make_root(["2026-11-12"], ["2026-11-12"], None)
        self.assertIsNone(up.latest_common_day(root))
        root = self.make_root([], ["2026-11-12"], ["2026-11-12"])
        self.assertIsNone(up.latest_common_day(root))


class NamesArchiveTests(unittest.TestCase):
    def test_roundtrip_and_chinese_names(self):
        path = os.path.join(tempfile.mkdtemp(), "data", "stock_names.csv")
        self.assertEqual(es.load_names(path), {})
        es.save_names({"2330": "台積電", "0050": "元大台灣50"}, path)
        self.assertEqual(es.load_names(path), {"2330": "台積電", "0050": "元大台灣50"})

    def test_merge_keeps_old_codes_and_new_name_overrides(self):
        path = os.path.join(tempfile.mkdtemp(), "stock_names.csv")
        es.save_names({"1101": "台泥", "9999": "舊公司"}, path)
        names = es.load_names(path)
        names.update({"1101": "台灣水泥", "2330": "台積電"})  # what cmd_final does with a fresh pull
        es.save_names(names, path)
        self.assertEqual(es.load_names(path), {"1101": "台灣水泥", "2330": "台積電", "9999": "舊公司"})


class CommonDaysTests(unittest.TestCase):
    def test_only_days_present_in_both_markets(self):
        tw = [{"d": "2026-11-11"}, {"d": "2026-11-12"}, {"d": "2026-11-13"}]
        tp = [{"d": "2026-11-11"}, {"d": "2026-11-12"}]
        a, b = es.common_days(tw, tp)
        self.assertEqual([r["d"] for r in a], ["2026-11-11", "2026-11-12"])
        self.assertEqual([r["d"] for r in b], ["2026-11-11", "2026-11-12"])

    def test_identical_calendars_are_unchanged(self):
        tw = [{"d": "2026-11-11", "x": 1}, {"d": "2026-11-12", "x": 2}]
        tp = [{"d": "2026-11-11", "x": 3}, {"d": "2026-11-12", "x": 4}]
        a, b = es.common_days(tw, tp)
        self.assertEqual((a, b), (tw, tp))


def official_frame(official):
    idx = pd.bdate_range("2026-11-02", periods=len(official))  # Mon..: 11-02 ..
    return pd.DataFrame({"official": official, "calc_listed": [150.0] * len(official)}, index=idx)


class TrimAndFillOfficialTests(unittest.TestCase):
    nan = float("nan")

    def test_no_official_at_all_is_untouched(self):
        m = official_frame([self.nan, self.nan, self.nan])
        out, filled = md.trim_and_fill_official(m)
        self.assertEqual(len(out), 3)
        self.assertEqual(filled, [])

    def test_days_before_official_starts_are_kept_as_is(self):
        m = official_frame([self.nan, self.nan, 170.0, 171.0])
        out, filled = md.trim_and_fill_official(m)
        self.assertEqual(len(out), 4)
        self.assertEqual(filled, [])
        self.assertTrue(out["official"].iloc[:2].isna().all())

    def test_trailing_days_without_official_are_dropped(self):
        m = official_frame([170.0, 171.0, 172.0, self.nan, self.nan])
        out, filled = md.trim_and_fill_official(m)
        self.assertEqual(len(out), 3)
        self.assertEqual(out.index[-1], pd.Timestamp("2026-11-04"))
        self.assertEqual(filled, [])

    def test_single_inner_gap_is_linearly_interpolated(self):
        m = official_frame([170.0, self.nan, 174.0, 175.0])
        out, filled = md.trim_and_fill_official(m)
        self.assertEqual(out["official"].tolist(), [170.0, 172.0, 174.0, 175.0])
        self.assertEqual(filled, ["2026-11-03"])

    def test_two_day_inner_gap(self):
        m = official_frame([170.0, self.nan, self.nan, 173.0])
        out, filled = md.trim_and_fill_official(m)
        self.assertEqual(out["official"].tolist(), [170.0, 171.0, 172.0, 173.0])
        self.assertEqual(len(filled), 2)

    def test_too_many_inner_gaps_raise(self):
        m = official_frame([170.0, self.nan, self.nan, self.nan, self.nan, 175.0])
        with self.assertRaises(RuntimeError):
            md.trim_and_fill_official(m, max_gap=3)

    def test_input_frame_is_not_modified(self):
        m = official_frame([170.0, self.nan, 174.0])
        md.trim_and_fill_official(m)
        self.assertTrue(pd.isna(m["official"].iloc[1]))


if __name__ == "__main__":
    unittest.main()
