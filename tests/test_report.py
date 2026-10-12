"""レポート出力(--json-out の数値データ → scripts/render_report.py → HTML)のオフラインテスト。

予報の計算・スコアには関わらない。数値データの形・必須要素の検査・「解説が無くても基本ページが揃う」ことを固定する。
"""
import contextlib
import copy
import io
import json
import os
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
