"""長い作業の前の予告(time_estimates.py・--panorama-estimate・進み具合・始まりの 1 行)のテスト。通信しない(ソケットを作ると失敗にする)。"""
import contextlib
import io
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

import mountain_weather_core as core  # noqa: E402
import mountain_weather_detail as detail  # noqa: E402
import mountain_weather_mvp as mvp  # noqa: E402
import time_estimates as te  # noqa: E402
from panorama import run, terrain, weather  # noqa: E402


def _no_socket(*a, **k):
    raise AssertionError("テストで通信しようとした")


def capture(func, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = func(*args)
    return result, out.getvalue(), err.getvalue()


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
        self.mtn = core.mountain_by_id("m058")

    def needed(self):
        """大菩薩嶺に要るタイル(実際の数え方)。"""
        ctx, _ = run.build_context()
        v = run.summit_viewer(self.mtn, ctx)
        z11, z14 = terrain.Dem(None, 11, allow_fetch=False), terrain.Dem(None, 14, allow_fetch=False)
        need11, need14, _, _ = run.missing_tiles([v], z11, z14)
        return v, sorted(need11), sorted(need14)

    def put(self, zoom, tiles):
        """空のファイルを置く(見積もりは手元にあるかだけを見る)。"""
        d = os.path.join(self.tmp, "terrain", f"dem_z{zoom}")
        os.makedirs(d, exist_ok=True)
        for x, y in tiles:
            open(os.path.join(d, f"{zoom}_{x}_{y}.npy"), "wb").close()


class TestTimeText(unittest.TestCase):
    def test_about(self):
        self.assertEqual([te.about(s) for s in (1, 20, 54, 55, 89, 109, 285, 734)],
                         ["約 10 秒", "約 20 秒", "約 50 秒", "約 1 分", "約 1 分", "約 2 分", "約 5 分", "約 12 分"])

    def test_start_line(self):
        self.assertEqual(te.start_line("76 座の予報を取ります", 300), "76 座の予報を取ります(時間の目安: 約 5 分)")
        self.assertEqual(te.start_line("3 座の予報を取ります", None), "3 座の予報を取ります")   # 未実測は時間を書かない

    def test_agents_table_matches_constants(self):
        """AGENTS.md の表の目安と、コードの定数が食い違わない。"""
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
            text = f.read()
        row = re.search(r"^\| 探索モード 全 76 座 \|[^|]*\| ([^|]*) \|", text, re.M)
        self.assertEqual(row.group(1), te.about(te.EXPLORE_ALL_SEC))
        row = re.search(r"^\| 診断モード 1 座 \|[^|]*\| ([^|]*) \|", text, re.M)
        self.assertEqual(row.group(1), "未実測" if te.DIAGNOSIS_SEC is None else te.about(te.DIAGNOSIS_SEC))
        row = re.search(r"^\| 診断モード \+ 展望図\(2 回目以降\) \|.*$", text, re.M).group(0)
        self.assertIn(f"すべて保存済みで{te.about(te.PANORAMA_SAVED_SEC)}", row)
        self.assertIn(f"予報は保存なしで{te.about(te.PANORAMA_FORECAST_SEC)}", row)


class TestEstimate(NoNetwork):
    def test_no_tiles(self):
        _, n11, n14 = self.needed()
        est = run.estimate(self.mtn)
        self.assertEqual((est["needed"], est["missing"]), (len(n11) + len(n14), len(n11) + len(n14)))
        self.assertEqual(est["zoom"][11], {"needed": len(n11), "missing": len(n11)})
        self.assertEqual(est["zoom"][14], {"needed": len(n14), "missing": len(n14)})
        self.assertAlmostEqual(est["sec"], est["missing"] * 734 / 566 + te.PANORAMA_FIRST_SEC)

    def test_some_tiles(self):
        _, n11, n14 = self.needed()
        self.put(11, n11[:400])
        self.put(14, n14[:3])
        est = run.estimate(self.mtn)
        self.assertEqual(est["zoom"][11]["missing"], len(n11) - 400)
        self.assertEqual(est["zoom"][14]["missing"], len(n14) - 3)
        self.assertEqual(est["missing"], len(n11) + len(n14) - 403)
        self.assertAlmostEqual(est["tiles_sec"], est["missing"] * 734 / 566)

    def test_all_tiles_without_saved_parts(self):
        _, n11, n14 = self.needed()
        self.put(11, n11)
        self.put(14, n14)
        est = run.estimate(self.mtn)
        self.assertEqual((est["missing"], est["tiles_sec"], est["sec"]), (0, 0, te.PANORAMA_FIRST_SEC))

    def _save_parts(self, v):
        ctx, gsi = run.build_context()
        parts = {"v": v, "peaks": [], "points": {}}
        path = run.parts_path(v)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump({"conditions": run.parts_conditions(v, gsi), "parts": parts}, fh)

    def test_saved_parts_shorten_the_estimate(self):
        """地形の計算だけ保存済み(予報は無い)= 49.3 秒の実測、予報もそろっている = 約 20 秒。"""
        v, n11, n14 = self.needed()
        self.put(11, n11)
        self.put(14, n14)
        self._save_parts(v)
        est = run.estimate(self.mtn)                             # 地形の計算だけ保存済み、予報は無い
        self.assertTrue(est["parts_saved"])
        self.assertFalse(est["weather_saved"])
        self.assertEqual(est["sec"], te.PANORAMA_FORECAST_SEC)
        self.assertEqual(run.estimate_lines(self.mtn, est)[2], "時間の目安: 約 50 秒(タイルの取得 なし + 予報と図づくり 約 50 秒)")
        with mock.patch.object(weather, "get_fc", return_value={"time": []}):
            est = run.estimate(self.mtn)                         # 予報もそろっている
        self.assertTrue(est["weather_saved"])
        self.assertEqual(est["sec"], te.PANORAMA_SAVED_SEC)

    def test_saved_weather_without_parts_is_long(self):
        """地形の計算の保存が無ければ、予報がそろっていても初回の値(その組み合わせの実測は無い)。"""
        _, n11, n14 = self.needed()
        self.put(11, n11)
        self.put(14, n14)
        with mock.patch.object(weather, "get_fc", return_value={"time": []}):
            est = run.estimate(self.mtn)
        self.assertFalse(est["parts_saved"])
        self.assertEqual(est["sec"], te.PANORAMA_FIRST_SEC)

    def test_missing_tiles_are_added_to_forecast_only_case(self):
        v, n11, n14 = self.needed()
        self.put(11, n11[:-10])
        self.put(14, n14)
        self._save_parts(v)
        est = run.estimate(self.mtn)
        self.assertEqual(est["missing"], 10)
        self.assertAlmostEqual(est["sec"], 10 * 734 / 566 + te.PANORAMA_FORECAST_SEC)

    def test_context_is_restored_and_does_not_fetch(self):
        v, n11, n14 = self.needed()
        self._save_parts(v)
        before = weather.CTX
        with mock.patch.object(core, "get_with_retry", side_effect=AssertionError("通信")):
            run.estimate(self.mtn)
        self.assertIs(weather.CTX, before)

    def test_old_parts_are_not_counted(self):
        v, _, _ = self.needed()
        self._save_parts(v)
        with mock.patch.object(run, "PARTS_VERSION", "other"):
            self.assertFalse(run.estimate(self.mtn)["parts_saved"])

    def test_lines(self):
        est = {"zoom": {11: {"needed": 550, "missing": 130}, 14: {"needed": 16, "missing": 6}}, "needed": 566, "missing": 136,
               "tiles_sec": 136 * 734 / 566, "draw_sec": 109.0, "sec": 136 * 734 / 566 + 109.0}
        self.assertEqual(run.estimate_lines(self.mtn, est), [
            "展望図の見積もり: 大菩薩嶺(m058)",
            "標高タイル: 要る 566 枚 / 手元にある 430 枚 / 足りない 136 枚"
            "(ズーム11: 要る 550・手元 420・足りない 130、ズーム14: 要る 16・手元 10・足りない 6)",
            "時間の目安: 約 5 分(タイルの取得 約 3 分 + 予報と図づくり 約 2 分)",
            "展望地の図があるときは、少し長くなります"])


class TestEstimateCommand(NoNetwork):
    def run_main(self, argv):
        boom = AssertionError("見積もりで呼んではいけない")
        with mock.patch.object(detail, "main_single_mountain", side_effect=boom), \
                mock.patch.object(detail, "fetch_forecast", side_effect=boom), \
                mock.patch.object(run, "make_panorama", side_effect=boom), \
                mock.patch.object(core, "get_with_retry", side_effect=boom):
            return capture(detail.main, argv)

    def test_does_not_fetch_or_draw(self):
        code, out, err = self.run_main(["--mountain", "m058", "--panorama-estimate"])
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "展望図の見積もり: 大菩薩嶺(m058)")
        self.assertRegex(lines[1], r"^標高タイル: 要る (\d+) 枚 / 手元にある 0 枚 / 足りない \1 枚")
        self.assertRegex(lines[2], r"^時間の目安: 約 \d+ 分\(タイルの取得 約 \d+ 分 \+ 予報と図づくり 約 3 分\)$")
        self.assertEqual(len(lines), 4)

    def test_with_panorama_and_json_out_writes_nothing(self):
        p = os.path.join(self.tmp, "r.json")
        code, out, _ = self.run_main(["--mountain", "m058", "--panorama", "--panorama-estimate", "--json-out", p])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(p))

    def test_out_file(self):
        p = os.path.join(self.tmp, "e.txt")
        code, out, _ = self.run_main(["--mountain", "m058", "--panorama-estimate", "--out", p])
        self.assertEqual(code, 0)
        with open(p, encoding="utf-8-sig") as f:
            self.assertIn("時間の目安:", f.read())

    def test_refusals_are_same_as_panorama(self):
        code, out, _ = self.run_main(["--name", "見本の山", "--lat", "35.5", "--lon", "138.5", "--elevation", "1500",
                                      "--panorama-estimate"])
        self.assertEqual((code, out), (0, "展望図は 76 座だけです\n"))
        with mock.patch.object(run, "estimate", side_effect=RuntimeError("見本の失敗")):
            code, out, _ = self.run_main(["--mountain", "m058", "--panorama-estimate"])
        self.assertEqual((code, out), (0, "展望図は作れませんでした(見本の失敗)\n"))
        self.assertEqual(self.run_main(["--panorama-estimate"])[0], 2)
        self.assertEqual(self.run_main(["--list", "--panorama-estimate"])[0], 2)


class TestProgress(unittest.TestCase):
    def lines(self, total, sec_per_tile):
        now = [0.0]
        out = []
        p = run.TileProgress(total, clock=lambda: now[0], out=out.append)
        for _ in range(total):
            now[0] += sec_per_tile
            p.step()
        return out

    def test_every_50_tiles(self):
        out = self.lines(566, 1.0)
        self.assertEqual(len(out), 11)
        self.assertEqual(out[1], "標高タイル 100 / 566 枚(残り 約 10 分)")

    def test_every_60_seconds_when_slow(self):
        out = self.lines(30, 10.0)
        self.assertEqual([x.split(" ")[1] for x in out], ["6", "12", "18", "24"])

    def test_short_run_is_silent(self):
        self.assertEqual(self.lines(30, 1.0), [])
        self.assertEqual(self.lines(50, 1.0), [])                # 最後の 1 枚では出さない

    def test_goes_to_stderr(self):
        _, out, err = capture(lambda: te.announce("標高タイル 50 / 566 枚"))
        self.assertEqual((out, err), ("", "標高タイル 50 / 566 枚\n"))


class TestStartLine(NoNetwork):
    def test_explore(self):
        import requests
        fail = requests.exceptions.ConnectionError("offline")
        with mock.patch.object(mvp, "fetch_forecast", side_effect=fail):
            _, out, err = capture(mvp.main, ["--limit", "2"])
        self.assertEqual(err, "2 座の予報を取ります\n")            # 一部だけの実行は未実測
        self.assertNotIn("予報を取ります", out)
        with mock.patch.object(mvp, "fetch_forecast", side_effect=fail):
            _, out, err = capture(mvp.main, [])
        self.assertRegex(err, r"^76 座の予報を取ります\(時間の目安: 約 \d+ 分")
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("予報を取ります", out)

    def test_bad_arguments_have_no_start_line(self):
        _, out, err = capture(mvp.main, ["--limit", "0"])
        self.assertEqual(err, "")

    def test_diagnosis_stdout_is_unchanged(self):
        """始まりの 1 行を出さなくしたときと、標準出力が 1 文字も変わらない。"""
        import requests
        fail = requests.exceptions.ConnectionError("offline")
        with mock.patch.object(detail, "fetch_forecast", side_effect=fail):
            code, out, err = capture(detail.main, ["--mountain", "m058"])
            with mock.patch.object(te, "announce", lambda text: None):
                code2, out2, err2 = capture(detail.main, ["--mountain", "m058"])
        self.assertEqual((code, out), (code2, out2))
        self.assertEqual(out, "取得に失敗しました: offline\n")
        self.assertTrue(err.startswith("大菩薩嶺の予報を取ります"))
        self.assertEqual(err2, "")


if __name__ == "__main__":
    unittest.main()
