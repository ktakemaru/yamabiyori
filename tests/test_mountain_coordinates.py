"""14 座の座標の修正(2026-11-18)の確かめ(通信しない)。

国土地理院「日本の主な山岳標高」(1003 山、2026-03-31 版)の山頂の点に直した 14 座の lat・lon が
その値であること、id・name・elevation_m・座の数が変わっていないことを確かめる。
経緯は docs/mountain-coordinates-fix-report.md と CHANGELOG。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mountain_weather_core as core  # noqa: E402

# id: (name, lat, lon, elevation_m)
FIXED_2026_11_18 = {
    "m039": ("唐松岳", 36.68723, 137.75469, 2696),
    "m050": ("金峰山", 35.87166, 138.62553, 2599),
    "m051": ("瑞牆山", 35.89345, 138.59209, 2230),
    "m053": ("榛名山(掃部ヶ岳)", 36.47740, 138.85089, 1449),
    "m043": ("霧ヶ峰(車山)", 36.10289, 138.19667, 1925),
    "m022": ("塔ノ岳", 35.45406, 139.16331, 1491),
    "m052": ("赤城山(黒檜山)", 36.56037, 139.19327, 1828),
    "m028": ("雲取山", 35.85551, 138.94391, 2017),
    "m038": ("常念岳", 36.32552, 137.72759, 2857),
    "m073": ("安達太良山", 37.62112, 140.28789, 1700),
    "m060": ("男体山", 36.76495, 139.49068, 2486),
    "m008": ("木曽駒ヶ岳", 35.78955, 137.80457, 2956),
    "m068": ("妙高山", 36.89126, 138.11346, 2454),
    "m002": ("八ヶ岳(赤岳)", 35.97080, 138.37007, 2899),
}


class FixedCoordinates(unittest.TestCase):
    def setUp(self):
        self.by_id = {m["id"]: m for m in core.MOUNTAINS}

    def test_count_unchanged(self):
        self.assertEqual(len(core.MOUNTAINS), 76)

    def test_fixed_values(self):
        for mid, (name, lat, lon, ele) in FIXED_2026_11_18.items():
            with self.subTest(mid=mid):
                m = self.by_id[mid]
                self.assertEqual(m["name"], name)
                self.assertEqual(m["lat"], lat)
                self.assertEqual(m["lon"], lon)
                self.assertEqual(m["elevation_m"], ele)

    def test_ids_and_names_match_fixture(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "mountain_ids.json"), encoding="utf-8") as fh:
            ids = json.load(fh)["ids"]
        self.assertEqual([(e["id"], e["name"]) for e in ids], [(m["id"], m["name"]) for m in core.MOUNTAINS])


if __name__ == "__main__":
    unittest.main()
