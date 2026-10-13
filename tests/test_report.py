"""レポート出力(--json-out の数値データ → scripts/render_report.py → HTML)のオフラインテスト。

予報の計算・スコアには関わらない。数値データの形・必須要素の検査・「解説が無くても基本ページが揃う」ことを固定する。
"""
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest

import mountain_weather_detail as detail
import mountain_weather_mvp as mvp
import report_data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_report  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "report")
EXAMPLES = os.path.join(ROOT, "skills", "yamabiyori", "references", "report_examples")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_quietly(func, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args)
    return result, out.getvalue()


def render(argv):
    """render_report.main(argv) の (終了コード, 標準出力, 標準エラー)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = render_report.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestReportData(unittest.TestCase):
    def test_hazard_class(self):
        self.assertEqual(report_data.hazard_class({"thunder": "-", "wind": "-", "cold": "-"}), "none")
        self.assertEqual(report_data.hazard_class({"thunder": "-", "wind": "注意", "cold": "-"}), "caution")
        self.assertEqual(report_data.hazard_class({"thunder": "-", "wind": "注意", "cold": "危険"}), "warning")

    def test_write_report_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "x.json")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(report_data.write_report_json(path, {"mode": "explore"}), 0)
            data = load(path)
        self.assertEqual(data["schema"], report_data.SCHEMA)
        self.assertEqual(data["mode"], "explore")

    def test_detail_json_out_requires_mountain(self):
        code, out = run_quietly(detail.main, ["--json-out", "x.json"])
        self.assertEqual(code, 2)
        self.assertIn("--mountain", out)


class TestRenderSpec(unittest.TestCase):
    def test_examples_render(self):
        for name in ("diagnosis", "plan", "explore"):
            with tempfile.TemporaryDirectory() as d:
                code, _, err = render([os.path.join(EXAMPLES, f"{name}.json"), "--out", os.path.join(d, "o.html")])
                self.assertEqual(code, 0, err)

    def test_missing_required_block_fails(self):
        spec = load(os.path.join(EXAMPLES, "diagnosis.json"))
        spec["blocks"] = [b for b in spec["blocks"] if b.get("id") != "synoptic"]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(spec, f, ensure_ascii=False)
            code, _, err = render([p, "--out", os.path.join(d, "o.html")])
        self.assertEqual(code, 2)
        self.assertIn("synoptic", err)

    def test_html_is_escaped(self):
        spec = {"mode": "diagnosis", "title": "<script>alert(1)</script>", "blocks": [
            {"type": "note", "text": "<img src=x onerror=alert(1)>"},
            {"type": "section", "id": "hero", "title": "h", "omitted": "テスト"},
            {"type": "section", "id": "synoptic", "title": "s", "omitted": "テスト"},
            {"type": "section", "id": "scores", "title": "t", "omitted": "テスト"}], "footer": "f"}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(spec, f)
            o = os.path.join(d, "o.html")
            render([p, "--out", o])
            with open(o, encoding="utf-8") as f:
                page = f.read()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)


class TestBaseline(unittest.TestCase):
    def run_data(self, name, extra=()):
        with tempfile.TemporaryDirectory() as d:
            o = os.path.join(d, "o.html")
            code, _, err = render(["--data", os.path.join(FIX, f"{name}.json"), "--out", o, *extra])
            page = ""
            if code == 0:
                with open(o, encoding="utf-8") as f:
                    page = f.read()
        return code, err, page

    def test_diagnosis_without_commentary_is_complete(self):
        code, err, page = self.run_data("diagnosis")
        self.assertEqual(code, 0, err)
        data = load(os.path.join(FIX, "diagnosis.json"))
        for day in data["days"]:                      # 全日が表に出る(上位だけに削らない)
            self.assertIn(day["date"], page)
        self.assertIn("ECMWF", page)
        self.assertIn("天気図", page)                 # 画像が無いことも明記される(黙って落とさない)

    def test_diagnosis_dates_limits_days_and_hero(self):
        """commentary の dates で診断を週末などに絞れる。指定なしの既定(全日・先頭が今日)は変えない。"""
        data = load(os.path.join(FIX, "diagnosis.json"))
        dates = ["2026-10-17", "2026-10-18"]
        with tempfile.TemporaryDirectory() as d:
            cm = os.path.join(d, "cm.json")
            with open(cm, "w", encoding="utf-8") as f:
                json.dump({"dates": dates}, f)
            o = os.path.join(d, "o.html")
            code, _, err = render(["--data", os.path.join(FIX, "diagnosis.json"), "--commentary", cm, "--out", o])
            self.assertEqual(code, 0, err)
            with open(o, encoding="utf-8") as f:
                page = f.read()
            bad = os.path.join(d, "bad.json")
            with open(bad, "w", encoding="utf-8") as f:
                json.dump({"dates": ["2000-01-01"]}, f)
            code2, _, err2 = render(["--data", os.path.join(FIX, "diagnosis.json"), "--commentary", bad, "--out", o])
        self.assertEqual(code, 0)
        by_date = {x["date"]: x["label"] for x in data["days"]}
        for dt in dates:
            self.assertIn(by_date[dt], page)
        self.assertNotIn(by_date["2026-10-16"], page)
        self.assertNotIn("今日 ", page)               # 先頭の日ではないので「今日」と書かない
        self.assertIn("2日間の登山向けスコア", page)
        # 指定した各日が先頭に同じ項目で並ぶ(日曜が「要約カードだけ」にならない)
        self.assertEqual(page.count("総合スコア"), 2)
        for dt in dates:
            day = next(x for x in data["days"] if x["date"] == dt)
            self.assertIn(day["cells"]["terrain"], page)
        self.assertNotEqual(code2, 0)                 # 当たる日が無ければ黙って全日を出さずエラー

    def _hourly_page(self, dates, with_hourly=True):
        data = load(os.path.join(FIX, "diagnosis.json"))
        if with_hourly:
            labels = {"precip": "降水確率%(地上)", "precip_mm": "降水量mm/h", "cloud": "雲量地上%", "cloud_1000m": "雲量1000m%", "cloud_2000m": "雲量2000m%",
                      "cloud_3000m": "雲量3000m%", "cloud_summit": "雲量山頂877m%", "wind10": "風地上", "wind_1000m": "風1000m",
                      "wind_2000m": "風2000m", "wind_3000m": "風3000m", "wind_summit": "風山頂877m", "upslope": "地形風"}
            rows = []
            for day in data["days"]:
                rows.append({"date": day["date"], "label": day["label"], "time": "09:00", "precip": "17", "precip_mm": "9.3", "cloud": "34",
                             "cloud_1000m": "0.0", "cloud_2000m": "1.0", "cloud_3000m": "2.0", "cloud_summit": "3.0",
                             "wind10": "1.7m/s(北)", "wind_1000m": "2.0m/s(北)", "wind_2000m": "5.6m/s(西)",
                             "wind_3000m": "8.9m/s(西)", "wind_summit": "4.4m/s(北東)", "upslope": "並 w0.1 / 並(北)"})
            data["hourly"] = {"labels": labels, "rows": rows}
        with tempfile.TemporaryDirectory() as d:
            dpath, cpath, o = (os.path.join(d, n) for n in ("d.json", "cm.json", "o.html"))
            with open(dpath, "w", encoding="utf-8") as f:
                json.dump(data, f)
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"dates": dates} if dates else {}, f)
            code, _, err = render(["--data", dpath, "--commentary", cpath, "--out", o])
            self.assertEqual(code, 0, err)
            with open(o, encoding="utf-8") as f:
                return f.read()

    def test_diagnosis_hourly_table_for_one_or_two_days(self):
        """山と日付(1〜2日)が決まっているときだけ、時間別の降水確率・雲量・風の表が載る。"""
        for dates in (["2026-10-17"], ["2026-10-17", "2026-10-18"]):
            page = self._hourly_page(dates)
            self.assertIn("時間ごとの降水確率・雲量・風", page)
            self.assertIn("雲量山頂877m%", page)
            self.assertIn("4.4m/s(北東)", page)
            self.assertIn("降水量mm/h", page)
        self.assertEqual(self._hourly_page(["2026-10-17", "2026-10-18"]).count("4.4m/s(北東)"), 2)   # 日ごとに載る
        # 3日以上・日付指定なしでは載せない(既定の動作は変えない)
        self.assertNotIn("時間ごとの降水確率・雲量・風", self._hourly_page(["2026-10-16", "2026-10-17", "2026-10-18"]))
        self.assertNotIn("時間ごとの降水確率・雲量・風", self._hourly_page(None))

    def test_diagnosis_hourly_missing_in_data_is_noted(self):
        page = self._hourly_page(["2026-10-17"], with_hourly=False)
        self.assertIn("時間ごとの降水確率・雲量・風は、この数値データに含まれていません", page)

    def test_diagnosis_default_hero_lists_best_days(self):
        """日付の指定が無いときは、先頭が「今日」ではなくスコアの高い日の候補。hero:"today" で従来の表示に戻せる。"""
        data = load(os.path.join(FIX, "diagnosis.json"))
        future = [d for d in data["days"] if d["date"] > data["generated"][:10]]   # 生成日(今日)は候補にしない
        best = sorted(future, key=lambda d: (-d["score"], d["date"]))[:3]
        with tempfile.TemporaryDirectory() as d:
            out = {}
            for name, cm in (("default", {}), ("today", {"hero": "today"})):
                cpath, o = os.path.join(d, name + ".json"), os.path.join(d, name + ".html")
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump(cm, f)
                code, _, err = render(["--data", os.path.join(FIX, "diagnosis.json"), "--commentary", cpath, "--out", o])
                self.assertEqual(code, 0, err)
                with open(o, encoding="utf-8") as f:
                    out[name] = f.read()
        self.assertIn("行く日の候補", out["default"])
        self.assertNotIn("今日 ", out["default"])
        self.assertEqual(out["default"].count("総合スコア"), 3)
        for day in best:
            self.assertIn(f"{day['score']:.1f}", out["default"])
        m = re.match(r"^\d{4}-(\d\d)-(\d\d)\((.)\)$", data["days"][0]["label"])
        today_md = f"{int(m.group(1))}/{int(m.group(2))}({m.group(3)})"
        self.assertNotIn(f"<h3>{today_md} —", out["default"])    # 今日(生成日)は候補のパネルに入らない
        self.assertIn("今日 ", out["today"])
        self.assertNotIn("行く日の候補", out["today"])

    def test_diagnosis_confidence_gap_is_noted_not_zero(self):
        """確信度を出せない日(アンサンブルの範囲外)は、0と表示せず、出していないと明記する。"""
        data = load(os.path.join(FIX, "diagnosis.json"))
        last = data["days"][-1]
        data["confidence"] = [c for c in data["confidence"] if c["date"] != last["date"]]
        with tempfile.TemporaryDirectory() as d:
            dpath, o = os.path.join(d, "d.json"), os.path.join(d, "o.html")
            with open(dpath, "w", encoding="utf-8") as f:
                json.dump(data, f)
            code, _, err = render(["--data", dpath, "--out", o])
            self.assertEqual(code, 0, err)
            with open(o, encoding="utf-8") as f:
                page = f.read()
        self.assertIn("確信度を出していません", page)

    def _explore_page(self, cm, extra=()):
        with tempfile.TemporaryDirectory() as d:
            cpath, o = os.path.join(d, "cm.json"), os.path.join(d, "o.html")
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(cm, f)
            code, _, err = render(["--data", os.path.join(FIX, "explore.json"), "--commentary", cpath, "--out", o, *extra])
            page = ""
            if code == 0:
                with open(o, encoding="utf-8") as f:
                    page = f.read()
        return code, err, page

    def test_explore_picks_and_avoid_tables(self):
        """picks / avoid を指定すると、その山の日ごとの数値表が先頭に出る(数値は自動)。指定なしの既定は変えない。"""
        data = load(os.path.join(FIX, "explore.json"))
        day = next(d for d in data["days"] if d["rows"])
        rows = day["rows"]
        pick, avoid = rows[0]["mountain"].rsplit("(", 1)[0], rows[1]["mountain"].rsplit("(", 1)[0]
        code, err, page = self._explore_page({"dates": [day["date"]], "picks": [pick], "avoid": [avoid]})
        self.assertEqual(code, 0, err)
        self.assertIn("おすすめの山", page)
        self.assertIn("避けたほうがよい山", page)
        self.assertIn(f"{rows[0]['score']:.1f}", page)
        self.assertNotIn("上位: ", page)                       # おすすめと食い違う「上位」の列挙は外す
        code, err, base = self._explore_page({"dates": [day["date"]]})
        self.assertEqual(code, 0, err)
        self.assertNotIn("おすすめの山", base)
        self.assertIn("上位: ", base)                           # 既定は従来どおり

    def test_explore_picks_unknown_name_fails(self):
        code, err, _ = self._explore_page({"picks": ["存在しない山"]})
        self.assertNotEqual(code, 0)
        self.assertIn("存在しない山", err)

    def test_diagnosis_single_day_page_is_tidy(self):
        """1日だけ指定したページ: 右側は次の2日、要約は1日分の文、確信度の対象外を明記、MSM範囲内の脚注。"""
        data = load(os.path.join(FIX, "diagnosis.json"))
        by = {x["date"]: x for x in data["days"]}
        with tempfile.TemporaryDirectory() as d:
            cpath, o = os.path.join(d, "cm.json"), os.path.join(d, "o.html")
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"dates": ["2026-10-13"]}, f)
            code, _, err = render(["--data", os.path.join(FIX, "diagnosis.json"), "--commentary", cpath, "--out", o])
            self.assertEqual(code, 0, err)
            with open(o, encoding="utf-8") as f:
                page = f.read()
        self.assertEqual(page.count('class="nextcard"'), 2)                      # 右半分が空白にならない
        for nxt in ("2026-10-14", "2026-10-15"):
            self.assertIn(f"{by[nxt]['score']:.1f}", page)
        self.assertNotIn("1日間で最もスコアが高い", page)
        self.assertIn("のスコアは", page)
        self.assertIn("2日先以降の日だけ出します", page)                       # 確信度の表が無い理由
        self.assertNotIn("9日目以降などMSM範囲外", page)
        self.assertIn("気象庁MSM(約3日先まで)の範囲内", page)
        self.assertNotIn("mm%", page)                                           # 「%」は数字の直後(末尾に付けない)

    def test_explore_without_commentary_has_all_columns(self):
        code, err, page = self.run_data("explore")
        self.assertEqual(code, 0, err)
        for col in ("アクセス", "稜線風速", "雷CAPE", "地形", "危険信号"):
            self.assertIn(col, page)

    def test_explore_days_and_top_options(self):
        _, _, p3 = self.run_data("explore", ["--days", "3"])
        _, _, p0 = self.run_data("explore", ["--days", "0"])
        self.assertLess(p3.count('class="tab'), p0.count('class="tab'))

    def test_commentary_appears_and_is_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.json")
            with open(c, "w", encoding="utf-8") as f:
                json.dump({"overview": "高気圧<b>に覆われる", "synoptic": {"analysis": [{"tag": "気圧配置", "text": "移動性高気圧"}]}}, f, ensure_ascii=False)
            o = os.path.join(d, "o.html")
            code, _, err = render(["--data", os.path.join(FIX, "diagnosis.json"), "--commentary", c, "--out", o])
            with open(o, encoding="utf-8") as f:
                page = f.read()
        self.assertEqual(code, 0, err)
        self.assertIn("移動性高気圧", page)
        self.assertNotIn("<b>に覆われる", page)

    def test_rejects_wrong_data(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"mode": "explore"}, f)
            code, _, err = render(["--data", p, "--out", os.path.join(d, "o.html")])
        self.assertEqual(code, 2)
        self.assertIn("schema", err)

    def test_spec_and_data_are_exclusive(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                render(["--out", "x.html"])


if __name__ == "__main__":
    unittest.main()
