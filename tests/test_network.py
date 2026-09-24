"""Open-Meteo に実際にアクセスする smoke テスト。既定ではスキップされる。

実行: 環境変数 YAMABIYORI_NETWORK_TESTS=1 を付けて unittest / pytest を実行する。
  PowerShell: $env:YAMABIYORI_NETWORK_TESTS="1"; ./venv/Scripts/python.exe -X utf8 -m unittest tests.test_network
  bash:       YAMABIYORI_NETWORK_TESTS=1 ./venv/bin/python -X utf8 -m unittest tests.test_network
失敗しても、Open-Meteo 側の一時的な障害やネットワーク制限の可能性がある(コードの不具合とは限らない)。
キャッシュは一時ディレクトリに向けるので、リポジトリの cache/ は読み書きしない。
"""
import os
import tempfile
import unittest
from unittest import mock

import mountain_terrain as terrain
import mountain_weather_core as core
import mountain_weather_detail as detail

ENABLED = os.environ.get("YAMABIYORI_NETWORK_TESTS") == "1"


@unittest.skipUnless(ENABLED, "YAMABIYORI_NETWORK_TESTS=1 のときだけ実行")
class TestOpenMeteoSmoke(unittest.TestCase):
    def test_one_mountain_end_to_end(self):
        mtn = core.MOUNTAINS[0]
        summit_m, wind_var, _ = detail.wind_vars_for_elevation(mtn["elevation_m"])
        temp_var = detail.temp_var_for_elevation(mtn["elevation_m"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(core, "CACHE_DIR", tmp):
            forecast = detail.fetch_forecast(mtn["lat"], mtn["lon"], summit_m=summit_m,
                                             terrain_profile=terrain.terrain_for(mtn["name"]))
            self.assertTrue(os.listdir(tmp), "キャッシュが一時ディレクトリに書かれていない")
        self.assertIn("time", forecast["hourly"])
        self.assertTrue(forecast["hourly"]["time"])
        day_scores = detail.compute_day_scores(forecast, wind_var, summit_m, temp_var)
        self.assertGreaterEqual(len(day_scores), 3)
        for day, row in day_scores.items():
            self.assertTrue(0.0 <= row["score"] <= 100.0, (day, row["score"]))


if __name__ == "__main__":
    unittest.main()
