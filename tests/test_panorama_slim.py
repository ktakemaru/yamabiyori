"""展望図の HTML を軽くする書き方(図の形・色は変えない)のテスト。実通信はしない。"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from panorama import svgfmt  # noqa: E402


class TestNum(unittest.TestCase):
    def test_short_numbers(self):
        self.assertEqual(svgfmt.num(20.0), "20")
        self.assertEqual(svgfmt.num(0.5), ".5")
        self.assertEqual(svgfmt.num(-0.5), "-.5")
        self.assertEqual(svgfmt.num(-0.04), "0")
        self.assertEqual(svgfmt.num(12.34), "12.3")
        self.assertEqual(svgfmt.num(0.12345, 4), ".1235")
        self.assertEqual(svgfmt.num(1.0, 3), "1")
        self.assertEqual(svgfmt.num(-3.0, 0), "-3")

    def test_value_is_kept(self):
        for v in (0.0, 0.05, 0.15, 1.25, -2.35, 123.45, 8679.95):
            self.assertAlmostEqual(float(svgfmt.num(v)), float(f"{v:.1f}"))


class TestCircle(unittest.TestCase):
    def test_center_is_rounded_to_pixel(self):
        # 案 B: 雲のかたまりの丸は中心だけ 1 画素に丸める(半径は小数 1 桁のまま)
        self.assertEqual(svgfmt.circle(431.44, 189.9, 5.5), '<circle cx="431" cy="190" r="5.5"/>')
        self.assertEqual(svgfmt.circle(10.0, 20.0, 3.0), '<circle cx="10" cy="20" r="3"/>')

    def test_clump_uses_integer_centers(self):
        from panorama import clouds
        s = clouds.clump_svg37(100.3, 50.6, 60, 30, 20, 12345, "t", "x")
        for cx, cy in re.findall(r'<circle cx="([^"]+)" cy="([^"]+)"', s):
            self.assertRegex(cx, r"^-?\d+$")
            self.assertRegex(cy, r"^-?\d+$")


class TestPolygonPath(unittest.TestCase):
    """案 C: 地形の帯を相対座標の path にする。"""

    @staticmethod
    def absolute(d):
        """d(M・l・L・Z だけ)を絶対座標の点の列に戻す(0.1 単位の整数)。"""
        pts, cur, mode = [], None, None
        for cmd, body in re.findall(r"([MLlZ])([^MLlZ]*)", d):
            ns = [round(float(v) * 10) for v in re.findall(r"-?\d*\.?\d+", body)]
            for i in range(0, len(ns), 2):
                x, y = ns[i], ns[i + 1]
                cur = (cur[0] + x, cur[1] + y) if cmd == "l" else (x, y)
                pts.append(cur)
        return pts

    def test_same_points_except_collinear(self):
        pts = [(20.0, 288.8), (20.0, 164.6), (22.4, 165.5), (24.8, 166.4), (27.2, 167.3), (29.6, 160.04),
               (32.0, 160.0), (34.4, 160.0), (36.8, 160.0), (39.2, 288.8)]
        d = svgfmt.polygon_path(pts, sync=3)
        got = self.absolute(d)
        # 一直線の 22.4・24.8(傾き 0.9/2.4)と 32.0・34.4(水平)は落ち、ほかは元の点(小数 1 桁の書き方と同じ値)
        want = [(200, 2888), (200, 1646), (272, 1673), (296, 1600), (368, 1600), (392, 2888)]
        self.assertEqual(got, want)
        self.assertTrue(d.endswith("Z"))
        self.assertIn("L", d)   # 3 点ごとに絶対座標を挟む

    def test_every_kept_point_is_on_original(self):
        import math
        pts = [(20 + 2.4 * i, 200 + 30 * math.sin(i / 7.0)) for i in range(500)] + [(20 + 2.4 * 499, 300), (20, 300)]
        got = self.absolute(svgfmt.polygon_path(pts))
        orig = [(round(float(f"{x:.1f}") * 10), round(float(f"{y:.1f}") * 10)) for x, y in pts]
        self.assertTrue(set(got) <= set(orig))
        self.assertEqual(got[0], orig[0])
        self.assertEqual(got[-1], orig[-1])

    def test_figure_uses_paths_and_depth_regex(self):
        from panorama import draw, weather
        svg = '<svg><path d="M0 0l1 1Z" class="B1"/><path d="M0 0l1 1Z" class="near"/><path class="skyc" d="M1 1"/></svg>'
        self.assertEqual([m.group(1) for m in weather.POLY_RE.finditer(svg)], ["B1", "near"])
        out = draw.insert_by_depth(svg, [(1, "<g>雲</g>")], n_b=2)
        self.assertLess(out.index("<g>雲</g>"), out.index('class="B1"'))
        css = draw.style(3)
        self.assertIn("path.B1{fill:var(--B1)}", css)
        self.assertIn("path.near{fill:url(#hatch)}", css)
        self.assertNotIn("polygon", css)


class TestSkyMerge(unittest.TestCase):
    """案 D: 空のマスを横につなぐ(塗る所と色は同じ)。"""

    RUNS = [(0, 94.0, 120.0, "#a"), (0, 119.4, 289.0, "#b"),
            (1, 94.0, 120.0, "#a"), (1, 119.4, 289.0, "#c"),
            (2, 94.0, 120.0, "#a"), (2, 119.4, 289.0, "#b"),
            (4, 94.0, 120.0, "#a")]

    def test_merge_keeps_area_and_color(self):
        from panorama import draw
        cells = draw.merge_sky_cells(self.RUNS)
        self.assertIn((0, 3, 94.0, 120.0, "#a"), cells)      # 0〜2° は 1 つに
        self.assertIn((4, 1, 94.0, 120.0, "#a"), cells)      # 3° が空くので別
        self.assertIn((0, 1, 119.4, 289.0, "#b"), cells)     # 色が違う所はつながない
        self.assertIn((2, 1, 119.4, 289.0, "#b"), cells)
        # 方位ごとの色が元と同じ
        covered = {}
        for a, n, y0, y1, col in cells:
            for k in range(a, a + n):
                covered[(k, round(y0, 1))] = col
        self.assertEqual(covered, {(a, round(y0, 1)): col for a, y0, y1, col in self.RUNS})

    def test_path_is_one_rect_for_merged_run(self):
        from panorama import draw
        s = draw.sky_paths(self.RUNS, lambda a: 20 + a * 24, 24)
        self.assertIn("M20.0 94.0h72.6v26.0h-72.6z", s)      # 3 マス: 幅 3×24 + 重なり 0.6


class TestStops(unittest.TestCase):
    """案 E: グラデーションの書き方を短くする(値の桁と塗りは同じ)。"""

    def test_short_and_same_values(self):
        s = svgfmt.stops([(0.0, 0.0), (0.0064, 0.0), (0.0179, 0.0), (0.03, 0.25), (0.5, 0.4), (1.0, 1.0)])
        self.assertEqual(s, '<stop offset="0" stop-opacity="0"/><stop offset=".0179" stop-opacity="0"/>'
                            '<stop offset=".03" stop-opacity=".25"/><stop offset=".5" stop-opacity=".4"/>'
                            '<stop offset="1" stop-opacity="1"/>')
        self.assertNotIn("stop-color", s)

    def test_only_inner_equal_stops_are_dropped(self):
        import random
        rnd = random.Random(3)
        pairs = sorted((rnd.random(), rnd.choice([0.0, 0.2, 0.2, 0.5])) for _ in range(200))
        got = [(float(o), float(a)) for o, a in re.findall(r'offset="([\d.]+)" stop-opacity="([\d.]+)"', svgfmt.stops(pairs))]
        want = [(float(f"{o:.4f}"), float(f"{a:.3f}")) for o, a in pairs]

        def value(stops, x):
            for (o0, a0), (o1, a1) in zip(stops, stops[1:]):
                if o0 <= x <= o1:
                    return a0 if o1 == o0 else a0 + (a1 - a0) * (x - o0) / (o1 - o0)
            return stops[-1][1]
        self.assertEqual(got[0], want[0])
        self.assertEqual(got[-1], want[-1])
        self.assertLess(len(got), len(want))
        for o, _ in want:
            self.assertAlmostEqual(value(got, o), value(want, o), places=9)

    def test_band_gradient_and_css(self):
        from panorama import clouds, draw
        geom = {"kind": "sea", "x0": 10.0, "x1": 110.0, "y_bottom": 60.0, "upper": [(10.0, 40.0), (60.0, 38.0), (110.0, 40.0)],
                "shadow_upper": [(10.0, 50.0), (110.0, 50.0)], "thin_upper": None,
                "stops": [(0.0, 0.0, 0.0, 0.0), (0.5, 0.6, 0.0, 0.6), (1.0, 0.0, 0.0, 0.0)]}
        s = clouds.band_svg37(geom, "b1")
        self.assertIn('<linearGradient id="mgb1" gradientUnits="userSpaceOnUse" x1="10.0" x2="110.0">', s)
        self.assertIn('<stop offset=".5" stop-opacity=".6"/>', s)
        self.assertIn("stop{stop-color:#fff}", draw.style(3))


class TestPathSeparators(unittest.TestCase):
    """雲の輪郭(clouds.path_d)の数の区切りを省く。点は同じ。"""

    def test_separators(self):
        self.assertEqual(svgfmt.join_nums(["1.1", "-.2", ".3", ".4", "5", ".6", "7"]), "1.1-.2.3.4 5 .6 7")

    def test_same_points(self):
        import math
        from panorama import clouds
        pts = [(100 + i * 1.13, 50 + 0.4 * math.sin(i)) for i in range(300)] + [(440, 60), (100, 60)]
        d = clouds.path_d(pts)
        m = re.match(r"M([-\d.]+),([-\d.]+)l(.*)Z$", d)
        nums = re.findall(r"-?\d*\.\d+|-?\d+", m.group(3))
        x, y = round(float(m.group(1)) * 10), round(float(m.group(2)) * 10)
        got = [(x, y)]
        for i in range(0, len(nums), 2):
            x += round(float(nums[i]) * 10)
            y += round(float(nums[i + 1]) * 10)
            got.append((x, y))
        want = []
        for p in [(round(a * 10), round(b * 10)) for a, b in pts]:
            if not want or want[-1] != p:
                want.append(p)
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
