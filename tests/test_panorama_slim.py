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


if __name__ == "__main__":
    unittest.main()
