"""オフラインで動くテスト(ネットワーク不要、既定で実行される)。

実行: python -X utf8 -m unittest        (リポジトリのルートで。pytest でも可)
スコアリングのロジックは変更せず、現行の出力を期待値として固定している。期待値が変わったら、
それはスコアが変わったということ -- CHANGELOG に理由を書いたうえで期待値を更新すること。
"""
import codecs
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import requests

import mountain_terrain as terrain
import mountain_weather_core as core
import mountain_weather_detail as detail
import mountain_weather_mvp as mvp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED_KEYS = {"name": str, "elevation_m": (int, float), "lat": float, "lon": float, "access": str, "region": str}


def run_quietly(func, *args):
    """func(*args) の戻り値と標準出力を返す。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args)
    return result, out.getvalue()


class TestImports(unittest.TestCase):
    def test_modules_import(self):
        for mod in (core, detail, mvp, terrain):
            self.assertTrue(hasattr(mod, "__name__"))
        self.assertTrue(callable(core.mountain_climb_score))
        self.assertTrue(callable(detail.main))
        self.assertTrue(callable(mvp.main))


class TestMountains(unittest.TestCase):
    def test_required_keys_and_types(self):
        for m in core.MOUNTAINS:
            for key, typ in REQUIRED_KEYS.items():
                self.assertIn(key, m, m.get("name"))
                self.assertIsInstance(m[key], typ, (m.get("name"), key))
            self.assertTrue(30 < m["lat"] < 46 and 128 < m["lon"] < 146, m["name"])   # 日本の範囲
            self.assertTrue(0 < m["elevation_m"] < 4000, m["name"])

    def test_names_unique(self):
        names = [m["name"] for m in core.MOUNTAINS]
        self.assertEqual(len(names), len(set(names)))

    def test_count_matches_terrain_profiles(self):
        # 山数は固定値で書かない: 地形の事前計算 (terrain_profiles.json) と揃っていることを確かめる。
        # 山を追加したら `python precompute_terrain.py 山名` を実行すること(AGENTS.md 参照)。
        with open(os.path.join(ROOT, "terrain_profiles.json"), encoding="utf-8") as f:
            profiles = json.load(f)["profiles"]
        self.assertEqual({m["name"] for m in core.MOUNTAINS}, set(profiles))


class TestScoreFormula(unittest.TestCase):
    # (cloud_pct, ridge_visibility_m, precip_wet_pct, precip_mm) -> 現行の出力 (v1.6.1 時点)
    CASES = [
        ((0, 40000, 0, 0), 100.0),
        ((30, 20000, 20, 2), 81.8),
        ((60, 5000, 50, 8), 56.2),
        ((100, 500, 100, 30), 0.0),
        ((10, None, 0, 0), 97.4),
    ]

    def score(self, cloud, vis, wet, mm):
        return core.mountain_climb_score(cloud_pct=cloud, ridge_visibility_m=vis, precip_wet_pct=wet, precip_mm=mm)

    def test_representative_values(self):
        for args, expected in self.CASES:
            with self.subTest(args=args):
                self.assertEqual(self.score(*args), expected)

    def test_range(self):
        for cloud in (0, 25, 50, 75, 100):
            for wet in (0, 30, 60, 100):
                s = self.score(cloud, 10000, wet, wet / 5)
                self.assertTrue(0.0 <= s <= 100.0, (cloud, wet, s))

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(core.SCORE_WEIGHTS.values()), 1.0)


class TestCloudCalibrationTable(unittest.TestCase):
    def test_shape(self):
        n_bins = len(core.CLOUD_CALIBRATION_BIN_EDGES)
        self.assertTrue(core.CLOUD_CALIBRATION_TABLE)
        for row in core.CLOUD_CALIBRATION_TABLE:
            with self.subTest(row=row[:3]):
                self.assertEqual(len(row), 5)            # (model, lead_lo, lead_hi, bins, P_ref)
                model, lo, hi, bins, p_ref = row
                self.assertIn(model, ("ecmwf_ifs025", "jma_msm"))
                self.assertLessEqual(lo, hi)
                self.assertEqual(len(bins), n_bins)
                self.assertTrue(all(0.0 <= p <= 1.0 for p in bins))
                self.assertTrue(0.0 < p_ref <= 1.0)

    def test_every_model_has_lead_1(self):
        for model in ("ecmwf_ifs025", "jma_msm"):
            self.assertTrue(any(r[0] == model and r[1] <= 1 <= r[2] for r in core.CLOUD_CALIBRATION_TABLE), model)


class TestDetailCli(unittest.TestCase):
    """診断モードの引数処理 (--list / --mountain)。ネットワークには出ない。"""

    def test_resolve_by_number_and_name(self):
        first = core.MOUNTAINS[0]
        self.assertIs(detail.resolve_mountain("1")[0], first)
        self.assertIs(detail.resolve_mountain(first["name"])[0], first)
        self.assertIsNone(detail.resolve_mountain(str(len(core.MOUNTAINS) + 1))[0])
        self.assertIsNone(detail.resolve_mountain("0")[0])

    def test_ambiguous_name_lists_candidates(self):
        mtn, candidates = detail.resolve_mountain("岳")
        self.assertIsNone(mtn)
        self.assertGreater(len(candidates), 1)

    def test_list_exits_0(self):
        code, out = run_quietly(detail.main, ["--list"])
        self.assertEqual(code, 0)
        self.assertIn(core.MOUNTAINS[0]["name"], out)

    def test_unknown_mountain_exits_2(self):
        code, _ = run_quietly(detail.main, ["--mountain", "存在しない山"])
        self.assertEqual(code, 2)

    def test_fetch_failure_exits_2(self):
        with mock.patch.object(detail, "fetch_forecast", side_effect=requests.exceptions.ConnectionError("offline")):
            code, out = run_quietly(detail.main, ["--mountain", "1"])
        self.assertEqual(code, 2)
        self.assertIn("取得に失敗しました", out)


class TestMvpCli(unittest.TestCase):
    """探索モードの山の選別 (--region / --limit)。ネットワークには出ない。"""

    def test_select_pool(self):
        pool, err = mvp.select_pool([], 3)
        self.assertIsNone(err)
        self.assertEqual(pool, core.MOUNTAINS[:3])
        region = core.MOUNTAINS[0]["region"]
        pool, err = mvp.select_pool([region], None)
        self.assertIsNone(err)
        self.assertTrue(pool and all(m["region"] == region for m in pool))

    def test_bad_arguments_exit_2(self):
        for argv in (["--region", "存在しない地域"], ["--limit", "0"]):
            with self.subTest(argv=argv):
                code, _ = run_quietly(mvp.main, argv)
                self.assertEqual(code, 2)

    def test_all_fetches_failed_exits_2(self):
        with mock.patch.object(mvp, "fetch_forecast", side_effect=requests.exceptions.ConnectionError("offline")):
            code, out = run_quietly(mvp.main, ["--limit", "2"])
        self.assertEqual(code, 2)
        self.assertIn("取得失敗", out)
        self.assertNotIn("【対象地域:", out)


class TestOutFile(unittest.TestCase):
    """--out: 出力を UTF-8(BOM付き)でファイルに保存し、画面には1行だけ出す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "out.txt")

    def read_out(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8), "BOM 付き UTF-8 ではない")
        return raw.decode("utf-8-sig")

    def test_detail_list_to_file(self):
        code, screen = run_quietly(detail.main, ["--list", "--out", self.path])
        self.assertEqual(code, 0)
        self.assertIn(core.MOUNTAINS[0]["name"], self.read_out())
        self.assertEqual(len(screen.strip().splitlines()), 1)
        self.assertTrue(screen.startswith("[OK]"))

    def test_detail_interactive_rejects_out(self):
        code, screen = run_quietly(detail.main, ["--out", self.path])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("--out", screen)

    def test_mvp_failure_to_file(self):
        with mock.patch.object(mvp, "fetch_forecast", side_effect=requests.exceptions.ConnectionError("offline")):
            code, screen = run_quietly(mvp.main, ["--limit", "2", "--out", self.path])
        self.assertEqual(code, 2)
        self.assertIn("取得失敗", self.read_out())
        self.assertEqual(len(screen.strip().splitlines()), 1)
        self.assertTrue(screen.startswith("[NG]"))

    def test_mvp_bad_args_not_written(self):
        code, screen = run_quietly(mvp.main, ["--limit", "0", "--out", self.path])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("--limit", screen)


if __name__ == "__main__":
    unittest.main()
