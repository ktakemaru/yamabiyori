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
import unittest.mock

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

    def _tied_data(self, n=20, score=98.6, hazards=None):
        """同点 n 座の1日(作った値。稜線の風は m001 がいちばん弱い)。hazards: {番号: 危険信号の段階}。"""
        data = load(os.path.join(FIX, "explore.json"))
        day = copy.deepcopy(next(d for d in data["days"] if d["rows"]))
        base = day["rows"][0]
        rows = []
        for i in range(1, n + 1):
            r = copy.deepcopy(base)
            level = (hazards or {}).get(i, "-")
            r.update(mountain=f"見本{i:02d}山(1000m)", id=f"m{i:03d}", score=score, ridge_wind_ms=float(i), rank=i,
                     hazards={"thunder": "-", "wind": level, "cold": "-", "any": level != "-"})
            r["hazard_class"] = report_data.hazard_class(r["hazards"])
            r["cells"] = dict(r["cells"], hazard="-" if level == "-" else f"⚠強風{level}")
            rows.append(r)
        day["rows"] = rows
        day["all"] = [{"mountain": r["mountain"], "region": r["region"], "score": r["score"],
                       "hazard_class": r["hazard_class"], "id": r["id"]} for r in rows]
        data["days"] = [day]
        return data, day

    def _page(self, data, cm=None, *extra):
        with tempfile.TemporaryDirectory() as d:
            p, c, o = (os.path.join(d, x) for x in ("d.json", "c.json", "o.html"))
            for path, obj in ((p, data), (c, cm or {})):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False)
            code, _, err = render(["--data", p, "--commentary", c, "--out", o, *extra])
            self.assertEqual(code, 0, err)
            with open(o, encoding="utf-8") as f:
                return f.read()

    @staticmethod
    def _row(page, name, start=0):
        """その山の行(tr)の文字を、セルごとに取り出す(start 以降の最初の行)。"""
        for m in re.finditer(r"<tr>(.*?)</tr>", page[start:], re.S):
            if name in m.group(1):
                return [re.sub(r"<[^>]+>", "", c) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)]
        return None

    def test_pick_outside_top_is_added_to_ranking(self):
        """上位の外のおすすめの山が、区切りの行の下に、並びの番号と★つきで出る。避ける山も印つき。"""
        data, _ = self._tied_data(hazards={2: "注意"})
        page = self._page(data, {"picks": ["見本18山"], "avoid": ["見本03山", "見本19山"]}, "--ranking-layout", "single")
        at = page.index("日ごとのランキング")
        ranking = page[at:]
        self.assertEqual(ranking.count("★ 見本18山"), 1)
        self.assertLess(ranking.index("おすすめの山(上位 15 座の外)"), ranking.index("★ 見本18山"))
        self.assertLess(ranking.index("★ 見本18山"), ranking.index("避ける山(上位 15 座の外)"))
        self.assertEqual(self._row(page, "★ 見本18山", at)[0], "17")           # 見本02山(注意)は最後の20番
        self.assertEqual(self._row(page, "見本19山", at)[0], "18")
        self.assertIn("避ける", self._row(page, "見本03山", at)[1])              # 上位の中でも印が付く
        self.assertEqual(ranking.count("凡例: ★ = おすすめの山、「避ける」= 避ける山。"), 1)
        # おすすめ・避ける表の「順位」: 同点は同じ順位
        head = page[:at]
        self.assertIn("<th", head[head.index(">順位<") - 40:head.index(">順位<")])
        self.assertEqual(self._row(head, "見本18山")[2], "1位(同点 20 座)")
        self.assertEqual(self._row(head, "見本03山")[2], "1位(同点 20 座)")

    def test_pick_inside_top_has_no_extra_row(self):
        data, _ = self._tied_data()
        page = self._page(data, {"picks": ["見本01山"]}, "--ranking-layout", "single")
        ranking = page[page.index("日ごとのランキング"):]
        self.assertEqual(ranking.count("★ 見本01山"), 1)
        self.assertNotIn("座の外", ranking)
        self.assertNotIn("避ける", ranking)                                      # avoid が無ければ凡例にも出さない

    def test_place_counts_ties_once(self):
        data, day = self._tied_data()
        for i in (0, 1):
            day["rows"][i]["score"] = day["all"][i]["score"] = 99.0
        day["rows"][5]["score"] = day["all"][5]["score"] = 90.0
        page = self._page(data, {"picks": ["見本01山", "見本06山", "見本10山"]})
        head = page[:page.index("日ごとのランキング")]
        self.assertEqual([self._row(head, f"見本{n}山")[2] for n in ("01", "10", "06")],
                         ["1位(同点 2 座)", "3位(同点 17 座)", "20位"])

    def test_pick_below_threshold_is_still_listed(self):
        data, day = self._tied_data()
        day["rows"] = day["rows"][:3]
        day["all"][19].update(score=30.0, hazard_class="warning")
        page = self._page(data, {"picks": ["見本20山"]}, "--ranking-layout", "single")
        at = page.index("日ごとのランキング")
        row = self._row(page, "★ 見本20山", at)
        self.assertEqual((row[0], row[4], row[5]), ("-", "30.0", "⚠警戒以上"))
        self.assertEqual(self._row(page[:at], "見本20山")[2], "基準点未満")

    def test_tie_line_and_nothing_else_without_picks(self):
        """picks の無いレポートは、同点の1行のほかは変わらない(見出し・印・順位の列・凡例は出ない)。"""
        import report_baseline
        data, _ = self._tied_data()
        page = self._page(data, None, "--ranking-layout", "single")
        line = '<p class="summary">※ 98.6点が 20 座あります。同点の中は、危険信号の軽い順・稜線の風が弱い順に並べています。</p>'
        self.assertEqual(page.count(line), 1)
        for word in ("★", "凡例", "順位", "座の外"):
            self.assertNotIn(word, page)
        with unittest.mock.patch.object(report_baseline, "tie_note", lambda rows: None):
            self.assertEqual(page.replace(line, ""), self._page(data, None, "--ranking-layout", "single"))

    def test_old_json_order_is_fixed_in_report(self):
        """同点が登録順のままの JSON(2026-11-24 より前)でも、レポートでは危険信号の軽い順に並ぶ。数値は変えない。"""
        data, day = self._tied_data(n=3, hazards={1: "危険", 2: "注意"})
        before = copy.deepcopy(data)
        page = self._page(data, None, "--ranking-layout", "single")
        ranking = page[page.index("日ごとのランキング"):]
        self.assertEqual(re.findall(r"見本0[123]山", ranking[ranking.index("<tbody>"):]), ["見本03山", "見本02山", "見本01山"])
        self.assertEqual(data, before)

    @staticmethod
    def _tables(page):
        """ランキングの節の、表ごとの山の名前(印つき)。"""
        ranking = page[page.index("日ごとのランキング"):]
        return [[re.sub(r"<span.*", "", m) for m in re.findall(r'<td class="strong">(.*?)</td>', t, re.S)]
                for t in re.findall(r"<tbody>(.*?)</tbody>", ranking, re.S)]

    def _split_data(self):
        """20座: 1・2 は危険、3〜8 は警戒、9・10 は注意、ほかは危険信号なし。"""
        levels = {1: "危険", 2: "危険", 9: "注意", 10: "注意", **{i: "警戒" for i in range(3, 9)}}
        data, day = self._tied_data(hazards=levels)
        return data, day

    def test_layout_default_is_split(self):
        data, _ = self._split_data()
        self.assertEqual(self._page(data), self._page(data, None, "--ranking-layout", "split"))
        self.assertNotEqual(self._page(data), self._page(data, None, "--ranking-layout", "single"))

    def test_split_tables(self):
        data, day = self._split_data()
        day["rows"][1]["score"] = day["all"][1]["score"] = 99.9           # 危険どうしはスコアの高い順
        page = self._page(data)
        go, skip = self._tables(page)
        self.assertEqual(go, [f"見本{i:02d}山" for i in list(range(11, 21)) + [9, 10]])
        self.assertEqual(skip, ["見本02山", "見本01山", "見本03山", "見本04山", "見本05山"])      # 危険の重い順 → スコア → ID、5座まで
        self.assertIn("スコアが高くても、危険信号が警戒以上の山です(危険信号はスコアとは別の警告)", page)
        self.assertIn("全12座。危険信号なし・注意の山", page)
        self.assertIn("全8座のうち5座", page)
        at = page.index("<b>見送る山")
        self.assertEqual(self._row(page, "見本02山", at)[0], "1")          # 番号は表の中で数える
        self.assertEqual(self._row(page, "見本11山", page.index("<b>行ける山"))[0], "1")

    def test_split_options(self):
        data, _ = self._split_data()
        go, skip = self._tables(self._page(data, None, "--top", "3", "--skip-top", "2"))
        self.assertEqual((len(go), len(skip)), (3, 2))
        go, skip = self._tables(self._page(data, None, "--top", "0", "--skip-top", "0"))
        self.assertEqual((len(go), len(skip)), (12, 8))

    def test_single_has_one_table(self):
        data, _ = self._split_data()
        page = self._page(data, None, "--ranking-layout", "single")
        self.assertEqual(len(self._tables(page)), 1)
        for word in ("行ける山", "見送る山"):
            self.assertNotIn(word, page)

    def test_split_marks_go_to_their_own_table(self):
        data, _ = self._split_data()
        page = self._page(data, {"picks": ["見本20山"], "avoid": ["見本08山", "見本01山"]}, "--top", "3")
        go, skip = self._tables(page)
        self.assertEqual(go[-1], "★ 見本20山")
        self.assertEqual((len(go), len(skip)), (4, 6))
        self.assertEqual(skip[-1], "見本08山")
        self.assertIn("おすすめの山(上位 3 座の外)", page)
        self.assertIn("避ける山(上位 5 座の外)", page)
        self.assertIn("それぞれの表の中の並びの番号", page)
        self.assertEqual(page.count("凡例: "), 1)

    def test_split_day_without_go(self):
        data, _ = self._tied_data(n=3, hazards={1: "警戒", 2: "警戒", 3: "危険"})
        page = self._page(data)
        self.assertIn("この日は、危険信号が警戒未満の山がありません", page)
        self.assertEqual(self._tables(page), [["見本03山", "見本01山", "見本02山"]])
        self.assertNotIn("上位: ", page)                                      # 見送る山を「上位」に挙げない

    def test_split_day_without_skip(self):
        data, _ = self._tied_data(n=3)
        page = self._page(data)
        self.assertEqual(len(self._tables(page)), 1)
        self.assertNotIn("<b>見送る山", page)

    def test_split_does_not_change_summary_and_pick_tables(self):
        data, _ = self._split_data()
        cm = {"picks": ["見本20山"], "avoid": ["見本01山"]}
        a, b = self._page(data, cm), self._page(data, cm, "--ranking-layout", "single")
        self.assertEqual(a[:a.index("日ごとのランキング")], b[:b.index("日ごとのランキング")])

    def test_tie_note_in_json_is_used(self):
        data, day = self._tied_data()
        day["tie_note"] = "※ 見本の1行"
        self.assertIn("※ 見本の1行", self._page(data))

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
    """--panorama-html / --panorama-viewpoint-html: 展望図を1枚だけ、総評(と注意)のあとに、iframe を使わず直接入れる。"""

    CSS = (':root{--bg:#f7f8fa;--fg:#1d2733;--B1:hsl(208 29% 38%)}\n'
           '@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#11161c}}\n'
           ':root[data-theme="dark"]{--bg:#11161c}\n'
           "*{box-sizing:border-box}\nbody{margin:0;background:var(--bg);color:var(--fg)}\n"
           "main{max-width:1100px;margin:0 auto;padding:16px}\n"
           "path.near{fill:url(#hatch)} path.B1{fill:var(--B1)}\n#frame{position:relative}\n"
           "body.narrow #vp svg{width:100%}\nbody.wide .ctl{display:flex}\n"
           "@media (orientation:landscape) and (max-height:500px){\n body.narrow .ctl{display:flex;gap:6px}\n"
           " main.p39{padding:4px 8px 12px}\n}\n")
    JS = ("(function(){\nvar D=JSON.parse(document.getElementById('p39data').textContent);\n"
          "var svg=document.querySelector('#vp svg'),vp=document.getElementById('vp'),body=document.body;\n"
          "var W=window.innerWidth||document.documentElement.clientWidth;var h=location.hash;\n"
          "document.querySelectorAll('.stbar button[data-st]');document.addEventListener('click',function(){});\n"
          "window.__p39=D;\n})();\n")
    SUMMIT = ('<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>大菩薩嶺からの展望と雲(2026-11-22)</title>'
              '<style>' + CSS + '</style></head><body class="narrow"><main class="p39">\n<div class="ctl"><div class="stbar">'
              '<button data-st="h08">8時</button></div></div><div id="frame"><div id="vp"><svg><defs>'
              '<pattern id="hatch"></pattern><clipPath id="cc1"><circle/></clipPath></defs><g id="bw1">山頂の図</g>'
              '<path d="M0 0" class="near"/><path d="M0 0" class="B1" clip-path="url(#cc1)"/><use href="#bw1"/></svg></div>'
              '<div class="credit39">出典</div></div>'
              '<p class="intro">視点: 山頂(国土地理院の山頂の点)。</p>\r\n<script type="application/json" id="p39data">{"a":1}</script>'
              '<script>' + JS + '</script></main></body></html>')
    VIEW = (SUMMIT.replace("大菩薩嶺からの", "大菩薩嶺・雷岩からの").replace("山頂の図", "展望地の図")
            .replace("視点: 山頂(国土地理院の山頂の点)。",
                     "視点: 雷岩(OpenStreetMap の展望地。山頂の南 276m)。'x' &amp;"))
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
    def section(page):
        """展望の節(直接入れた CSS・本文・スクリプトを含む)。無ければ ""。"""
        i = page.find('<div class="section" id="panorama">')
        if i < 0:
            return ""
        j = page.find('<div class="section"', i + 10)
        return page[i:j if j > 0 else len(page)]

    @staticmethod
    def styles(sec):
        return "".join(re.findall(r"<style>(.*?)</style>", sec, re.S))

    @staticmethod
    def selectors(css):
        """CSS の指定(@media の中も)を全部取り出す。"""
        out = []
        for head in re.findall(r"([^{}]+)\{", css):
            head = head.strip()
            if not head.startswith("@"):
                out += [x.strip() for x in head.split(",")]
        return out

    def assert_ids_ok(self, page):
        ids = re.findall(r'\bid="([^"]+)"', page)
        dup = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dup, [], "同じ id が 2 つ以上ある")
        refs = set(re.findall(r"url\(#([^)]+)\)", page)) | set(re.findall(r'href="#([^"]+)"', page))
        self.assertEqual(sorted(refs - set(ids)), [], "指す先の無い url(#…)・href=\"#…\" がある")

    # ---- 出す・出さない、位置、見出し ----
    def test_without_arguments_no_section(self):
        code, page, err = self._render([])
        self.assertEqual(code, 0, err)
        self.assertNotIn("からの展望", page)
        self.assertNotIn("ybp", page)  # 展望図の CSS・スクリプトも足さない

    def test_without_panorama_output_is_unchanged(self):
        spec = load(os.path.join(EXAMPLES, "diagnosis.json"))
        a = render_report.Renderer(copy.deepcopy(spec), EXAMPLES, False).render()
        b = render_report.Renderer(copy.deepcopy(spec), EXAMPLES, False, None).render()
        self.assertEqual(a, b)

    def test_summit_only(self):
        code, page, err = self._render(["--panorama-html", "{S}"])
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>山頂からの展望</h2>", page)
        self.assertIn("山頂の図", self.section(page))
        self.assertEqual(page.count('id="ybp-root"'), 1)

    def test_both_embeds_only_viewpoint(self):
        code, page, err = self._render(["--panorama-html", "{S}", "--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count('id="ybp-root"'), 1)
        self.assertIn("展望地の図", page)
        self.assertNotIn("山頂の図", page)
        self.assertNotIn("山頂からの展望", page)

    def test_viewpoint_only_is_allowed(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count('id="ybp-root"'), 1)

    def test_heading_has_viewpoint_name(self):
        code, page, err = self._render(["--panorama-html", "{S}", "--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>雷岩(展望地)からの展望</h2>", page)  # 読み仮名は付けない
        self.assertIn("11/22(日)の予報にもとづく展望図", page)

    def test_heading_without_viewpoint_name(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], view=self.NONAME)
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>展望地からの展望</h2>", page)

    def test_section_after_overview_and_alert(self):
        # 順番は 総評 → 注意 → 展望(安全に関わる注意を図の下に回さない)
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        i_over, i_alert = page.index("総評の文。"), page.index("注意の文。")
        i_pano = page.index('<div class="section" id="panorama">')
        self.assertLess(i_over, i_alert)
        self.assertLess(i_alert, i_pano)
        self.assertNotIn('class="section', page[i_over:i_pano])  # 総評・注意と展望の節のあいだに、ほかの節が無い
        self.assertLess(i_pano, page.index("気圧配置と実況"))

    def test_section_right_after_overview_without_alert(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._write(d, "c.json", json.dumps({"overview": "総評の文。"}, ensure_ascii=False))
            code, page, err = self._render(["--panorama-viewpoint-html", "{V}"],
                                           base=["--data", os.path.join(FIX, "diagnosis.json"), "--commentary", c])
        self.assertEqual(code, 0, err)
        i_over, i_pano = page.index("総評の文。"), page.index('<div class="section" id="panorama">')
        self.assertLess(i_over, i_pano)
        self.assertNotIn('class="section', page[i_over:i_pano])

    def test_position_rule(self):
        pos = render_report.Renderer.panorama_position
        ov = {"type": "analysis", "items": [{"tag": "総評"}]}
        self.assertEqual(pos([{"type": "hero"}, {"type": "summary"}, ov, {"type": "alert"}, {"type": "section"}]), 4)
        self.assertEqual(pos([{"type": "hero"}, {"type": "summary"}, ov, {"type": "section"}, {"type": "alert"}]), 3)
        self.assertEqual(pos([{"type": "hero"}, {"type": "summary"}, {"type": "alert"}]), 2)   # 総評が無いときは今のまま

    def test_section_without_overview_follows_summary(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], commentary=False)
        self.assertEqual(code, 0, err)
        i_sum, i_pano = page.index('<p class="summary">'), page.index('<div class="section" id="panorama">')
        self.assertLess(i_sum, i_pano)
        self.assertNotIn('class="section', page[i_sum:i_pano])

    # ---- 直接入れる形 ----
    def test_no_iframe_srcdoc_template(self):
        # 共有の審査を通らなかった形(別のページを srcdoc に入れる)と、それに近い仕組みを使わない
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        low = page.lower()
        for w in ("<iframe", "srcdoc", "<template", "<object", "<embed", "document.write", "new function", "eval(",
                  "postmessage", "defineproperty", "blob:", "data:text/html"):
            self.assertNotIn(w, low)
        self.assertNotIn("<!doctype", low[low.index('<div class="section" id="panorama">'):])
        self.assertEqual(page.lower().count("<body"), 1)

    def test_content_kept_and_captions_hidden(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        sec = self.section(page)
        self.assertIn('<div class="ybp narrow" id="ybp-root"', sec)   # body の class は包む要素に
        self.assertIn('<div id="ybp-frame">', sec)
        self.assertIn("展望地の図", sec)
        self.assertIn("出典", sec)                                    # 出典は図の枠の中に残る
        self.assertIn(".ybp main.p39>#ybp-frame~*{display:none!important}", self.styles(sec))   # 図の下の説明は出さない
        self.assertIn('<script type="application/json" id="ybp-p39data">{"a":1}</script>', sec)

    def test_ids_are_prefixed_unique_and_resolve(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        sec = self.section(page)
        for i in re.findall(r'\bid="([^"]+)"', sec)[1:]:      # 最初は節の id(panorama)
            self.assertTrue(i.startswith("ybp-"), i)
        self.assertIn('fill:url(#ybp-hatch)', self.styles(sec))
        self.assertIn('clip-path="url(#ybp-cc1)"', sec)
        self.assertIn('href="#ybp-bw1"', sec)
        self.assert_ids_ok(page)

    def test_css_is_scoped(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        css = self.styles(self.section(page))
        for sel in self.selectors(css):
            self.assertRegex(sel, r'^(html(\[data-theme="dark"\]|:not\(\[data-theme="light"\]\)) )?\.ybp([.\s,#\[:>]|$)', sel)
        self.assertNotIn(":root", css)
        self.assertNotRegex(css, r"(^|[}\s,])(body|html)\s*[{.,]")
        # 図の CSS の変数は --ybp- で始まる(レポートの変数を読むのは、地の色をレポートに合わせる 1 か所だけ)
        self.assertEqual(css.count("var(--bg)"), 1)
        self.assertNotRegex(css.replace("var(--bg)", ""), r"(?<![\w-])--(?!ybp-)[A-Za-z]")
        self.assertIn(".ybp{--ybp-bg:#f7f8fa;", css)
        self.assertIn('@media (prefers-color-scheme: dark){html:not([data-theme="light"]) .ybp{--ybp-bg:#11161c}}', css)
        self.assertIn('html[data-theme="dark"] .ybp{--ybp-bg:#11161c}', css)
        self.assertIn(".ybp.narrow #ybp-vp svg{width:100%}", css)       # body.narrow → 包む要素の class
        self.assertIn("@media (orientation:landscape) and (max-height:500px){.ybp.narrow .ctl{display:flex;gap:6px}", css)
        self.assertTrue(css.startswith(".ybp{all:initial;display:block;position:relative}"))   # レポートから受け継がない

    def test_background_follows_report(self):
        # 地の色はレポートの --bg(ライト・ダーク・data-theme は report.css の決まりのまま)
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        self.assertIn(".ybp{background:var(--bg)!important}", self.styles(self.section(page)))
        self.assertIn("--bg:", render_report.CSS_PATH.read_text(encoding="utf-8"))

    def test_script_only_looks_inside_root(self):
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])
        self.assertEqual(code, 0, err)
        js = re.findall(r"<script>(.*?)</script>", self.section(page), re.S)
        self.assertEqual(len(js), 1)
        js = js[0]
        self.assertTrue(js.startswith("(function(){var R=document.getElementById('ybp-root');if(!R)return;"))
        rest = js.split("if(!R)return;", 1)[1]
        for w in ("document.", "location", "window.innerWidth"):
            self.assertNotIn(w, rest)
        self.assertIn("R.querySelector('#ybp-p39data')", rest)
        self.assertIn("R.querySelector('#ybp-vp svg')", rest)
        self.assertIn("R.addEventListener('click'", rest)

    def test_real_panorama_css_and_script_are_scoped(self):
        # 展望図を作る側(panorama.draw)の本物の CSS とスクリプトでも、外を見る所・漏れる指定が残らない
        from panorama import draw
        css = render_report.scope_panorama_css(draw.style(8) + draw.CSS39, {"frame", "vp", "hatch", "omitnote", "p39data"})
        for sel in self.selectors(css):
            self.assertRegex(sel, r'^(html(\[data-theme="dark"\]|:not\(\[data-theme="light"\]\)) )?\.ybp([.\s,#\[:>]|$)', sel)
        self.assertNotRegex(css, r"(?<![\w-])--(?!ybp-)[A-Za-z]")
        self.assertIn("@media (orientation:landscape) and (max-height:500px){", css)
        self.assertEqual(css.count("{"), css.count("}"))
        js = render_report.scope_panorama_js(draw.JS39, {"frame", "vp", "hatch", "omitnote", "p39data"})
        rest = js.split("if(!R)return;", 1)[1]
        for w in ("document.", "location", "window.innerWidth"):
            self.assertNotIn(w, rest)

    def test_class_names_do_not_collide_with_report(self):
        # 図の class(CSS の指定に出てくるもの)と包む要素の class は、レポートの CSS の class と重ならない
        from panorama import draw
        def classes(css):
            return set(re.findall(r"\.([A-Za-z_　-鿿][\w　-鿿-]*)", re.sub(r"\{[^{}]*\}", "{}", css)))
        report = classes(render_report.CSS_PATH.read_text(encoding="utf-8"))
        pano = classes(draw.style(8) + draw.CSS39) | {"ybp", "narrow", "wide"}
        self.assertEqual(sorted(report & pano), [])

    def test_unexpected_page_fails(self):
        code, _, err = self._render(["--panorama-viewpoint-html", "{V}"], view="<html><body>図</body></html>")
        self.assertEqual(code, 2)
        self.assertIn("展望図の HTML の形が想定と違います", err)

    # ---- 注記・名前 ----
    @staticmethod
    def vstate(*texts):
        return "".join(f'<p class="vstate wxt" data-st="h{h:02d}"{"" if h == 8 else " style=" + chr(34) + "display:none" + chr(34)}>'
                       f"<b>視点の状態:</b> {t}</p>" for h, t in zip((6, 8, 10, 12), texts))

    def test_viewer_state_text(self):
        f = render_report.viewer_state_text
        self.assertEqual(f(self.vstate("とくになし", "とくになし", "とくになし", "とくになし")), "とくになし")
        self.assertEqual(f(self.vstate("視点が雲の中の見込み", "視点が雲の中の見込み", "とくになし", "とくになし")),
                         "6・8時 視点が雲の中の見込み/10・12時 とくになし")
        self.assertEqual(f(self.vstate("a &amp; b", "c", "a &amp; b", "a &amp; b")), "6時 a & b/8時 c/10・12時 a & b")
        self.assertIsNone(f("<p>なし</p>"))

    def test_viewer_state_in_note(self):
        # 図の下の説明(視点の状態を含む)は出さないので、見出しの下の注記に出す
        view = self.VIEW.replace("</main>", self.vstate("眼下に雲海の見込み", "とくになし", "とくになし", "とくになし") + "</main>")
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], view=view)
        self.assertEqual(code, 0, err)
        note = page.split('<div class="section" id="panorama">', 1)[1].split("</div></div>", 1)[0]
        self.assertIn("視点の状態: 6時 眼下に雲海の見込み/8・10・12時 とくになし。", note)
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"])     # 読めないときは足さない
        self.assertNotIn("視点の状態:", page.split('id="ybp-root"', 1)[0])

    def test_viewpoint_name_is_not_double_escaped(self):
        view = self.VIEW.replace("雷岩", "雷岩 &amp; 東")
        code, page, err = self._render(["--panorama-viewpoint-html", "{V}"], view=view)
        self.assertEqual(code, 0, err)
        self.assertIn("<h2>雷岩 &amp; 東(展望地)からの展望</h2>", page)

    def test_works_with_spec_json(self):
        code, page, err = self._render(["--panorama-html", "{S}"], [os.path.join(EXAMPLES, "diagnosis.json")])
        self.assertEqual(code, 0, err)
        self.assertEqual(page.count('id="ybp-root"'), 1)
        self.assert_ids_ok(page)

    def test_missing_file_fails(self):
        code, _, err = self._render(["--panorama-html", "no_such_panorama.html"])
        self.assertEqual(code, 2)
        self.assertIn("エラー", err)


if __name__ == "__main__":
    unittest.main()
