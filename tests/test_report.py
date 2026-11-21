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

    def _hourly_page(self, dates, with_hourly=True, cloud_layers=False):
        """cloud_layers=False は 2026-11-14 より前の JSON(低層雲・中層雲・高層雲の列が無い)を再現する。"""
        data = load(os.path.join(FIX, "diagnosis.json"))
        if with_hourly:
            labels = {"precip": "降水確率%(地上)", "precip_mm": "降水量mm/h", "cloud": "総雲量%", "cloud_1000m": "雲量1000m%", "cloud_2000m": "雲量2000m%",
                      "cloud_3000m": "雲量3000m%", "cloud_summit": "雲量山頂877m%", "wind10": "風地上", "wind_1000m": "風1000m",
                      "wind_2000m": "風2000m", "wind_3000m": "風3000m", "wind_summit": "風山頂877m", "upslope": "地形風"}
            rows = []
            for day in data["days"]:
                rows.append({"date": day["date"], "label": day["label"], "time": "09:00", "precip": "17", "precip_mm": "9.3", "cloud": "34",
                             "cloud_1000m": "0.0", "cloud_2000m": "1.0", "cloud_3000m": "2.0", "cloud_summit": "3.0",
                             "wind10": "1.7m/s(北)", "wind_1000m": "2.0m/s(北)", "wind_2000m": "5.6m/s(西)",
                             "wind_3000m": "8.9m/s(西)", "wind_summit": "4.4m/s(北東)", "upslope": "並 w0.1 / 並(北)"})
                if cloud_layers:
                    rows[-1].update({"cloud_low": "0.0", "cloud_mid": "12", "cloud_high": "87"})
            if cloud_layers:
                labels.update({"cloud_low": "低層雲%", "cloud_mid": "中層雲%", "cloud_high": "高層雲%"})
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

    def test_diagnosis_hourly_cloud_layers(self):
        """総雲量に加え、低層雲・中層雲・高層雲の列が載る。古い JSON(列が無い)でもエラーにならず、その列だけ出さない。"""
        page = self._hourly_page(["2026-10-17"], cloud_layers=True)
        for label in ("総雲量%", "低層雲%", "中層雲%", "高層雲%"):
            self.assertIn(label, page)
        self.assertIn("87", page)
        self.assertNotIn("雲量地上", page)
        old = self._hourly_page(["2026-10-17"])
        self.assertIn("総雲量%", old)
        self.assertNotIn("高層雲%", old)

    def test_cloud_sea_deck_labels(self):
        """雲海の層の表示: 低層雲は「低い雲の層」。旧版の 'surface'(総雲量)は「地表付近」と書かない。"""
        import report_baseline as rb
        self.assertEqual(rb.deck_label("低層雲"), "低い雲の層(低層雲)")
        self.assertNotIn("地表付近", rb.deck_label("surface"))
        self.assertIn("総雲量", rb.deck_label("surface"))
        self.assertEqual(rb.deck_label("1000m帯"), "標高1000m付近")
        o = {"summit_cloud_pct": 3, "deck_cloud_pct": 87, "deck_humidity_pct": None}
        self.assertEqual(rb.cloud_sea_text(dict(o, deck_band="低層雲")),
                         "山頂付近は雲3%(ほぼ晴れ) / 下の低い雲の層(低層雲)は雲87%")
        legacy = rb.cloud_sea_text(dict(o, deck_band="surface"))
        self.assertNotIn("下の", legacy)
        self.assertIn("総雲量87%", legacy)

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


class TestPanorama(unittest.TestCase):
    """--panorama-html / --panorama-viewpoint-html: 展望図を1枚だけ、総評のすぐあとに iframe(srcdoc)で埋め込む。"""

    LAND = ("@media (orientation:landscape) and (max-height:500px){\n body.narrow .ctl{display:flex;gap:6px}\n"
            " body.narrow .ctl .stbar,body.narrow .ctl .navbar{margin:0 0 3px}\n main.p39{padding:4px 8px 12px}\n}\n")
    SUMMIT = ('<!doctype html><html lang="ja"><head><title>大菩薩嶺からの展望と雲(2026-11-22)</title><style>' + LAND +
              '</style></head><body class="narrow"><main class="p39">\n<div id="frame">山頂の図</div>'
              '<p class="intro">視点: 山頂(国土地理院の山頂の点)。</p>\r\n<script>var a=1&&2;</script></main></body></html>')
    VIEW = ('<!doctype html><html lang="ja"><head><title>大菩薩嶺・雷岩からの展望と雲(2026-11-22)</title></head>'
            '<body><main class="p39"><div id="frame">展望地の図</div>'
            "<p>視点: 雷岩(OpenStreetMap の展望地。山頂の南 276m)。'x' &amp;</p></main></body></html>")
    NONAME = VIEW.replace("雷岩", "名前のない展望地")
    COMMENTARY = {"overview": "総評の文。", "alert": {"heading": "注意", "body": "注意の文。"}}

    def _write(self, d, name, text):
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        return path

    def _render(self, extra, base=None, commentary=True, view=None):
        """extra の {S}・{V} を山頂・展望地の仮の展望図のパスに置き換えて変換する。"""
        with tempfile.TemporaryDirectory() as d:
            s = self._write(d, "m.html", self.SUMMIT)
            v = self._write(d, "mv.html", view or self.VIEW)
            c = self._write(d, "c.json", json.dumps(self.COMMENTARY, ensure_ascii=False))
            args = [a.replace("{S}", s).replace("{V}", v) for a in extra]
            o = os.path.join(d, "o.html")
            if base is None:
                base = ["--data", os.path.join(FIX, "diagnosis.json")] + (["--commentary", c] if commentary else [])
            code, _, err = render(base + ["--out", o] + args)
            page = ""
            if code == 0:
                with open(o, encoding="utf-8", newline="") as f:
                    page = f.read()
        return code, page, err

    @staticmethod
    def srcdocs(page):
        import html as html_lib
        return [html_lib.unescape(x) for x in re.findall(r"<iframe[^>]* srcdoc='([^']*)'", page)]

    def test_without_arguments_no_section(self):
        code, page, err = self._render([])
        self.assertEqual(code, 0, err)
        self.assertNotIn("<iframe", page)
        self.assertNotIn("からの展望", page)
        self.assertNotIn("yamabiyori-panorama", page)  # 展望図の枠のスクリプトも足さない

    def test_without_panorama_output_is_unchanged(self):
        spec = load(os.path.join(EXAMPLES, "diagnosis.json"))
        a = render_report.Renderer(copy.deepcopy(spec), EXAMPLES, False).render()
        b = render_report.Renderer(copy.deepcopy(spec), EXAMPLES, False, None).render()
        self.assertEqual(a, b)

    def test_summit_only(self):
        code, page, err = self._render(["--panorama-html", "{S}"])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count("<iframe"), 1)
        self.assertIn("<h2>山頂からの展望</h2>", page)
        self.assertIn("山頂の図", self.srcdocs(page)[0])

    def test_both_embeds_only_viewpoint(self):
        code, page, err = self._render(["--panorama-html", "{S}", "--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count("<iframe"), 1)
        docs = self.srcdocs(page)
        self.assertEqual(len(docs), 1)
        self.assertIn("展望地の図", docs[0])
        self.assertNotIn("山頂の図", page)
        self.assertNotIn("山頂からの展望", page)

    def test_viewpoint_only_is_allowed(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count("<iframe"), 1)

    def test_heading_has_viewpoint_name(self):
        code, page, err = self._render(["--panorama-html", "{S}", "--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>雷岩(展望地)からの展望</h2>", page)  # 読み仮名は付けない
        self.assertIn("11/22(日)の予報にもとづく展望図", page)

    def test_heading_without_viewpoint_name(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], view=self.NONAME)
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>展望地からの展望</h2>", page)

    def test_section_right_after_overview(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        i_over, i_pano = page.index("総評の文。"), page.index('<div class="section" id="panorama">')
        self.assertLess(i_over, i_pano)
        between = page[i_over:i_pano]
        self.assertNotIn('class="section', between)  # 総評と展望の節のあいだに、ほかの節が無い
        self.assertNotIn("注意の文。", between)
        self.assertLess(i_pano, page.index("注意の文。"))
        self.assertLess(i_pano, page.index("気圧配置と実況"))

    def test_section_without_overview_follows_summary(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], commentary=False)
        self.assertEqual(code, 0, err)
        i_sum, i_pano = page.index('<p class="summary">'), page.index('<div class="section" id="panorama">')
        self.assertLess(i_sum, i_pano)
        self.assertNotIn('class="section', page[i_sum:i_pano])

    def test_captions_hidden_and_body_unchanged(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        doc = self.srcdocs(page)[0]
        head, body = doc.split("</head>", 1)
        self.assertEqual(body, self.VIEW.split("</head>", 1)[1])  # 本文(図・ボタン・説明)は1文字も変えない
        self.assertIn("main.p39>#frame~*{display:none!important}", head)  # 図の下の説明は出さない
        self.assertNotIn("\n<p", page)  # 改行は文字参照で入る

    def test_frame_height_follows_content(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        doc = self.srcdocs(page)[0]
        self.assertIn("Object.defineProperty(window,'innerHeight'", doc)  # 図の高さは枠ではなくレポートの画面の高さで決める
        self.assertIn("yamabiyori-panorama", doc)
        self.assertIn("-height", doc)
        self.assertIn("yamabiyori-panorama", page.split("</iframe>")[1])  # 親が受けて枠の高さを合わせる

    def test_landscape_rules_follow_report_screen(self):
        code, page, err = self._render(["--panorama-html", "{S}"])
        self.assertEqual(code, 0, err)
        doc = self.srcdocs(page)[0]
        self.assertNotIn("@media (orientation:landscape)", doc)
        self.assertIn("html.yb-land body.narrow .ctl{display:flex;gap:6px}", doc)
        self.assertIn("html.yb-land body.narrow .ctl .stbar,html.yb-land body.narrow .ctl .navbar{margin:0 0 3px}", doc)
        self.assertIn("html.yb-land main.p39{padding:4px 8px 12px}", doc)

    def test_landscape_block_of_real_panorama_is_found(self):
        from panorama import draw
        self.assertIn(render_report.PANORAMA_LANDSCAPE_MEDIA, draw.CSS39)
        out = render_report._landscape_to_class("<style>" + draw.CSS39 + "</style>")
        self.assertNotIn("orientation:landscape", out)
        self.assertIn("html.yb-land body.narrow .ctl button small{display:none}", out)
        self.assertEqual(out.count("{"), out.count("}"))

    def test_background_matches_report(self):
        css = render_report.CSS_PATH.read_text(encoding="utf-8")
        light, dark = render_report.report_bg_colors(css)
        self.assertEqual((light, dark), ("#eef1ee", "#121815"))
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        doc = self.srcdocs(page)[0]
        self.assertIn(f"html,body{{background:{light}!important}}", doc)
        # ダークの地の色は report.css と同じく、html に data-theme="light" があるときは使わない
        self.assertIn('@media (prefers-color-scheme: dark){html:not([data-theme="light"]),'
                      f'html:not([data-theme="light"]) body{{background:{dark}!important}}}}', doc)

    def test_theme_attribute_is_passed_to_frame(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        doc = self.srcdocs(page)[0]
        parent = page.split("</iframe>")[1]
        self.assertIn("theme:document.documentElement.getAttribute('data-theme')", parent)
        self.assertIn("if(t==='light')de.setAttribute('data-theme','light')", doc)

    def test_no_height_message_when_override_fails(self):
        # innerHeight の書き換えが効かないブラウザでは高さを送らない(枠は 460px のまま。循環させない)
        js = render_report.PANORAMA_CHILD_JS
        self.assertIn("OK=(window.innerHeight===VH);", js)
        self.assertIn("if(!b||!OK)return;", js)
        self.assertIn("height:460px", self._render(["--panorama-viewpoint-html", "{V}"])[1])

    def test_messages_are_not_lost_when_frame_loads_first(self):
        # 枠の中が先に読み終わって高さの知らせを取りこぼしても、親の知らせを受けたら送り直す
        self.assertIn("last=0;post()", render_report.PANORAMA_CHILD_JS)
        # 親は読み終わりと data-theme の変化でも知らせ直す
        js = render_report.PANORAMA_PARENT_JS
        self.assertIn("document.addEventListener('DOMContentLoaded',all)", js)
        self.assertIn("attributeFilter:['data-theme']", js)

    def test_srcdoc_is_single_quoted_and_short(self):
        # srcdoc は ' で囲み、& と ' と改行だけを文字参照にする(< > " はそのまま)
        self.assertEqual(render_report.srcdoc_value("<a href=\"x\">'&amp;'</a>\r\n"),
                         "<a href=\"x\">&#39;&amp;amp;&#39;</a>&#13;&#10;")
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertIn("srcdoc='<!doctype html>", page)
        self.assertNotIn("srcdoc=\"", page)
        self.assertNotIn("&lt;", page.split("srcdoc='", 1)[1].split("'", 1)[0])

    def test_viewpoint_name_is_not_double_escaped(self):
        view = self.VIEW.replace("雷岩", "雷岩 &amp; 東")
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], view=view)
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>雷岩 &amp; 東(展望地)からの展望</h2>", page)

    def test_works_with_spec_json(self):
        code, page, err = self._render(["--panorama-html", "{S}"], [os.path.join(EXAMPLES, "diagnosis.json")])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count("<iframe"), 1)

    def test_missing_file_fails(self):
        code, _, err = self._render(["--panorama-html", "no_such_panorama.html"])
        self.assertEqual(code, 2)
        self.assertIn("エラー", err)


if __name__ == "__main__":
    unittest.main()
