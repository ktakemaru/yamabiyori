"""展望図の実証 段階1 の計算部品(experiments/panorama/geometry.py)のテスト。通信しない。

座標は作った値(実在の撮影地点ではない)。
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments", "panorama"))

import geometry as g  # noqa: E402
from mountain_terrain import vincenty_direct  # noqa: E402

LAT0, LON0 = 36.0, 138.0


def flat(h=0.0):
    return lambda lat, lon: h


def with_bump(base, lat_c, lon_c, height, radius_m):
    """base の上に、中心 (lat_c, lon_c) で高さ height・半径 radius_m の円錐の山を足す。"""
    def fn(lat, lon):
        d, _ = g.vincenty_inverse(lat_c, lon_c, lat, lon)
        extra = max(0.0, height * (1 - d / radius_m))
        return base(lat, lon) + extra
    return fn


class TestBearingDistance(unittest.TestCase):
    def test_due_north(self):
        lat2, lon2 = vincenty_direct(LAT0, LON0, 0.0, 10000.0)
        d, az = g.vincenty_inverse(LAT0, LON0, lat2, lon2)
        self.assertAlmostEqual(d, 10000.0, delta=0.01)
        self.assertAlmostEqual(g.angle_diff_deg(az, 0.0), 0.0, delta=1e-6)

    def test_due_east_and_roundtrip(self):
        for az_in, dist in ((90.0, 50000.0), (237.3, 123456.0)):
            lat2, lon2 = vincenty_direct(LAT0, LON0, az_in, dist)
            d, az = g.vincenty_inverse(LAT0, LON0, lat2, lon2)
            self.assertAlmostEqual(d, dist, delta=0.01)
            self.assertAlmostEqual(az, az_in, delta=1e-6)

    def test_one_degree_latitude(self):
        # 子午線上の緯度1度(35.5〜36.5度)は約 110.95 km
        d, az = g.vincenty_inverse(35.5, LON0, 36.5, LON0)
        self.assertAlmostEqual(d / 1000.0, 110.95, delta=0.05)
        self.assertAlmostEqual(az, 0.0, delta=1e-9)


class TestApparentAngle(unittest.TestCase):
    def test_curvature_and_refraction(self):
        v = {"lat": LAT0, "lon": LON0, "ele_m": 1000.0}
        lat2, lon2 = vincenty_direct(LAT0, LON0, 45.0, 100000.0)
        t = {"lat": lat2, "lon": lon2, "ele_m": 1000.0 + g.EYE_HEIGHT_M}
        no_ref = g.target_visibility(v, t, flat(0.0), k=0.0)
        with_ref = g.target_visibility(v, t, flat(0.0))
        self.assertLess(no_ref["summit_angle_deg"], 0.0)          # 同じ高さでも水平より下
        self.assertLess(with_ref["summit_angle_deg"], 0.0)
        self.assertGreater(with_ref["summit_angle_deg"], no_ref["summit_angle_deg"])   # 屈折で少し上がる
        # 100km での見かけの下がり: 地球の丸みだけなら d/(2R) ≈ 0.45°
        self.assertAlmostEqual(no_ref["summit_angle_deg"], -math.degrees(100000.0 / (2 * 6371000.0)), delta=0.01)


class TestMargin(unittest.TestCase):
    def setUp(self):
        self.v = {"lat": LAT0, "lon": LON0, "ele_m": 1500.0}
        lat2, lon2 = vincenty_direct(LAT0, LON0, 200.0, 60000.0)
        self.t = {"lat": lat2, "lon": lon2, "ele_m": 3000.0}
        self.wall_lat, self.wall_lon = vincenty_direct(LAT0, LON0, 200.0, 10000.0)

    def test_flat_ground_visible(self):
        r = g.target_visibility(self.v, self.t, flat(500.0))
        self.assertGreater(r["margin_deg"], 0.0)
        self.assertTrue(r["visible"])
        self.assertAlmostEqual(r["azimuth_deg"], 200.0, delta=1e-6)
        self.assertAlmostEqual(r["distance_km"], 60.0, delta=1e-4)

    def test_high_wall_hides_then_low_wall_reveals(self):
        high = with_bump(flat(500.0), self.wall_lat, self.wall_lon, 2000.0, 1500.0)   # 2500m の壁(10km 先)
        r = g.target_visibility(self.v, self.t, high)
        self.assertLess(r["margin_deg"], 0.0)
        self.assertFalse(r["visible"])
        self.assertAlmostEqual(r["max_terrain_at_km"], 10.0, delta=0.3)
        low = with_bump(flat(500.0), self.wall_lat, self.wall_lon, 1200.0, 1500.0)    # 1700m の壁
        r2 = g.target_visibility(self.v, self.t, low)
        self.assertGreater(r2["margin_deg"], 0.0)

    def test_missing_elevation_counted(self):
        r = g.target_visibility(self.v, self.t, lambda lat, lon: None)
        self.assertIsNone(r["margin_deg"])
        self.assertGreater(r["missing_samples"], 0)


class TestSkyline(unittest.TestCase):
    def test_single_peak_direction(self):
        v = {"lat": LAT0, "lon": LON0, "ele_m": 100.0}
        plat, plon = vincenty_direct(LAT0, LON0, 230.0, 20000.0)
        terrain = with_bump(flat(0.0), plat, plon, 2000.0, 3000.0)
        sky = g.skyline(v, 220.0, 240.0, 1.0, 40000.0, terrain)
        best = max(sky, key=lambda s: s["angle_deg"])
        self.assertAlmostEqual(best["azimuth_deg"], 230.0, delta=1e-9)
        self.assertAlmostEqual(best["distance_km"], 20.0, delta=0.25)
        expect = g.apparent_elevation_angle_deg(100.0 + g.EYE_HEIGHT_M, 2000.0, 20000.0)
        self.assertAlmostEqual(best["angle_deg"], expect, delta=0.05)
        # 山から離れた方位は平地(水平より下)
        self.assertLess(sky[0]["angle_deg"], 0.0)


class TestCameraFit(unittest.TestCase):
    def test_recovers_parameters(self):
        cx, f, az0 = 812.0, 1350.0, 247.5
        az = [215.0, 222.0, 230.5, 238.0, 246.0, 251.0, 259.0, 266.0, 274.0, 279.0]
        x = g.project_x(az, cx, f, az0)
        fit = g.fit_camera(az, x)
        self.assertAlmostEqual(fit["cx"], cx, delta=0.5)
        self.assertAlmostEqual(fit["f"], f, delta=0.5)
        self.assertAlmostEqual(fit["az0_deg"], az0, delta=0.01)
        self.assertLess(fit["rms_px"], 1e-6)
        fixed = g.fit_camera(az, x, cx_fixed=cx)
        self.assertAlmostEqual(fixed["f"], f, delta=0.5)
        self.assertAlmostEqual(fixed["az0_deg"], az0, delta=0.01)
        self.assertAlmostEqual(g.horizontal_fov_deg(1600 / 2 / math.tan(math.radians(30)), 1600), 60.0, delta=1e-9)

    def test_linear_and_order(self):
        lin = g.fit_linear([10.0, 20.0, 30.0], [100.0, 200.0, 300.0])
        self.assertAlmostEqual(lin["b_px_per_deg"], 10.0, delta=1e-9)
        mism = g.order_mismatches([("A", 100, 10.0), ("B", 200, 25.0), ("C", 300, 20.0)])
        self.assertEqual([(m[0], m[1]) for m in mism], [("B", "C")])
        # 0度をまたぐ並びは逆転と数えない
        self.assertEqual(g.order_mismatches([("A", 100, 355.0), ("B", 200, 5.0)]), [])


if __name__ == "__main__":
    unittest.main()
