"""山の変わらない ID(MOUNTAINS の "id"、2026-11-16)のテスト。ネットワークには出ない。

ID はほかのビヨリ・ビヨリコンシェルが山を指す名前(例: イキモノビヨリの "yamabiyori:m001")。
tests/fixtures/mountain_ids.json に固定した対応と MOUNTAINS が一致することを確かめ、並べ替え・番号の付け直し・
削除をしたら落ちるようにする。山を足すときは、その一覧の末尾に1行足す(AGENTS.md)。
"""
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

import mountain_weather_core as core
import mountain_weather_detail as detail
import mountain_weather_mvp as mvp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDS_FIXTURE = os.path.join(ROOT, "tests", "fixtures", "mountain_ids.json")
REPORT_FIX = os.path.join(ROOT, "tests", "fixtures", "report")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_report  # noqa: E402

ID_PATTERN = re.compile(r"^m\d{3}$")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_quietly(func, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args)
    return result, out.getvalue()


class TestMountainIds(unittest.TestCase):
    def test_every_mountain_has_id(self):
        for m in core.MOUNTAINS:
            self.assertIn("id", m, m["name"])
            self.assertIsInstance(m["id"], str, m["name"])

    def test_id_format(self):
        for m in core.MOUNTAINS:
            self.assertRegex(m["id"], ID_PATTERN, m["name"])

    def test_ids_unique(self):
        ids = [m["id"] for m in core.MOUNTAINS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ids_are_sequential_in_list_order(self):
        # 2026-11-16 の状態の確かめ(並び順のまま m001〜)。山を外して欠番ができたら、この確かめは見直す
        self.assertEqual([m["id"] for m in core.MOUNTAINS],
                         [f"m{i:03d}" for i in range(1, len(core.MOUNTAINS) + 1)])

    def test_matches_fixed_list(self):
        fixed = load(IDS_FIXTURE)["ids"]
        self.assertEqual([(e["id"], e["name"]) for e in fixed],
                         [(m["id"], m["name"]) for m in core.MOUNTAINS])

    def test_mountain_by_id(self):
        first, last = core.MOUNTAINS[0], core.MOUNTAINS[-1]
        self.assertIs(core.mountain_by_id(first["id"]), first)
        self.assertIs(core.mountain_by_id(last["id"]), last)
        for missing in ("m000", "m999", "u001", "", "M001", None, 1):
            with self.subTest(missing=missing):
                self.assertIsNone(core.mountain_by_id(missing))


class TestResolveById(unittest.TestCase):
    def test_resolve_by_id(self):
        m = core.MOUNTAINS[11]
        self.assertIs(detail.resolve_mountain(m["id"])[0], m)
        self.assertIs(detail.resolve_mountain(f"  {m['id']} ")[0], m)

    def test_number_and_name_unchanged(self):
        self.assertIs(detail.resolve_mountain("12")[0], core.MOUNTAINS[11])
        self.assertIs(detail.resolve_mountain(core.MOUNTAINS[11]["name"])[0], core.MOUNTAINS[11])
        mtn, candidates = detail.resolve_mountain("岳")
        self.assertIsNone(mtn)
        self.assertGreater(len(candidates), 1)

    def test_unknown_id_exits_2(self):
        code, out = run_quietly(detail.main, ["--mountain", "m999"])
        self.assertEqual(code, 2)
        self.assertIn("当てはまる山がありません", out)

    def test_list_output_has_no_ids(self):
        code, out = run_quietly(detail.main, ["--list"])
        self.assertEqual(code, 0)
        self.assertNotRegex(out, r"\bm\d{3}\b")


def _fake_windows():
    """window_scores_by_day() の1日分と同じ形の値(数値は適当。スコア計算は本物を通る)。"""
    return {
        "ridge": {"wind": 10.0, "temp": 5.0, "cloud": 20.0, "cloud_raw": 30.0, "visibility": 30000},
        "pm": {"cloud": 25.0, "cloud_raw": 35.0, "cape": 0.0},
        "activity": {"wet_fraction_pct": 0.0, "total_mm": 0.0, "wet_hours": 0.0, "total_hours": 10,
                     "msm_hours": 10, "moist_hours": 0, "prob_hours": 0, "terrain": None, "terrain_base": None},
        "day_peak": {"wind_ms": 4.0, "hour": "10:00"},
        "day_total_precip_mm": 0.0,
    }


class TestJsonOutIds(unittest.TestCase):
    def test_explore_json_has_ids(self):
        today = str(date.today())
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(mvp, "fetch_forecast", return_value={}), \
                mock.patch.object(mvp, "window_scores_by_day", return_value={today: _fake_windows()}):
            path = os.path.join(d, "e.json")
            code, out = run_quietly(mvp.main, ["--limit", "3", "--json-out", path])
            self.assertEqual(code, 0, out)
            data = load(path)
        self.assertNotRegex(out, r"\bm\d{3}\b")   # 画面には ID を出さない
        day0 = data["days"][0]
        expected = {f'{m["name"]}({m["elevation_m"]}m)': m["id"] for m in core.MOUNTAINS[:3]}
        self.assertEqual({r["mountain"]: r["id"] for r in day0["all"]}, expected)
        self.assertTrue(day0["rows"])
        for r in day0["rows"]:
            self.assertEqual(r["id"], expected[r["mountain"]])
        # 既存の項目は残り、並びも先頭は従来どおり(id は末尾に足しただけ)
        self.assertEqual(list(day0["rows"][0])[:4], ["rank", "mountain", "region", "access"])
        self.assertEqual(list(day0["rows"][0])[-1], "id")
        self.assertEqual(list(day0["all"][0]), ["mountain", "region", "score", "hazard_class", "id"])
        self.assertEqual(data["schema"], "yamabiyori-report-data/1")

    def test_diagnosis_report_has_id(self):
        m = core.MOUNTAINS[0]
        data = detail.build_report_data(m, {}, [], [], None, None)
        self.assertEqual(data["mountain"]["id"], m["id"])
        self.assertEqual(list(data["mountain"]), ["name", "elevation_m", "lat", "lon", "region", "access", "id"])

    def test_diagnosis_custom_mountain_id_is_null(self):
        args = detail.parse_args(["--name", "テスト山", "--lat", "38.9", "--lon", "140.7", "--elevation", "1600"])
        custom, err = detail.custom_mountain_from_args(args)
        self.assertIsNone(err)
        data = detail.build_report_data(custom, {}, [], [], None, None)
        self.assertIn("id", data["mountain"])
        self.assertIsNone(data["mountain"]["id"])
        self.assertIn('"id": null', json.dumps(data["mountain"], ensure_ascii=False))


class TestRendererIgnoresIds(unittest.TestCase):
    """render_report は id のある JSON でも無い JSON(既存の見本)でも同じ HTML を作る(id を表示しない)。"""

    def _render(self, data):
        with tempfile.TemporaryDirectory() as d:
            p, o = os.path.join(d, "data.json"), os.path.join(d, "o.html")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = render_report.main(["--data", p, "--out", o])
            self.assertEqual(code, 0, err.getvalue())
            with open(o, encoding="utf-8") as f:
                return f.read()

    def test_explore_html_unchanged_by_ids(self):
        base = load(os.path.join(REPORT_FIX, "explore.json"))
        with_ids = copy.deepcopy(base)
        for n, day in enumerate(with_ids["days"]):
            for i, r in enumerate(day["rows"]):
                r["id"] = f"m{i + 1:03d}"
            for i, r in enumerate(day["all"]):
                r["id"] = f"m{i + 1:03d}"
        self.assertEqual(self._render(base), self._render(with_ids))

    def test_diagnosis_html_unchanged_by_id(self):
        base = load(os.path.join(REPORT_FIX, "diagnosis.json"))
        for value in ("m012", None):
            with self.subTest(id=value):
                with_id = copy.deepcopy(base)
                with_id["mountain"]["id"] = value
                self.assertEqual(self._render(base), self._render(with_id))


if __name__ == "__main__":
    unittest.main()
