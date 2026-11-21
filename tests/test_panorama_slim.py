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


if __name__ == "__main__":
    unittest.main()
