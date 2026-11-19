"""展望図の仕上げ(2026-11-20 の決定)のテスト。通信しない(ソケットを作ると失敗にする)。
- 山頂の点のファイル summits.json の形と出典、表が無いとき・あるときの名前
- 山頂の雲量「はさむ 2 つの面の大きいほう」
- 雲の帯のまとめ・濃さの上限(既定)
- 名前の置き方(「広く」「標準」は重ならない分だけ・「拡大」は百名山と 76 座を全部)
- 保護領域に差分が無いこと(git と main があるときだけ)
予報・地形の値はどれも作った値。"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

import mountain_weather_core as core  # noqa: E402


def _no_socket(*a, **k):
    raise AssertionError("テストで通信しようとした")


class NoNetwork(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", _no_socket)
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        e = mock.patch.dict(os.environ, {"YAMABIYORI_PANORAMA_CACHE": os.path.join(self.tmp, "pano"),
                                         "YAMABIYORI_TERRAIN_CACHE": os.path.join(self.tmp, "terrain")})
        e.start()
        self.addCleanup(e.stop)


def load_summits_file():
    from panorama import names
    with open(names.summits_path(), encoding="utf-8") as fh:
        return json.load(fh)


class TestSummitsFile(NoNetwork):
    def test_shape_and_source(self):
        d = load_summits_file()
        keys = list(d)
        self.assertEqual(keys[:2], ["source", "processing"])            # 出典はファイルの先頭
        self.assertIn("「日本の主な山岳標高」(国土地理院)", d["source"])
        self.assertIn("加工", d["processing"])
        self.assertIn("日本百名山とヤマビヨリの 76 座", d["processing"])
        self.assertRegex(d["extracted"], r"^\d{4}-\d{2}-\d{2}$")
        rows = d["summits"]
        need = {"連番", "name", "yomi", "display", "lat", "lon", "ele_m", "kind", "pref", "tiers", "hyakumeizan_no",
                "mountain_ids"}
        for r in rows:
            self.assertTrue(need <= set(r), r)
            self.assertTrue(r["tiers"])
            self.assertTrue(set(r["tiers"]) <= {"百名山", "76座"})
            self.assertTrue(r["display"])
        # 百名山は 100 座すべて、1 座 1 点
        nos = [r["hyakumeizan_no"] for r in rows if r["hyakumeizan_no"] is not None]
        self.assertEqual(sorted(nos), list(range(1, 101)))
        # 76 座の ID は MOUNTAINS にあり、表に点の無い山(辞書の material が null)は入れない
        from panorama import names
        D = names.PeakDictionary()
        ids = [i for r in rows for i in r["mountain_ids"]]
        self.assertEqual(len(ids), len(set(ids)))
        all_ids = {m["id"] for m in core.MOUNTAINS}
        self.assertTrue(set(ids) <= all_ids)
        no_mat = {e["id"] for e in D.m76 if not e["material"]}
        self.assertFalse(no_mat & set(ids))
        self.assertEqual(set(ids) | no_mat, all_ids)
        self.assertEqual(d["counts"]["points"], len(rows))

    def test_readme_has_source(self):
        from panorama import config
        with open(os.path.join(config.DATA_DIR, "README.md"), encoding="utf-8") as fh:
            s = fh.read()
        self.assertIn("「日本の主な山岳標高」(国土地理院)をもとに作成", s)
        self.assertIn("summits.json", s)


class TestNamesWithoutAndWithTable(NoNetwork):
    def marks(self):
        from panorama import terrain
        ctx = terrain.build_ctx(None, terrain.TileLedger())
        return ctx

    def test_without_table_all_hyakumeizan_and_76_get_names(self):
        from panorama import names
        ctx = self.marks()
        self.assertTrue(ctx["summits_only"])
        D = names.PeakDictionary()
        D.attach(ctx["materials"])
        self.assertEqual(D.missing, {"hyakumeizan": [], "m76": []})
        tiers = [mk["tier"] for mk in ctx["marks"].values()]
        self.assertNotIn(None, tiers)                                   # そのほかの山は材料に無い
        self.assertEqual(sum(1 for t in tiers if t == "大"), 100)
        # 呼び名は summits.json に書いたもの
        disp = {f"gsi{r['連番']}": r["display"] for r in load_summits_file()["summits"]}
        for mid, lab in disp.items():
            self.assertEqual(ctx["marks"][mid]["display"], lab)
        # 表に点の無い 76 座は MOUNTAINS の座標で、格は 76 座
        mo = [m for m in ctx["materials"] if m["src"] == "MOUNTAINS"]
        self.assertTrue(mo)
        self.assertTrue(all(ctx["marks"][m["id"]]["tier"] == "中" for m in mo))

    def test_with_table_other_names_are_added(self):
        """作った「表」(summits.json の点 + 作った山 1 つ)を置くと、そのほかの山(格なし)が材料に足される。"""
        from panorama import config
        rows = load_summits_file()["summits"]
        feats = []
        for r in rows:
            feats.append({"type": "Feature", "properties": {
                "連番": r["連番"], "山名＜山頂名＞": r["name"], "山名よみ＜山頂名よみ＞": r["yomi"], "標高値(m)": r["ele_m"],
                "種別": r["kind"], "都道府県": r["pref"]}, "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]}})
        feats.append({"type": "Feature", "properties": {
            "連番": 99999, "山名＜山頂名＞": "見本山", "山名よみ＜山頂名よみ＞": "みほんやま", "標高値(m)": 1234,
            "種別": "三角点", "都道府県": "見本県"}, "geometry": {"type": "Point", "coordinates": [139.123, 35.123]}})
        os.makedirs(config.gsi_dir(), exist_ok=True)
        with open(os.path.join(config.gsi_dir(), "sample.geojson"), "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False)
        ctx = self.marks()
        self.assertFalse(ctx["summits_only"])
        self.assertIn("gsi99999", ctx["marks"])
        self.assertIsNone(ctx["marks"]["gsi99999"]["tier"])
        self.assertEqual(ctx["marks"]["gsi99999"]["display"], "見本山")
        self.assertEqual(sum(1 for mk in ctx["marks"].values() if mk["tier"] == "大"), 100)

    def test_credit_and_rough_summit_text(self):
        from panorama import draw, run
        line = " ".join(draw.credit_lines(False, gsi_partial=True))
        self.assertIn("「日本の主な山岳標高」(国土地理院)を加工して作成", line)
        self.assertIn("名前は日本百名山とヤマビヨリの 76 座です", line)
        self.assertNotIn("76 座です", " ".join(draw.credit_lines(False, gsi_partial=False)))
        ctx = self.marks()
        rough = [m for m in core.MOUNTAINS if not run.summit_viewer(m, ctx)["summit_point"]]
        self.assertEqual(len(rough), 11)
        self.assertIn("山頂の点は目安です", run.SUMMIT_ROUGH_TEXT)

    def test_parts_conditions_include_summits_version(self):
        from panorama import run
        c = run.parts_conditions({"lat": 35.0, "lon": 138.0, "kind": "summit"}, None)
        self.assertEqual(c["summits"], run.summits_version())
        self.assertNotEqual(c["summits"], "none")


def hourly_levels(pairs):
    """pairs: [(hPa, 高さ m, 雲量 %)] → 1 時刻の hourly。"""
    h = {"time": ["2026-01-02T08:00"]}
    for hpa, z, c in pairs:
        h[core.level_var("cloudcover", hpa)] = [c]
        h[core.level_var(core.GPH_KIND, hpa)] = [z]
    return h


class TestSummitCloudMax(unittest.TestCase):
    def test_larger_of_bracketing_levels(self):
        from panorama import weather
        h = hourly_levels([(850, 1500.0, 90.0), (700, 3100.0, 10.0), (600, 4300.0, 40.0)])
        self.assertEqual(weather.summit_cloud(h, 0, 2000.0), 90.0)     # 按分なら 65
        self.assertAlmostEqual(core.interp_at_altitude(h, "cloudcover", 0, 2000.0), 65.0)
        self.assertEqual(weather.summit_cloud(h, 0, 3500.0), 40.0)
        self.assertEqual(weather.summit_cloud(h, 0, 1000.0), 90.0)     # いちばん下より低い: 端の値
        self.assertEqual(weather.summit_cloud(h, 0, 5000.0), 40.0)     # いちばん上より高い: 端の値
        self.assertIsNone(weather.summit_cloud({"time": ["x"]}, 0, 2000.0))

    def test_core_is_not_changed(self):
        """本体の interp_at_altitude(診断の文・--json-out の雲量)は按分のまま。"""
        h = hourly_levels([(850, 1500.0, 100.0), (700, 3100.0, 0.0)])
        self.assertAlmostEqual(core.interp_at_altitude(h, "cloudcover", 0, 2300.0), 50.0)


class TestClumpUsesSummitCloud(unittest.TestCase):
    def test_clump_from_upper_bracket_takes_summit_value(self):
        from panorama import clouds
        lv = [(1500.0, 90.0, 850), (3100.0, 30.0, 700), (4300.0, 20.0, 600)]
        # 山頂 2000m: 下の面(1500m)は 300m より下なのでかたまりに入らず、かたまりは 700hPa から
        groups = clouds.clump_levels(lv, 2000.0, 10.0, None, [850, 700, 600])
        self.assertEqual(groups[0]["bottom"], 3100.0)
        g = clouds.with_summit_cloud(groups, lv, 2000.0, 90.0)
        self.assertEqual(g[0]["c"], 90.0)
        self.assertEqual(groups[0]["c"], 30.0)                          # 元は変えない
        # 山頂のすぐ上の面より上から始まるかたまり(山頂から離れた雲)は変えない
        lv2 = [(1500.0, 90.0, 850), (3100.0, 5.0, 700), (4300.0, 40.0, 600)]
        g2 = clouds.clump_levels(lv2, 2000.0, 10.0, None, [850, 700, 600])
        self.assertEqual(clouds.with_summit_cloud(g2, lv2, 2000.0, 90.0)[0]["c"], 40.0)
        self.assertEqual(clouds.with_summit_cloud(groups, lv, 2000.0, None), groups)


class TestBands(unittest.TestCase):
    def geom(self, y, alpha):
        return {"x0": 0.0, "x1": 8700.0, "upper": [(0.0, y), (8700.0, y)],
                "stops": [(0.0, alpha, alpha, alpha), (1.0, alpha, alpha, alpha)]}

    def test_default_on(self):
        from panorama import clouds
        self.assertTrue(clouds.OPTS["bands"])
        self.assertTrue(clouds.OPTS["clumps"])

    def test_merge_keeps_denser_and_cap(self):
        from panorama import clouds

        def X(a):
            return 20.0 + a * 24.0
        # 上の縁が 6px 以内の 2 本: うすいほうが 0 になり、濃いほうは残る
        b = [{"bi": 2, "geom": self.geom(100.0, 0.3), "wrap": None}, {"bi": 4, "geom": self.geom(104.0, 0.7), "wrap": None}]
        fac, n_m, _ = clouds.band_factors(b, X, 24.0, 8640.0)
        self.assertEqual(n_m, 360)
        self.assertTrue(np.allclose(fac[0], 0.0))
        self.assertTrue(np.allclose(fac[1], 1.0))
        # 離れた 3 本: 重なった濃さの合計を 0.85 までに
        b = [{"bi": 1, "geom": self.geom(100.0, 0.6), "wrap": None}, {"bi": 3, "geom": self.geom(130.0, 0.6), "wrap": None},
             {"bi": 5, "geom": self.geom(160.0, 0.6), "wrap": None}]
        fac, n_m, n_c = clouds.band_factors(b, X, 24.0, 8640.0)
        self.assertEqual(n_m, 0)
        self.assertEqual(n_c, 360)
        al = [0.6 * fac[k, 100] for k in range(3)]
        self.assertLessEqual(1.0 - np.prod([1.0 - a for a in al]), clouds.BAND_ALPHA_CAP + 1e-6)


def dense_items(n=40, seed=1):
    """作った名前の並び(図の x は 24px/度)。1°〜2° おきに山が並ぶ混んだ視点。"""
    rnd = np.random.default_rng(seed)
    out = []
    x = 20.0
    for i in range(n):
        x += float(rnd.uniform(10.0, 40.0))
        t = int(rnd.integers(0, 3))
        lab = "富士山" if i == 17 else f"見本{'山' * int(rnd.integers(1, 4))}{i}"
        out.append({"x": x, "y": 300.0, "tier": 0 if i == 17 else t, "texts": [lab, lab + " 雲 80%"], "label": lab,
                    "dist_km": float(rnd.uniform(5, 150)), "ele_m": float(rnd.uniform(800, 3000))})
    return out


class TestNameLayout(unittest.TestCase):
    def test_strict_zooms_have_no_overlap(self):
        from panorama import draw
        items = dense_items()
        for deg in (90, 45):
            for wb in draw.WIDTH_BUCKETS:
                for mx in (draw.MAX_ROWS, draw.LAND_MAX_ROWS):
                    L = draw.layout_names(items, deg, wb, max_rows=mx)
                    c = draw.count_conflicts(items, L)
                    self.assertEqual(c, {"name_name": 0, "name_lead": 0}, (deg, wb, mx))
                    fuji = next(i for i, it in enumerate(items) if it["label"] == "富士山")
                    self.assertGreaterEqual(L["rows"][fuji], 0)          # 富士山は必ず出す
        # 混んでいるので「標準」で名前を省いた山がある
        L = draw.layout_names(items, 45, 390)
        self.assertGreater(draw.missing_required(items, L), 0)

    def test_zoom_shows_all_required(self):
        from panorama import draw
        items = dense_items()
        for wb in draw.WIDTH_BUCKETS:
            for mx in (draw.MAX_ROWS, draw.LAND_MAX_ROWS):
                L = draw.layout_names(items, 20, wb, max_rows=mx)
                self.assertEqual(draw.missing_required(items, L), 0, (wb, mx))

    def test_order_hyakumeizan_then_76_then_others_near_first(self):
        from panorama import draw
        items = [{"x": 100.0, "tier": 1, "dist_km": 5.0, "ele_m": 1000.0, "label": "a", "texts": ["a"]},
                 {"x": 110.0, "tier": 0, "dist_km": 90.0, "ele_m": 2000.0, "label": "b", "texts": ["b"]},
                 {"x": 120.0, "tier": 0, "dist_km": 20.0, "ele_m": 1500.0, "label": "c", "texts": ["c"]},
                 {"x": 130.0, "tier": 0, "dist_km": 20.0, "ele_m": 2500.0, "label": "d", "texts": ["d"]},
                 {"x": 140.0, "tier": 0, "dist_km": 100.0, "ele_m": 3776.0, "label": "富士山", "texts": ["富士山"]},
                 {"x": 150.0, "tier": 2, "dist_km": 1.0, "ele_m": 900.0, "label": "e", "texts": ["e"]}]
        self.assertEqual([items[i]["label"] for i in draw.name_order(items)], ["富士山", "d", "c", "b", "a", "e"])

    def test_page_has_omit_note_and_free_height_zoom(self):
        from panorama import draw
        self.assertEqual(draw.OMIT_NOTE, "名前を省いた山があります。『拡大』で出ます")
        self.assertEqual(draw.FREE_HEIGHT_ZOOM, 20)
        self.assertIn("omitnote", draw.JS39)
        self.assertIn("free=land&&deg===D.freeZoom", draw.JS39)


class TestProtected(unittest.TestCase):
    PROTECTED = ("mountain_weather_core.py", "mountain_weather_mvp.py")

    def test_no_diff_from_main(self):
        try:
            p = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "main"], cwd=ROOT, capture_output=True,
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("git が無い")
        if p.returncode != 0:
            self.skipTest("main が無い")
        d = subprocess.run(["git", "diff", "--quiet", "main", "--"] + list(self.PROTECTED), cwd=ROOT, timeout=30)
        self.assertEqual(d.returncode, 0, "保護領域のファイルに main との差分がある")


if __name__ == "__main__":
    unittest.main()
