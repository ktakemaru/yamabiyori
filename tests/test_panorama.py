"""診断モードの展望図(--panorama・panorama/)のテスト。通信しない(ソケットを作ると失敗にする)。
山・地形・予報はどれも作った値(座標は小数 3 桁まで。MOUNTAINS の値は前からあるもの)。"""
import contextlib
import io
import json
import os
import pickle
import re
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

import mountain_weather_core as core  # noqa: E402

DAY = "2026-01-02"


def _no_socket(*a, **k):
    raise AssertionError("テストで通信しようとした")


class NoNetwork(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", _no_socket)
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        e = mock.patch.dict(os.environ, {"YAMABIYORI_PANORAMA_CACHE": os.path.join(self.tmp, "pano"),
                                         "YAMABIYORI_TERRAIN_CACHE": os.path.join(self.tmp, "terrain")})
        e.start()
        self.addCleanup(e.stop)


def fake_parts():
    """作った図の地形(視点・輪郭・山 4 つ)。"""
    from panorama import terrain
    az = np.round(np.arange(0.0, 360.01, 0.1), 4)
    n_b = len(terrain.DEFAULT_BANDS)
    ang = np.stack([-1.5 + 0.12 * b + 0.25 * np.sin(np.radians(az * (b + 2))) for b in range(n_b)], axis=1)
    sky = {"az": az, "ang": ang, "far_top": ang[:, -1].copy()}
    peaks, points = [], {}
    for k, (az0, tier, nm) in enumerate(((40.0, "大", "東の山"), (130.0, "中", "南の山"), (250.0, "大", "富士山"),
                                         (300.0, "小", "北の小山"))):
        rid = f"r{k}"
        peaks.append({"row_id": rid, "label": nm, "tier": tier, "az": az0, "draw_angle": 0.35, "prio": 1.0,
                      "marginal": False, "title": nm, "band": 6, "ridge": None, "dist_km": 60.0 + 10 * k})
        if tier in ("大", "中"):
            points[rid] = {"lat": 35.0 + 0.1 * k, "lon": 138.0 + 0.1 * k, "elev_m": 2600.0, "source": "MOUNTAINS"}
    v = {"kind": "summit", "id": "m999", "name": "見本の山", "lat": 35.5, "lon": 138.5, "source": "summit_gsi",
         "osm_used": False}
    return {"sky": sky, "peaks": peaks, "points": points, "v": v, "viewer": {"lat": 35.5, "lon": 138.5, "ele_m": 2000.0},
            "rows": []}


def fake_response(lat, lon, cloud=40.0, level_cloud=None, missing_hours=()):
    """1 日分(24 時間)の予報の応答。気圧面の高さは標準大気、雲量は level_cloud {hPa: %}。"""
    times = [f"{DAY}T{h:02d}:00" for h in range(24)]
    h = {"time": times}
    for v in ("cloudcover", "cloudcover_low", "cloudcover_mid", "cloudcover_high"):
        h[v] = [None if i in missing_hours else cloud for i in range(24)]
    for hpa in core.LEVEL_STACK_HPA:
        c = (level_cloud or {}).get(hpa, 0.0)
        h[core.level_var("cloudcover", hpa)] = [None if i in missing_hours else c for i in range(24)]
        h[core.level_var(core.GPH_KIND, hpa)] = [core.pressure_level_altitude_m(hpa)] * 24
    return {"latitude": lat, "longitude": lon, "elevation": 900.0, "hourly": h}


def write_forecasts(parts, models=("jma_msm",), **kw):
    """図に要る点(視点・108 点・山の点)の予報を、展望図の保存先に書く。"""
    from panorama import weather
    pts = [(parts["v"]["lat"], parts["v"]["lon"])] + [(g["lat"], g["lon"]) for g in weather.grid_points(35.5, 138.5)]
    pts += [(p["lat"], p["lon"]) for p in parts["points"].values()]
    for lat, lon in pts:
        for m in models:
            path = weather.pano_cache_path(m, lat, lon, DAY)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fake_response(lat, lon, **kw), fh)


class TestFigureFromFakeData(NoNetwork):
    def make(self, with_weather=True):
        from panorama import run, weather
        parts = fake_parts()
        if with_weather:
            write_forecasts(parts, level_cloud={850: 70.0, 800: 60.0})
        weather.CTX = weather.FetchContext(allow_fetch=False, ttl_sec=0)
        states = weather.make_states(DAY, run.HOURS)
        default = weather.state_of(f"{DAY}T08:00")
        try:
            scenes = run.scenes_for(parts, DAY, states, (35.5, 138.5))
        except weather.WeatherUnavailable:
            scenes = {}
        path = os.path.join(self.tmp, "out", "p.html")
        info = run.figure(parts, scenes, states, default, path, "見本の山からの展望と雲", "<p>説明</p>", None,
                          seed_key="m999", viewer_is_fuji=False, gsi_partial=False)
        with open(path, encoding="utf-8") as fh:
            return fh.read(), info, scenes

    def test_html_from_fake_forecast(self):
        s, info, scenes = self.make()
        self.assertEqual(len(scenes), 4)
        self.assertIn('data-st="h08" aria-pressed="true"', s)          # 開いたときは 8 時
        self.assertIsNone(re.search(r'(src|href)="(https?:)?//', s))   # 外の読み込みが無い
        self.assertNotIn("@import", s)
        frame = re.search(r'<div class="credit39">(.*?)</div></div>', s, re.S).group(1)
        for t in ("地理院タイル(標高タイル)を加工して作成", "「日本の主な山岳標高」(国土地理院)を加工して作成",   # 2026-11-20 の決定で「加工して作成」に
                  "気象データ: Open-Meteo(気象庁 MSM ほか)", "予報にもとづく目安です"):
            self.assertIn(t, frame)
        self.assertNotIn("OpenStreetMap", frame)                       # 山頂の図(OSM を使っていない)
        self.assertEqual(info["question_marks"], 0)

    def test_no_forecast_gives_terrain_only(self):
        s, info, scenes = self.make(with_weather=False)
        self.assertEqual(scenes, {})
        self.assertIn('data-st="terrain" aria-pressed="true"', s)
        self.assertIn("予報なし", s)
        self.assertRegex(s, r'data-st="h08" aria-pressed="false" disabled')


class TestWeather(NoNetwork):
    def test_ecmwf_when_msm_missing(self):
        from panorama import weather
        for m, kw in (("jma_msm", {"missing_hours": (8,)}), ("ecmwf_ifs025", {"cloud": 77.0})):
            p = weather.pano_cache_path(m, 35.5, 138.5, DAY)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(fake_response(35.5, 138.5, **kw), fh)
        weather.CTX = weather.FetchContext(allow_fetch=False, ttl_sec=0)
        fc = weather.get_fc(35.5, 138.5, DAY, [f"{DAY}T08:00"])
        self.assertEqual(fc["cloudcover"][8], 77.0)
        self.assertEqual(fc["_model"][8], "ECMWF")
        self.assertEqual(fc["cloudcover"][7], 40.0)

    def test_unavailable_without_cache_and_fetch(self):
        from panorama import weather
        weather.CTX = weather.FetchContext(allow_fetch=False, ttl_sec=0)
        with self.assertRaises(weather.WeatherUnavailable):
            weather.get_fc(35.5, 138.5, DAY, [f"{DAY}T08:00"])

    def test_old_cache_is_not_used(self):
        from panorama import weather
        p = weather.pano_cache_path("jma_msm", 35.5, 138.5, DAY)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        d = fake_response(35.5, 138.5)
        d["_fetched_epoch"] = 0.0
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        weather.CTX = weather.FetchContext(allow_fetch=False)       # 既定の TTL(本体と同じ 3 時間)
        with self.assertRaises(weather.WeatherUnavailable):
            weather.get_fc(35.5, 138.5, DAY, [f"{DAY}T08:00"])

    def test_reads_main_cache_for_the_mountain(self):
        """診断した山の点(1km 以内)は、診断モードが取った予報(本体の cache/)を読む。本体の関数は変えない。"""
        import time
        from panorama import weather
        main_dir = os.path.join(self.tmp, "main_cache")
        os.makedirs(main_dir)
        full = fake_response(35.5, 138.5, cloud=55.0)
        h = full["hourly"]
        h2 = {k: v + v for k, v in h.items()}
        h2["time"] = [f"2026-01-01T{i:02d}:00" for i in range(24)] + h["time"]
        full["hourly"] = h2
        full["_fetched_at"] = time.time()
        with mock.patch.object(core, "CACHE_DIR", main_dir):
            with open(core._cache_path(35.5, 138.5, "jma_msm", 14), "w", encoding="utf-8") as fh:
                json.dump(full, fh)
            weather.CTX = weather.FetchContext(allow_fetch=False, ttl_sec=0, main_point=(35.5, 138.5), main_days=14)
            fc = weather.get_fc(35.501, 138.5, DAY, [f"{DAY}T08:00"])
        self.assertEqual(fc["time"][0], f"{DAY}T00:00")
        self.assertEqual(fc["cloudcover"][8], 55.0)
        self.assertEqual(weather.CTX.from_main, 1)


class TestFallbacks(NoNetwork):
    def test_without_gsi_table_uses_summits_file(self):
        # 2026-11-20 の決定: 表が無いときは、リポジトリの山頂の点(summits.json。百名山と 76 座だけ)と、
        # 表に点の無い 76 座だけ MOUNTAINS を材料にする(前は MOUNTAINS の 76 座だけだった)
        from panorama import names
        mats, meta = names.load_materials()
        self.assertIsNone(meta["gsi_file"])
        self.assertTrue(meta["summits_only"])
        n_file = len(names.load_summits()[0])
        self.assertGreater(n_file, 100)
        self.assertEqual(sum(1 for m in mats if m["src"] == "gsi1003"), n_file)
        self.assertTrue(all(m["src"] in ("gsi1003", "MOUNTAINS") for m in mats))
        from panorama import draw
        self.assertIn("名前は日本百名山とヤマビヨリの 76 座です", " ".join(draw.credit_lines(False, gsi_partial=True)))

    def test_osm_failure_gives_summit_only(self):
        import requests
        from panorama import run
        stats = {"overpass_calls": 0}
        with mock.patch.object(requests, "post", side_effect=requests.ConnectionError("x")),                 mock.patch.object(run.time, "sleep") as sl:
            els, why = run.osm_elements("m999", 35.5, 138.5, stats)
        self.assertIsNone(els)
        self.assertIn("接続できない", why)
        # 2026-11-20 の指示書 7 章: 失敗したら 5 秒あけて 1 回だけ取り直す(前は 1 回だけ)
        self.assertEqual(stats["overpass_calls"], 2)
        sl.assert_called_once_with(5.0)

    def test_osm_non_json_is_handled(self):
        import requests
        from panorama import run
        resp = mock.Mock(status_code=200, text="<html>busy</html>")
        resp.json.side_effect = ValueError("no json")
        with mock.patch.object(requests, "post", return_value=resp):
            els, why = run.osm_elements("m999", 35.5, 138.5, {"overpass_calls": 0})
        self.assertIsNone(els)
        self.assertIn("JSON でない", why)

    def test_choose_viewpoint_rules(self):
        from panorama import run

        class Elev:
            def pair(self, lat0, lon0, lat1, lon1):
                return 2000.0, 2000.0 - (lat0 - lat1) * 1e5 * 0.2, 14
        summit = {"kind": "summit", "id": "m999", "name": "見本の山", "lat": 35.5, "lon": 138.5, "summit_point": True,
                  "nominal_ele_m": 2000.0, "source": "summit_gsi", "osm_used": False}
        els = [{"type": "node", "id": 1, "lat": 35.499, "lon": 138.5, "tags": {"tourism": "viewpoint"}},          # 約 111m・-22m
               {"type": "node", "id": 2, "lat": 35.498, "lon": 138.5, "tags": {"tourism": "viewpoint", "name": "見晴台"}},  # 約 222m・-44m
               {"type": "node", "id": 3, "lat": 35.496, "lon": 138.5, "tags": {"tourism": "viewpoint", "name": "遠い所"}}]  # 約 444m
        vp, s = run.choose_viewpoint(summit, els, Elev())
        self.assertEqual(vp["label"], "見晴台")             # 名前のある点を優先(300m 以内・50m を超えて低くない)
        self.assertTrue(vp["osm_used"])
        vp2, s2 = run.choose_viewpoint(summit, [els[2]], Elev())
        self.assertIsNone(vp2)


class TestPartsCache(NoNetwork):
    def test_saved_and_rebuilt_when_conditions_change(self):
        from panorama import run
        calls = []

        def fake_compute(v, ctx):
            calls.append(v["lat"])
            return {"made": len(calls)}
        v = {"kind": "summit", "id": "m999", "lat": 35.5, "lon": 138.5}
        stats = {"parts_cached": 0, "parts_rebuilt": 0, "parts_sec": 0.0}
        with mock.patch.object(run, "compute_parts", fake_compute):
            a = run.load_or_compute_parts(v, {}, "x.geojson", stats)
            b = run.load_or_compute_parts(v, {}, "x.geojson", stats)
            self.assertEqual((a, b, len(calls), stats["parts_cached"]), ({"made": 1}, {"made": 1}, 1, 1))
            c = run.load_or_compute_parts(v, {}, None, stats)                       # 1003 山の表が変わった
            self.assertEqual((c, stats["parts_rebuilt"]), ({"made": 2}, 1))
            with mock.patch.object(run, "PARTS_VERSION", "test-2"):
                d = run.load_or_compute_parts(v, {}, None, stats)                   # コードの版が変わった
            self.assertEqual(d, {"made": 3})
            v2 = dict(v, lat=35.6)
            e = run.load_or_compute_parts(v2, {}, None, stats)                      # 視点の座標が変わった
            self.assertEqual(e, {"made": 4})
        with open(run.parts_path(v), "rb") as fh:
            self.assertIn("conditions", pickle.load(fh))


class TestDetailOption(NoNetwork):
    def run_main(self, argv, pano_effect=None):
        import mountain_weather_detail as detail

        def fake_single(mtn, report=None):
            print("診断の出力")
            if report is not None:
                report["x"] = 1
            return 0
        out = io.StringIO()
        with mock.patch.object(detail, "main_single_mountain", fake_single), contextlib.redirect_stdout(out):
            if pano_effect is not None:
                import panorama.run as pr
                with mock.patch.object(pr, "make_panorama", side_effect=pano_effect):
                    code = detail.main(argv)
            else:
                code = detail.main(argv)
        return code, out.getvalue()

    def test_default_has_no_panorama(self):
        import mountain_weather_detail as detail
        a = detail.parse_args(["--mountain", "m001"])
        self.assertFalse(a.panorama)
        self.assertIsNone(a.panorama_out)
        code, out = self.run_main(["--mountain", "m001"])
        self.assertEqual((code, out), (0, "診断の出力\n"))

    def test_failure_keeps_diagnosis_and_exit_code(self):
        code, out = self.run_main(["--mountain", "m001", "--panorama"], pano_effect=RuntimeError("見本の失敗"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "診断の出力\n展望図は作れませんでした(見本の失敗)\n")

    def test_success_adds_path_line(self):
        res = {"files": [{"path": "out/panorama/m001.html"}], "notes": [], "day": DAY,
               "stats": {"tiles_fetched": 0, "openmeteo_calls": 0, "openmeteo_weight": 0.0, "overpass_calls": 0, "sec": 1.0,
                         "tiles_sec": 0.0, "parts_sec": 0.0}}
        import panorama.run as pr
        out = io.StringIO()
        import mountain_weather_detail as detail
        with mock.patch.object(detail, "main_single_mountain", lambda m, r=None: print("診断の出力") or 0), \
                mock.patch.object(pr, "make_panorama", return_value=res), contextlib.redirect_stdout(out):
            code = detail.main(["--mountain", "m001", "--panorama"])
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "診断の出力")
        self.assertEqual(lines[1], "展望図: out/panorama/m001.html")

    def test_not_in_76(self):
        code, out = self.run_main(["--name", "見本の山", "--lat", "35.5", "--lon", "138.5", "--elevation", "1500",
                                   "--panorama"])
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("展望図は 76 座だけです\n"))

    def test_make_panorama_rejects_non_76(self):
        from panorama import run
        with self.assertRaises(run.NotSupported):
            run.make_panorama({"id": None, "name": "見本の山", "lat": 35.5, "lon": 138.5, "elevation_m": 1500})

    def test_argument_errors(self):
        code, out = self.run_main(["--mountain", "m001", "--panorama-out", "x.html"])
        self.assertEqual(code, 2)
        code, out = self.run_main(["--list", "--panorama"])
        self.assertEqual(code, 2)

    def test_json_out_unchanged_by_panorama_failure(self):
        p = os.path.join(self.tmp, "r.json")
        code, out = self.run_main(["--mountain", "m001", "--panorama", "--json-out", p], pano_effect=RuntimeError("x"))
        self.assertEqual(code, 0)
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh).get("x"), 1)


if __name__ == "__main__":
    unittest.main()


class TestFirstTiles(NoNetwork):
    """初めての山: 要る標高タイルを数え、知らせ、1 枚ずつ取る(通信は作った応答)。取れないタイルが残れば作らない。"""

    def run_tiles(self, fail=()):
        import requests
        from panorama import names, run, terrain
        txt = "\n".join([",".join(["1000"] * 256)] * 256)

        def fake_get(sess, url, **kw):
            bad = any(url.endswith(f"/{x}/{y}.txt") for x, y in fail)
            r = mock.Mock(status_code=500 if bad else 200, text=txt)
            sess.statuses.append(r.status_code)
            return r
        logs = []
        stats = {}
        v = {"lat": 35.5, "lon": 138.5}
        with mock.patch.object(terrain, "tiles_needed", lambda z11, z14, v: ({(10, 20), (10, 21)}, {(500, 600)})), \
                mock.patch.object(terrain, "STAGE2_EXTRA_DELAY_SEC", 0.0), \
                mock.patch.object(names.StatusSession, "get", fake_get), \
                mock.patch.object(requests.Session, "get", side_effect=AssertionError("ほかの通信")):
            try:
                run.ensure_tiles([v], stats, log=logs.append)
                err = None
            except run.PanoramaUnavailable as e:
                err = str(e)
        return logs, stats, err

    def test_notice_and_fetch_then_cached(self):
        logs, stats, err = self.run_tiles()
        self.assertIsNone(err)
        self.assertEqual(logs, ["(展望図: 初回は標高タイル 3 枚を取ります。約 1 分かかります)"])
        self.assertEqual((stats["tiles_needed"], stats["tiles_fetched"], stats["tiles_failed"]), (3, 3, 0))
        logs2, stats2, err2 = self.run_tiles()
        self.assertEqual((logs2, stats2["tiles_fetched"], err2), ([], 0, None))     # 2 回目は保存を読む

    def test_counts_add_up_over_two_calls(self):
        """山頂のあとに展望地の分を足して取っても、通信の量の行の枚数は合計になる。"""
        from panorama import run, terrain
        logs, stats, err = self.run_tiles()
        with mock.patch.object(terrain, "tiles_needed", lambda z11, z14, v: ({(10, 20), (10, 22)}, {(500, 600)})),                 mock.patch.object(terrain, "STAGE2_EXTRA_DELAY_SEC", 0.0),                 mock.patch.object(terrain.Dem, "tile", lambda self, x, y: (setattr(self, "fetched", self.fetched + 1),
                                                                           self.store._mem.__setitem__((x, y), 1))[0]):
            more = []
            run.ensure_tiles([{"lat": 35.5, "lon": 138.5}], stats, log=more.append, extra=True)
        self.assertEqual(stats["tiles_fetched"], 4)
        self.assertEqual(more, ["(展望図: 展望地のために標高タイルを 1 枚足して取ります。約 1 分かかります)"])

    def test_failed_tile_stops_the_figure(self):
        logs, stats, err = self.run_tiles(fail=((10, 21),))
        self.assertEqual(stats["tiles_failed"], 1)
        self.assertIn("標高タイルが 1 枚取れない", err)


class TestPanoramaDate(NoNetwork):
    """--panorama-date(展望図の日付の指定)。予報の範囲は今日から FORECAST_DAYS 日分。"""

    def test_check_day(self):
        import datetime as dt
        import mountain_weather_detail as detail
        today = dt.date(2026, 11, 24)
        last = today + dt.timedelta(days=detail.FORECAST_DAYS - 1)
        for ok in (today, dt.date(2026, 11, 26), last):
            self.assertEqual(detail.panorama_day_from_arg(ok.isoformat(), today), (ok.isoformat(), None))
        for text, word in (("2026-11-23", "過去の日付"), ((last + dt.timedelta(days=1)).isoformat(), "より先"),
                           ("11/26", "読めません"), ("20261126", "読めません"), ("2026-11-31", "読めません"), ("", "読めません")):
            day, error = detail.panorama_day_from_arg(text, today)
            self.assertIsNone(day, text)
            self.assertIn(word, error)

    def call(self, argv, error=None):
        import mountain_weather_detail as detail
        import panorama.run as pr
        seen = {}

        def fake(mtn, **kw):
            seen.update(kw)
            raise RuntimeError("ここまで")
        out = io.StringIO()
        with mock.patch.object(detail, "main_single_mountain", lambda m, r=None: 0), \
                mock.patch.object(detail, "panorama_day_from_arg", lambda text: (None, error) if error else (text, None)), \
                mock.patch.object(pr, "make_panorama", side_effect=fake), \
                mock.patch.object(pr, "estimate", side_effect=fake), contextlib.redirect_stdout(out):
            code = detail.main(argv)
        return code, out.getvalue(), seen

    def test_day_is_passed(self):
        _, _, seen = self.call(["--mountain", "m026", "--panorama", "--panorama-date", "2026-11-26"])
        self.assertEqual(seen["day"], "2026-11-26")
        _, _, seen = self.call(["--mountain", "m026", "--panorama-estimate", "--panorama-date", "2026-11-26"])
        self.assertEqual(seen["day"], "2026-11-26")

    def test_without_date_is_as_before(self):
        import datetime as dt
        from panorama import run
        _, _, seen = self.call(["--mountain", "m026", "--panorama"])
        self.assertIsNone(seen["day"])                             # None = 翌日(make_panorama の既定)
        with mock.patch.object(run, "date") as d:
            d.today.return_value = dt.date(2026, 11, 24)
            self.assertEqual(run.tomorrow(), "2026-11-25")
        self.assertEqual(os.path.basename(run.default_out_path("m026")), "m026.html")
        self.assertEqual(os.path.basename(run.default_out_path("m026", "viewpoint")), "m026v.html")

    def test_file_name_with_date(self):
        from panorama import run
        self.assertEqual(os.path.basename(run.default_out_path("m026", day="2026-11-26")), "m026_2026-11-26.html")
        self.assertEqual(os.path.basename(run.default_out_path("m029", "viewpoint", "2026-11-26")), "m029v_2026-11-26.html")

    def test_refused_before_diagnosis(self):
        code, out, seen = self.call(["--mountain", "m026", "--panorama", "--panorama-date", "2020-01-01"], error="断る文")
        self.assertEqual((code, out, seen), (2, "断る文\n", {}))

    def test_needs_panorama(self):
        code, out, seen = self.call(["--mountain", "m026", "--panorama-date", "2026-11-26"])
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("--panorama か --panorama-estimate", out)
