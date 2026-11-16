"""Open-Meteo の利用上限(HTTP 429)の種類分けと、探索モードの通信の間隔の調整のテスト(2026-11-16)。

通信しない・本当に待たない: requests.Session と時計(core._clock)・待ち(core._sleep、time_module.sleep)を差し替える。
考え方はホシビヨリ v1.1.0 と同じ(値: 直近60秒の重さ300、1分の上限は65秒待ち・1回の実行で5回まで)。
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import requests

import mountain_weather_core as core
import mountain_weather_detail as detail
import mountain_weather_mvp as mvp

MINUTELY_REASON = "Minutely API request limit exceeded. Please try again in one minute."
HOURLY_REASON = "Hourly API request limit exceeded. Please try again in the next hour."
DAILY_REASON = "Daily API request limit exceeded. Please try again tomorrow."


def run_quietly(func, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args, **kwargs)
    return result, out.getvalue()


def resp429(body=None, text=None):
    """429 の応答のまね。body が dict なら JSON、そうでなければ json() が失敗して text を読む。"""
    r = mock.MagicMock()
    r.status_code = 429
    if isinstance(body, dict):
        r.json.return_value = body
        r.text = json.dumps(body)
    else:
        r.json.side_effect = ValueError("not json")
        r.text = text if text is not None else ""
    return r


def resp_ok(payload=None):
    r = mock.MagicMock()
    r.status_code = 200
    r.json.return_value = payload if payload is not None else {}
    return r


class FakeClock:
    """_clock の代わり。_sleep(秒) で時計が進む。"""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PacingTestBase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClock()
        for target, attr, value in ((core, "_clock", self.fake.clock), (core, "_sleep", self.fake.sleep),
                                    (core, "_HTTP_SESSION", None)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        backoff = mock.patch.object(core.time_module, "sleep")   # 2秒・4秒の再試行の待ち
        self.backoff = backoff.start()
        self.addCleanup(backoff.stop)
        self.stats = core.configure_open_meteo_pacing(enabled=False)
        self.addCleanup(core.configure_open_meteo_pacing, False)

    def session(self, side_effect):
        """以後の get_with_retry が使う接続を、まねの Session にする(setUp の patch が後で元に戻す)。"""
        s = mock.MagicMock(spec=requests.Session)
        s.get.side_effect = side_effect
        core._HTTP_SESSION = s
        return s


class TestClassify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(core.classify_rate_limit(MINUTELY_REASON), "minutely")
        self.assertEqual(core.classify_rate_limit(HOURLY_REASON), "hourly")
        self.assertEqual(core.classify_rate_limit(DAILY_REASON), "daily")
        self.assertEqual(core.classify_rate_limit("DAILY limit"), "daily")
        self.assertEqual(core.classify_rate_limit("Too many requests"), "unknown")
        self.assertEqual(core.classify_rate_limit(""), "unknown")
        self.assertEqual(core.classify_rate_limit(None), "unknown")

    def test_heavier_kind_wins(self):
        self.assertEqual(core.classify_rate_limit("minutely and daily"), "daily")
        self.assertEqual(core.classify_rate_limit("minutely or hourly"), "hourly")

    def test_reason_from_json_or_text(self):
        self.assertEqual(core.rate_limit_reason(resp429({"error": True, "reason": MINUTELY_REASON})), MINUTELY_REASON)
        body = "<html>" + "x" * 500
        self.assertEqual(core.rate_limit_reason(resp429(text=body)), body[:200])
        self.assertEqual(core.rate_limit_reason(resp429({"error": True})), json.dumps({"error": True}))

    def test_exception_is_request_exception(self):
        e = core.OpenMeteoRateLimitError("hourly", HOURLY_REASON)
        self.assertIsInstance(e, requests.exceptions.RequestException)
        self.assertIn("hourly", str(e))
        self.assertIn(HOURLY_REASON, str(e))


class TestRateLimitInGetWithRetry(PacingTestBase):
    def test_minutely_waits_65_and_refetches(self):
        ok = resp_ok({"hourly": {}})
        s = self.session([resp429({"reason": MINUTELY_REASON}), ok])
        result, out = run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {"latitude": 35.0})
        self.assertIs(result, ok)
        self.assertEqual(s.get.call_count, 2)
        self.assertEqual(self.fake.sleeps, [65.0])
        self.backoff.assert_not_called()
        self.assertEqual(self.stats.minutely_waits, 1)
        self.assertEqual(self.stats.minutely_wait_sec, 65.0)
        self.assertIn("1分の上限のため65秒待ちます(1/5)", out)
        self.assertIn("種類=minutely", out)
        self.assertIn(MINUTELY_REASON, out)
        for call in s.get.call_args_list:   # 取り直しも同じ通信
            self.assertEqual(call.args, (core.OPEN_METEO_URL,))
            self.assertEqual(call.kwargs, {"params": {"latitude": 35.0}, "timeout": 20})

    def test_minutely_does_not_use_up_connection_retries(self):
        err = requests.exceptions.ConnectionError("reset")
        ok = resp_ok()
        s = self.session([resp429({"reason": MINUTELY_REASON}), err, err, ok])
        result, _ = run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {})
        self.assertIs(result, ok)
        self.assertEqual(s.get.call_count, 4)
        self.assertEqual([c.args for c in self.backoff.call_args_list], [(2,), (4,)])

    def test_minutely_six_times_raises_after_five_waits(self):
        s = self.session([resp429({"reason": MINUTELY_REASON}) for _ in range(6)])
        with self.assertRaises(core.OpenMeteoRateLimitError) as cm:
            run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {})
        self.assertEqual(cm.exception.kind, "minutely")
        self.assertTrue(cm.exception.exhausted_minutely)
        self.assertEqual(s.get.call_count, 6)
        self.assertEqual(self.fake.sleeps, [65.0] * 5)
        self.assertEqual(self.stats.minutely_waits, 5)

    def test_minutely_budget_is_per_run(self):
        # 5回の待ちは「1回の実行」で合計。使い切ったあとは、別の通信でも待たずに止める
        self.session([resp429({"reason": MINUTELY_REASON}), resp_ok()] * 5 + [resp429({"reason": MINUTELY_REASON})])
        for _ in range(5):
            run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {})
        with self.assertRaises(core.OpenMeteoRateLimitError):
            run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {})
        self.assertEqual(self.fake.sleeps, [65.0] * 5)
        core.configure_open_meteo_pacing(enabled=False)   # 次の実行では0に戻る
        self.assertEqual(core.open_meteo_run_stats().minutely_waits, 0)

    def test_hourly_daily_unknown_raise_without_wait(self):
        cases = (("hourly", resp429({"reason": HOURLY_REASON})), ("daily", resp429({"reason": DAILY_REASON})),
                 ("unknown", resp429(text="Too Many Requests")), ("unknown", resp429(text="")))
        for kind, r in cases:
            with self.subTest(kind=kind, text=r.text):
                core.configure_open_meteo_pacing(enabled=False)
                self.backoff.reset_mock()
                self.fake.sleeps.clear()
                s = self.session([r, resp_ok()])
                with self.assertRaises(core.OpenMeteoRateLimitError) as cm:
                    run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {})
                self.assertEqual(cm.exception.kind, kind)
                self.assertFalse(cm.exception.exhausted_minutely)
                self.assertEqual(s.get.call_count, 1)    # 2秒・4秒の再試行をしない
                self.backoff.assert_not_called()
                self.assertEqual(self.fake.sleeps, [])
                r.raise_for_status.assert_not_called()

    def test_non_open_meteo_429_is_unknown_and_still_a_request_exception(self):
        # ひまわり(気象庁)の 429 は Open-Meteo の reason の形ではないので unknown。呼び出し元の
        # `except RequestException` で従来どおり受けられる(灰色タイルにする等)。通信は1回で済む
        s = self.session([resp429(text="<html>429 Too Many Requests</html>")])
        url = f"{core.HIMAWARI_BASE_URL}/targetTimes_jp.json"
        with self.assertRaises(requests.exceptions.RequestException) as cm:
            run_quietly(core.get_with_retry, url, None)
        self.assertEqual(cm.exception.kind, "unknown")
        self.assertEqual(s.get.call_count, 1)
        self.assertEqual(self.stats.requests, 0)   # Open-Meteo の通信としては数えない

    def test_success_path_unchanged(self):
        ok = resp_ok({"a": 1})
        s = self.session([ok])
        result, out = run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {"latitude": 35.0, "hourly": ["x"]})
        self.assertIs(result, ok)
        self.assertEqual(out, "")
        self.assertEqual(s.get.call_count, 1)
        self.assertEqual(s.get.call_args.kwargs, {"params": {"latitude": 35.0, "hourly": ["x"]}, "timeout": 20})
        ok.raise_for_status.assert_called_once_with()
        self.assertEqual(self.fake.sleeps, [])

    def test_5xx_retry_unchanged(self):
        bad = resp_ok()
        bad.status_code = 503
        bad.raise_for_status.side_effect = requests.exceptions.HTTPError("503")
        s = self.session([bad, bad, bad])
        with self.assertRaises(requests.exceptions.HTTPError):
            run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {})
        self.assertEqual(s.get.call_count, 3)
        self.assertEqual([c.args for c in self.backoff.call_args_list], [(2,), (4,)])
        self.assertEqual(self.fake.sleeps, [])


class TestPacer(PacingTestBase):
    def test_weight(self):
        self.assertEqual(core.request_weight({"hourly": ["a"] * 5, "forecast_days": 7}), 1.0)
        self.assertEqual(core.request_weight({"hourly": ["a"] * 30, "forecast_days": 14}), 3.0)
        self.assertAlmostEqual(core.request_weight({"hourly": ["a"] * 30, "daily": ["s", "t"], "forecast_days": 21}),
                               3.2 * 1.5)
        self.assertEqual(core.request_weight({"hourly": "a,b,c", "forecast_days": 28}), 2.0)
        self.assertEqual(core.request_weight(None), 1.0)

    def test_values_match_hoshibiyori(self):
        self.assertEqual(core.PACING_LIMIT_PER_MINUTE, 300.0)
        self.assertEqual(core.PACING_WINDOW_SEC, 60.0)
        self.assertEqual(core.MINUTELY_RETRY_WAIT_SEC, 65.0)
        self.assertEqual(core.MINUTELY_RETRY_MAX, 5)

    def test_no_wait_under_limit(self):
        p = core.RequestPacer()
        for _ in range(30):
            self.assertEqual(p.acquire(10.0), 0.0)   # 合計 300 ちょうどまでは待たない
        self.assertEqual(self.fake.sleeps, [])

    def test_waits_when_over_limit_and_never_exceeds(self):
        p = core.RequestPacer()
        sent = []
        for i in range(100):
            p.acquire(7.0)
            sent.append(self.fake.now)
            self.fake.now += 0.5   # 0.5秒ごとに送りたい(放っておくと1分に約840)
            window = [t for t in sent if t > self.fake.now - 60.0]
            self.assertLessEqual(len(window) * 7.0, 300.0 + 1e-9)
        self.assertTrue(self.fake.sleeps)
        self.assertGreater(sum(self.fake.sleeps), 0)

    def test_get_with_retry_paces_only_when_enabled_and_only_api_host(self):
        heavy = {"hourly": [f"v{i}" for i in range(100)], "forecast_days": 14}   # 重さ 10
        # 無効(既定。診断モード): 何回送っても待たない
        self.session(lambda *a, **k: resp_ok())
        for _ in range(40):
            core.get_with_retry(core.OPEN_METEO_URL, heavy)
        self.assertEqual(self.fake.sleeps, [])
        # 有効(探索モード): 30回目まで待たず、31回目で待つ
        stats = core.configure_open_meteo_pacing(enabled=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(30):
                core.get_with_retry(core.OPEN_METEO_URL, heavy)
            self.assertEqual(self.fake.sleeps, [])
            core.get_with_retry(core.OPEN_METEO_URL, heavy)
        self.assertEqual(len(self.fake.sleeps), 1)
        self.assertEqual(stats.pacing_waits, 1)
        self.assertGreater(stats.pacing_sec, 0)
        self.assertIn("通信の間隔を調整しています", out.getvalue())
        # ENS(ensemble-api)・気象庁は数えない
        before = len(stats.pacer.sent)
        for _ in range(40):
            core.get_with_retry("https://ensemble-api.open-meteo.com/v1/ensemble", heavy)
            core.get_with_retry(f"{core.HIMAWARI_BASE_URL}/x.json", None)
        self.assertEqual(len(stats.pacer.sent), before)
        self.assertEqual(len(self.fake.sleeps), 1)

    def test_retries_go_through_pacer(self):
        stats = core.configure_open_meteo_pacing(enabled=True)
        err = requests.exceptions.ConnectionError("x")
        self.session([err, err, resp_ok()])
        run_quietly(core.get_with_retry, core.OPEN_METEO_URL, {"hourly": ["a"]})
        self.assertEqual(len(stats.pacer.sent), 3)

    def test_cache_hit_is_not_counted(self):
        stats = core.configure_open_meteo_pacing(enabled=True)
        payload = {"hourly": {"time": ["2026-11-16T00:00"], "temperature_2m": [1.0]}, "daily": {}}
        s = self.session(lambda *a, **k: resp_ok(json.loads(json.dumps(payload))))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "CACHE_DIR", d), \
                mock.patch.object(core.time_module, "time", return_value=1_800_000_000.0):
            core.fetch_open_meteo(35.0, 138.0, hourly=["temperature_2m"], days=15, model="jma_msm")
            core.fetch_open_meteo(35.0, 138.0, hourly=["temperature_2m"], days=15, model="jma_msm")
        self.assertEqual(s.get.call_count, 1)
        self.assertEqual(len(stats.pacer.sent), 1)
        self.assertEqual(stats.requests, 1)

    def test_breakdown_line(self):
        stats = core.configure_open_meteo_pacing(enabled=True)
        stats.fetch_sec, stats.pacing_sec = 100.0, 20.0
        line = core.timing_breakdown_line(150.0, stats)
        self.assertEqual(line, "実行時間の内訳: 全体 150.0 秒 / Open-Meteoの通信 100.0 秒 / 間隔の調整 20.0 秒 / "
                               "それ以外 30.0 秒(全体 − 通信 − 間隔の調整)")
        stats.minutely_waits, stats.minutely_wait_sec = 1, 65.0
        self.assertIn("間隔の調整 85.0 秒(うち1分の上限の待ち 1 回・65.0 秒)", core.timing_breakdown_line(200.0, stats))


def _fake_windows():
    return {
        "ridge": {"wind": 10.0, "temp": 5.0, "cloud": 20.0, "cloud_raw": 30.0, "visibility": 30000},
        "pm": {"cloud": 25.0, "cloud_raw": 35.0, "cape": 0.0},
        "activity": {"wet_fraction_pct": 0.0, "total_mm": 0.0, "wet_hours": 0.0, "total_hours": 10,
                     "msm_hours": 10, "moist_hours": 0, "prob_hours": 0, "terrain": None, "terrain_base": None},
        "day_peak": {"wind_ms": 4.0, "hour": "10:00"},
        "day_total_precip_mm": 0.0,
    }


class TestExploreStopsOnRateLimit(PacingTestBase):
    def run_mvp(self, side_effect, argv):
        today = str(date.today())
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(mvp, "fetch_forecast", side_effect=side_effect) as fetch, \
                mock.patch.object(mvp, "window_scores_by_day", return_value={today: _fake_windows()}):
            path = os.path.join(d, "e.json")
            code, out = run_quietly(mvp.main, [*argv, "--json-out", path])
            data = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
        return code, out, data, fetch

    def test_stops_remaining_mountains(self):
        limit = core.OpenMeteoRateLimitError("hourly", HOURLY_REASON)
        code, out, data, fetch = self.run_mvp([{}, limit, {}, {}, {}], ["--limit", "5"])
        self.assertEqual(code, 0)                # 一部が取れなかったときと同じ(従来: fetched>0 なら0)
        self.assertEqual(fetch.call_count, 2)    # 3座目以降は取りに行かない
        name2 = core.MOUNTAINS[1]["name"]
        self.assertIn(f"({name2}: 取得失敗のためスキップ - ", out)
        self.assertEqual(out.count("のため、残り 3 座の取得をやめました"), 1)
        self.assertIn("利用上限(種類=hourly、reason=" + HOURLY_REASON + ")", out)
        self.assertIn("【対象地域:", out)
        self.assertIn("実行時間の内訳: 全体", out)
        self.assertEqual((data["pool_size"], data["fetched"]), (5, 1))
        self.assertEqual(len(data["days"][0]["all"]), 1)
        self.assertFalse(core.open_meteo_run_stats().pacing_enabled)   # 実行が終われば無効に戻る

    def test_rate_limit_on_first_mountain_exits_2(self):
        limit = core.OpenMeteoRateLimitError("daily", DAILY_REASON)
        code, out, data, fetch = self.run_mvp([limit, {}, {}], ["--limit", "3"])
        self.assertEqual(code, 2)               # 全山失敗と同じ
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("残り 2 座の取得をやめました", out)
        self.assertNotIn("【対象地域:", out)
        self.assertIsNone(data)

    def test_ordinary_failure_still_skips_and_continues(self):
        err = requests.exceptions.ConnectionError("offline")
        code, out, data, fetch = self.run_mvp([{}, err, {}], ["--limit", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 3)
        self.assertNotIn("取得をやめました", out)
        self.assertEqual((data["pool_size"], data["fetched"]), (3, 2))

    def test_no_requests_after_rate_limit_end_to_end(self):
        # 本物の fetch_forecast → fetch_open_meteo → get_with_retry を通し、通信の回数を数える
        s = self.session(lambda *a, **k: resp429({"reason": DAILY_REASON}))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "CACHE_DIR", d):
            code, out = run_quietly(mvp.main, ["--limit", "4"])
        self.assertEqual(code, 2)
        self.assertEqual(s.get.call_count, 1)
        self.assertIn("残り 3 座の取得をやめました", out)
        self.assertEqual(self.fake.sleeps, [])
        self.backoff.assert_not_called()

    def test_explore_enables_pacing_during_run(self):
        seen = []

        def fetch(*a, **k):
            seen.append(core.open_meteo_run_stats().pacing_enabled)
            return {}
        self.run_mvp(fetch, ["--limit", "2"])
        self.assertEqual(seen, [True, True])
        self.assertFalse(core.open_meteo_run_stats().pacing_enabled)


class TestDiagnosisRateLimit(PacingTestBase):
    def test_rate_limit_is_fetch_failure_exit_2(self):
        limit = core.OpenMeteoRateLimitError("hourly", HOURLY_REASON)
        with mock.patch.object(detail, "fetch_forecast", side_effect=limit):
            code, out = run_quietly(detail.main, ["--mountain", "1"])
        self.assertEqual(code, 2)
        self.assertIn("取得に失敗しました", out)
        self.assertIn(HOURLY_REASON, out)

    def test_diagnosis_does_not_pace(self):
        self.assertFalse(core.open_meteo_run_stats().pacing_enabled)   # 既定は無効
        heavy = {"hourly": [f"v{i}" for i in range(100)], "forecast_days": 14}
        self.session(lambda *a, **k: resp_ok())
        for _ in range(50):
            core.get_with_retry(core.OPEN_METEO_URL, heavy)
        self.assertEqual(self.fake.sleeps, [])


if __name__ == "__main__":
    unittest.main()
