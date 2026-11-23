"""探索モードの同点の並び順(report_data.rank_key)と、同点の1行のテスト。通信しない。スコア・危険信号の計算には関わらない。"""
import contextlib
import io
import json
import os
import random
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mountain_weather_core as core  # noqa: E402
import mountain_weather_mvp as mvp  # noqa: E402
import report_data  # noqa: E402


def row(id_, score=98.6, wind=1.0, **hazards):
    h = {"thunder": "-", "wind": "-", "cold": "-", **hazards}
    return {"id": id_, "score": score, "ridge_wind_ms": wind, "hazards": h}


def order(rows):
    return [r["id"] for r in sorted(rows, key=report_data.rank_key)]


class TestRankKey(unittest.TestCase):
    def test_lighter_hazard_first(self):
        rows = [row("m001", thunder="危険"), row("m002", cold="注意"), row("m003"), row("m004", cold="警戒")]
        self.assertEqual(order(rows), ["m003", "m002", "m004", "m001"])

    def test_heaviest_level_then_count(self):
        rows = [row("m001", thunder="注意", cold="注意"), row("m002", cold="警戒"), row("m003", cold="注意")]
        self.assertEqual(order(rows), ["m003", "m001", "m002"])

    def test_same_hazard_weaker_wind_first_and_missing_wind_last(self):
        rows = [row("m001", wind=None), row("m002", wind=5.0), row("m003", wind=0.0), row("m004", wind=0.9)]
        self.assertEqual(order(rows), ["m003", "m004", "m002", "m001"])

    def test_then_id(self):
        self.assertEqual(order([row("m057"), row("m003"), row("m012")]), ["m003", "m012", "m057"])

    def test_different_scores_are_not_reordered(self):
        rows = [row("m001", 90.0), row("m002", 98.6, cold="危険"), row("m003", 98.5)]
        self.assertEqual(order(rows), ["m002", "m003", "m001"])

    def test_tie_is_by_shown_digit(self):
        """表示(小数1桁)が同じなら同点。内部の小さな差では並ばない。"""
        rows = [row("m001", 98.64, cold="危険"), row("m002", 98.61)]
        self.assertEqual(order(rows), ["m002", "m001"])

    def test_input_order_does_not_matter(self):
        rows = [row(f"m{i:03d}", wind=float(i % 3), cold=("注意" if i % 2 else "-")) for i in range(1, 30)]
        want = order(rows)
        rng = random.Random(1)
        for _ in range(5):
            rng.shuffle(rows)
            self.assertEqual(order(rows), want)


class TestTieNote(unittest.TestCase):
    def test_none_without_ties(self):
        self.assertIsNone(report_data.tie_note([]))
        self.assertIsNone(report_data.tie_note([row("m001", 98.6), row("m002", 98.5)]))

    def test_count(self):
        self.assertEqual(report_data.tie_note([row(f"m{i:03d}") for i in range(71)] + [row("m100", 90.0)]),
                         "※ 98.6点が 71 座あります。同点の中は、危険信号の軽い順・稜線の風が弱い順に並べています。")

    def test_largest_group_is_named(self):
        rows = [row("m001", 99.0), row("m002", 99.0)] + [row(f"m{i:03d}", 91.0) for i in range(10, 14)]
        self.assertIn("91.0点が 4 座あります(ほかにも同点の組が 1 組)", report_data.tie_note(rows))


class TestExploreOutput(unittest.TestCase):
    """本物の run_ranking を通す(予報は作った値)。山ごとに稜線の風だけを変える。"""

    def windows(self, wind_kmh, peak_ms):
        return {"activity": {"wet_fraction_pct": 0.0, "total_mm": 0.0, "wet_hours": 0, "total_hours": 12, "msm_hours": 12,
                             "moist_hours": 0, "prob_hours": 0, "terrain": None, "terrain_base": None},
                "ridge": {"cloud": 10.0, "cloud_raw": 10.0, "wind": wind_kmh, "temp": 12.0, "visibility": 30000.0},
                "pm": {"cape": 0.0, "cloud": 10.0, "cloud_raw": 10.0},
                "day_peak": {"wind_ms": peak_ms, "hour": 9}, "day_total_precip_mm": 0.0}

    def run_explore(self, pool):
        today = str(date.today())
        by_name = {"谷川岳": (20.0, 40.0), "高尾山": (4.0, 1.0), "大菩薩嶺": (2.0, 1.0)}   # (稜線の風 km/h, ピーク m/s)
        names = iter(m["name"] for m in pool)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(mvp, "MOUNTAINS", pool), \
                mock.patch.object(mvp, "fetch_forecast", side_effect=lambda *a, **k: next(names)), \
                mock.patch.object(mvp, "window_scores_by_day", side_effect=lambda n: {today: self.windows(*by_name[n])}):
            path = os.path.join(d, "e.json")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mvp.main(["--json-out", path]), 0)
            with open(path, encoding="utf-8") as f:
                return out.getvalue(), json.load(f)

    def setUp(self):
        self.pool = [m for m in core.MOUNTAINS if m["name"] in ("谷川岳", "高尾山", "大菩薩嶺")]
        self.assertEqual(len(self.pool), 3)

    def test_screen_and_json_order(self):
        out, data = self.run_explore(self.pool)
        day = data["days"][0]
        names = [r["mountain"].split("(")[0] for r in day["rows"]]
        self.assertEqual(len({r["score"] for r in day["rows"]}), 1)          # 同点
        self.assertEqual(names, ["大菩薩嶺", "高尾山", "谷川岳"])            # 信号なし(風の弱い順)→ 信号あり
        self.assertEqual([r["rank"] for r in day["rows"]], [1, 2, 3])
        self.assertEqual([a["id"] for a in day["all"]], [m["id"] for m in self.pool])   # all[] の並びは変えない
        self.assertIn("点が 3 座あります。同点の中は", day["tie_note"])
        self.assertEqual(out.count(day["tie_note"]), 1)
        self.assertLess(out.index("大菩薩嶺("), out.index("高尾山("))
        self.assertLess(out.index("高尾山("), out.index("谷川岳("))
        self.assertIsNone(data["days"][1]["tie_note"])

    def test_list_order_does_not_change_ties(self):
        _, a = self.run_explore(self.pool)
        _, b = self.run_explore(list(reversed(self.pool)))
        self.assertEqual(a["days"][0]["rows"], b["days"][0]["rows"])


if __name__ == "__main__":
    unittest.main()
