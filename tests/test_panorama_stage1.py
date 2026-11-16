"""展望図の実証 段階1 の実行スクリプト(experiments/panorama/stage1_daibosatsu.py)のうち、通信しない部分のテスト。

国土地理院のデータは使わない(作った GeoJSON で確かめる)。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments", "panorama"))

import stage1_daibosatsu as s1  # noqa: E402


class TestLoadGsi(unittest.TestCase):
    def test_missing_data_stops_with_guidance(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                s1.load_gsi(d)
        msg = str(cm.exception.code)
        self.assertIn("ブラウザ", msg)
        self.assertIn("cache/panorama/gsi_1003/", msg)

    def test_reads_saved_geojson(self):
        feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [138.0, 36.0]},
                   "properties": {"連番": 1, "山名＜山頂名＞": "試験山＜試験峰＞", "山名よみ＜山頂名よみ＞": "しけんやま",
                                  "標高値(m)": "1234.5", "種別": "山", "都道府県": "山梨県"}}
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test.geojson"), "w", encoding="utf-8") as fh:
                json.dump({"type": "FeatureCollection", "features": [feature]}, fh, ensure_ascii=False)
            rows, name = s1.load_gsi(d)
        self.assertEqual(name, "test.geojson")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ele_m"], 1234.5)
        self.assertEqual((rows[0]["lat"], rows[0]["lon"]), (36.0, 138.0))


if __name__ == "__main__":
    unittest.main()
