"""`--json-out` の数値データ(診断モード・探索モード)から、レポート仕様(render_report.py の JSON)を組み立てる。

エージェントが何も書かなくても、数値部分(表・危険信号・集計・出典)が揃ったページになる。
エージェントが足すのは commentary(天気図の読み取りなどの解説)だけ。標準ライブラリのみ。

commentary.json(すべて省略可):
  {"title": "...", "sub": ["..."], "overview": "総評(1〜3文)",
   "alert": {"heading": "...", "body": "..."},
   "synoptic": {"title": "...", "note": "...", "analysis": [{"tag": "気圧配置", "text": "..."}]},
   "forecast_maps": {"analysis": [{"tag": "+24h", "text": "..."}]},
   "footer_extra": ["..."], "dates": ["2026-10-17", "2026-10-18"]}
synoptic.json(scripts/fetch_synoptic.py の出力): 天気図・ひまわり画像の保存先と、取得に失敗したものの理由。
"""
import re

DATE_RE = re.compile(r"^\d{4}-(\d\d)-(\d\d)\((.)\)$")
NAME_RE = re.compile(r"^(.*)\((\d+)m\)$")
HAZARD_PLAIN = "危険信号なし"
MAX_LIST = 8


# 表の列の最小幅(px)。長めの文字列(危険信号・地形など)の列が細くなって1文字ずつ折り返すのを防ぐ。
COL_WIDTH = {"date": 150, "hazard": 210, "score": 64, "wind": 84, "cape": 70, "cloud": 100, "vis": 90,
             "wet": 190, "chill": 80, "mm": 90, "terrain": 230, "name": 140, "region": 100, "access": 100, "rank": 36}


def sized(cols):
    return [dict(c, width=COL_WIDTH[c["key"]]) if c["key"] in COL_WIDTH and "width" not in c else c for c in cols]


def md(label):
    """'2026-10-12(月)' → '10/12(月)'。"""
    m = DATE_RE.match(label or "")
    return f"{int(m.group(1))}/{int(m.group(2))}({m.group(3)})" if m else str(label)


def tab_parts(label):
    m = DATE_RE.match(label or "")
    return (f"{int(m.group(1))}/{int(m.group(2))}", m.group(3)) if m else (str(label), "")


def score_text(v):
    return "-" if v is None else f"{v:.1f}"


def level_of(score):
    return "safe" if score >= 80 else "caution" if score >= 40 else "warning"


def joined(items, limit=MAX_LIST):
    items = list(items)
    shown = "、".join(items[:limit])
    return shown + (f" ほか{len(items) - limit}日" if len(items) > limit else "")


def num(v, suffix="", digits=None, thousands=False):
    if v is None:
        return "-"
    if digits is not None:
        v = int(round(v)) if digits == 0 else round(v, digits)
    text = f"{v:,}" if thousands else f"{v}"
    return text + suffix


def hazard_cell(row):
    """危険信号のセル。無ければ '-'(レンダラーが灰色の '-' にする)。"""
    if row["hazard_class"] == "none":
        return "-"
    return {"text": row["cells"]["hazard"], "class": row["hazard_class"]}


# ----------------------------------------------------------------------------- 天気図・衛星画像
def synoptic_images(syn):
    """fetch_synoptic.py の出力 → (実況+ひまわり, 予想天気図, 出典の集合, 失敗理由の文字列)。"""
    now, fc, attributions = [], [], []
    failed = []
    if syn:
        for im in syn.get("images", []):
            item = {"src": im["src"], "alt": im.get("cap", ""), "cap": im.get("cap", "")}
            (fc if im.get("kind", "").startswith("jma_plus") else now).append(item)
            if im.get("attribution") and im["attribution"] not in attributions:
                attributions.append(im["attribution"])
        failed = [f"{f.get('what', '?')}({f.get('reason', '')})" for f in syn.get("failed", [])]
    return now, fc, attributions, "、".join(failed)


def synoptic_section(cm, now_images, failed_text, syn_given, title_default, note_default):
    """気圧配置の節(id=synoptic)。画像・解説が無いときは、無いことを明示した省略表示にする(黙って薄くしない)。"""
    c = (cm or {}).get("synoptic") or {}
    analysis = [a for a in c.get("analysis", []) if a.get("text")]
    images = now_images
    section = {"type": "section", "id": "synoptic", "title": c.get("title") or title_default}
    if not images and not analysis:
        reason = failed_text or ("天気図・衛星画像を取得していません(fetch_synoptic.py 未実施)" if not syn_given else "取得できた画像がありません")
        section["omitted"] = f"{reason}。気圧配置の解説もありません。数値の表のみのレポートです"
        return section
    section["note"] = c.get("note") or (note_default if images else None)
    content = []
    if images:
        content.append({"type": "images", "items": images})
    else:
        section["images_omitted"] = failed_text or "取得できた画像がありません"
    if not analysis:
        analysis = [{"tag": "注記", "text": "画像は添付しましたが、読み取りの解説は付いていません。"}]
    content.append({"type": "analysis", "items": analysis})
    section["content"] = content
    return section


def forecast_maps_section(cm, fc_images):
    c = (cm or {}).get("forecast_maps") or {}
    analysis = [a for a in c.get("analysis", []) if a.get("text")]
    if not fc_images and not analysis:
        return None
    content = []
    if fc_images:
        content.append({"type": "images", "items": fc_images})
    if analysis:
        content.append({"type": "analysis", "items": analysis})
    return {"type": "section", "id": "forecast_maps", "title": c.get("title") or "前線の接近 — 予想天気図(+24h/+48h)",
            "note": c.get("note") or "気象庁 予想天気図(実況と同一セッションで取得)", "content": content}


# ----------------------------------------------------------------------------- フッター
def footer(data, attributions, has_analysis, extra, kind):
    p = ["データ出典: Open-Meteo(気象庁MSM+ECMWF IFS 0.25、山頂標高へジオポテンシャル高度で補間)。"
         "他社の登山天気予報サービスは参照していません。スコアは雲量・視程・降水(活動時間中の雨天割合)の重み付き幾何平均。"
         "危険信号(雷/強風/低体温症)はスコアに含まれない別枠評価です。降水タイミングは直前の再確認を推奨します。"]
    if kind == "explore":
        p.append("MSM範囲外(約3日より先)の雨天割合・降水量はECMWF降水確率の期待値ベースの粗い見積もりです。"
                 "ランキング表の危険信号は一括計算のため、個別診断(診断モード)より見逃しが起きやすいです。有望な山ほど、行く前に診断モードで個別に確認してください。")
    else:
        p.append("9日目以降などMSM範囲外はECMWFのみの粗い見積もりです。ECMWFアンサンブル確信度は参考値で、スコアには含まれません。")
    if data.get("terrain_attribution"):
        p.append("地形(山頂風向に対する風上/風下の時間数と地形性上昇流)はスコアには未反映の参考情報です。" + data["terrain_attribution"])
    if attributions:
        p.append("天気図・衛星画像: " + " / ".join(attributions) + "。公共データ利用規約(第1.0版)に基づき出典表記。")
    if has_analysis:
        p.append("天気図・衛星画像の読み取りはAIによる目視の定性的な補足であり、点予報の数値を置き換えるものではありません。")
    p.extend(extra or [])
    scr = "mountain_weather_mvp.py" if kind == "explore" else "mountain_weather_detail.py"
    p.append(f"生成: ヤマビヨリ{'探索' if kind == 'explore' else '診断'}モード({scr})、{data.get('generated', '')}時点のデータ。")
    return p


# ----------------------------------------------------------------------------- 診断モード
def auto_summary_diagnosis(days):
    """数値だけから言える事実を並べた要約(気象の解釈は書かない)。"""
    best = max(days, key=lambda d: d["score"])
    good = [md(d["label"]) for d in days if d["score"] >= 80]
    bad = [md(d["label"]) for d in days if d["score"] < 40]
    warn = [md(d["label"]) for d in days if d["hazard_class"] == "warning"]
    parts = [f"{len(days)}日間で最もスコアが高いのは **{md(best['label'])}の{score_text(best['score'])}点** です。"]
    parts.append(f"スコア80以上の日: {joined(good)}。" if good else "スコア80以上の日はありません。")
    if bad:
        parts.append(f"スコア40未満(崩れる日): {joined(bad)}。")
    if warn:
        parts.append(f"警戒以上の危険信号がある日: {joined(warn)}(危険信号はスコアとは別枠です)。")
    msm = [d for d in days if d.get("model") == "MSM"]
    if msm and len(msm) < len(days):
        parts.append(f"{md(msm[-1]['label'])}まではMSM+ECMWF、それ以降はECMWFのみの粗い見積もりです。")
    return "".join(parts)


def diagnosis_spec(data, cm, syn):
    m, days = data["mountain"], data["days"]
    if not days:
        raise ValueError("data に days がありません")
    now_images, fc_images, attributions, failed_text = synoptic_images(syn)
    t = days[0]
    stats = [
        {"l": "稜線風速", "v": num(t["ridge_wind_ms"], " m/s")},
        {"l": "稜線雲量(較正/生)", "v": t["cells"]["cloud"] + "%"},
        {"l": "視程", "v": num(t["ridge_visibility"], " m", thousands=True)},
        {"l": "雨天割合", "v": t["cells"]["wet"] + "%"},
        {"l": "体感温度", "v": num(t["chill"], "℃")},
        {"l": "全日降水", "v": num(t["day_total_precip_mm"], " mm")},
        {"l": "雷CAPE", "v": num(t["cape"], digits=0)},
        {"l": "地形", "v": t["cells"]["terrain"]},
    ]
    hero = {
        "type": "hero", "id": "hero",
        "label": f"今日 {md(t['label'])} — 稜線帯滞在の総合スコア", "score": score_text(t["score"]),
        "hazard": ({"class": t["hazard_class"], "text": t["cells"]["hazard"]} if t["hazard_class"] != "none"
                   else {"class": "safe", "text": HAZARD_PLAIN}),
        "stats": stats,
        "next": [{"d": md(d["label"]), "s": score_text(d["score"]), "class": level_of(d["score"]),
                  "h": f"雨天割合{d['cells']['wet'].split('(')[0]}%・" + (d["cells"]["hazard"] if d["hazard_class"] != "none" else HAZARD_PLAIN)}
                 for d in days[1:3]],
    }
    blocks = [hero, {"type": "summary", "text": auto_summary_diagnosis(days)}]
    if cm.get("overview"):
        blocks.append({"type": "analysis", "items": [{"tag": "総評", "text": cm["overview"]}]})
    if cm.get("alert"):
        blocks.append({"type": "alert", **cm["alert"]})

    blocks.append(synoptic_section(cm, now_images, failed_text, syn is not None, "気圧配置と実況",
                                   "気象庁 実況天気図・ひまわり画像(出典:気象庁ホームページ)"))
    fm = forecast_maps_section(cm, fc_images)
    if fm:
        blocks.append(fm)

    msm_n = sum(1 for d in days if d.get("model") == "MSM")
    note = (f"{md(days[0]['label'])}〜{md(days[msm_n - 1]['label'])}は気象庁MSM+ECMWF決定論のマージ(実測に近い)、それ以降はECMWF決定論のみ(ECMWFタグ)。"
            if 0 < msm_n < len(days) else "ECMWF決定論のみの見積もり。")
    if data.get("precip_timing_note"):
        note += " " + data["precip_timing_note"]
    cols = [
        {"key": "date", "label": "日付", "kind": "nowrap"},
        {"key": "hazard", "label": "危険信号(雷/強風/低体温症)", "kind": "badge"},
        {"key": "score", "label": "スコア", "kind": "score"},
        {"key": "wind", "label": "稜線風速", "kind": "num"},
        {"key": "cape", "label": "雷CAPE", "kind": "num"},
        {"key": "cloud", "label": "雲量%(較正/生)", "kind": "num"},
        {"key": "vis", "label": "視程", "kind": "num"},
        {"key": "wet", "label": "雨天割合%(活動時間)", "kind": "num"},
        {"key": "chill", "label": "体感温度", "kind": "num"},
        {"key": "mm", "label": "降水量(全日)", "kind": "num"},
        {"key": "terrain", "label": "地形", "kind": "text"},
    ]
    rows = [{
        "date": {"text": d["label"], "tag": d.get("model", "")},
        "hazard": hazard_cell(d), "score": d["score"],
        "wind": num(d["ridge_wind_ms"], " m/s"), "cape": num(d["cape"], digits=0), "cloud": d["cells"]["cloud"],
        "vis": num(d["ridge_visibility"], " m", thousands=True), "wet": d["cells"]["wet"],
        "chill": num(d["chill"], "℃"), "mm": num(d["day_total_precip_mm"], " mm"), "terrain": d["cells"]["terrain"],
    } for d in days]
    blocks.append({"type": "section", "id": "scores", "title": f"{len(days)}日間の登山向けスコア", "note": note,
                   "content": [{"type": "table", "min_width": 1000, "columns": sized(cols), "rows": rows}]})

    panels = []
    if data.get("cloud_sea"):
        panels.append({"title": "雲海・快晴チャンス", "note": "日の出前後に、山頂付近は晴れていて、その下(地表付近など)が雲に覆われる予報の時刻。山頂から雲海を見下ろせるチャンスの目安です。参考値であり、実際に見えるかは大気の状態や視界に左右されます。",
                       "rows": [{"l": f"{md(o['label'])} {o['time']}",
                                 "v": cloud_sea_text(o)}
                                for o in data["cloud_sea"]]})
    if data.get("transitions"):
        panels.append({"title": "天候の変わり目", "note": "1日の中で降水確率が大きく変化する日。早出/待機の判断材料に。",
                       "items": [f"**{md(x['label'])}**: {x['note']}" for x in data["transitions"]]})
    if panels:
        blocks.append({"type": "section", "content": [{"type": "panels", "panels": panels}]})

    if data.get("confidence"):
        conf_rows = [{"date": md(c["label"]), "label": c["forecast"],
                      "conf": {"text": f"{c['confidence']:.0f}", "score": c["confidence"]},
                      "detail": f"{c['cloud']:.0f}/{c['precip']:.0f}/{c['temp']:.0f}"} for c in data["confidence"]]
        blocks.append({"type": "section", "id": "confidence", "title": "ECMWFアンサンブル確信度(2日先以降)",
                       "note": "51メンバーのブレの大きさの目安。参考値であり、スコアには含まれません。",
                       "content": [{"type": "table", "min_width": 520, "columns": [
                           {"key": "date", "label": "日付", "kind": "nowrap"}, {"key": "label", "label": "予報"},
                           {"key": "conf", "label": "確信度", "kind": "confbar"},
                           {"key": "detail", "label": "内訳(雲量/降水/気温)", "kind": "num"}], "rows": conf_rows}]})
    elif data.get("confidence_error"):
        blocks.append({"type": "note", "text": f"ECMWF確信度は取得できませんでした(点予報には影響しません): {data['confidence_error']}"})

    has_analysis = bool([a for a in (cm.get("synoptic") or {}).get("analysis", []) if a.get("text")])
    return {
        "mode": "diagnosis", "eyebrow": "ヤマビヨリ · 診断モード", "title": cm.get("title") or m["name"],
        "sub": cm.get("sub") or [f"**{m['elevation_m']}m**", m.get("region", ""), m.get("access", ""), f"{m['lat']}, {m['lon']}"],
        "blocks": blocks,
        "footer": footer(data, attributions, has_analysis, cm.get("footer_extra"), "diagnosis"),
    }


# ----------------------------------------------------------------------------- 探索モード
def deck_label(band):
    """雲海の層の表示名。内部名 'surface' は「地表付近」、'1000m帯' は「標高1000m付近」と読み替える。"""
    if band == "surface":
        return "地表付近"
    m = re.match(r"^(\d+)m帯$", str(band))
    return f"標高{m.group(1)}m付近" if m else str(band)


def cloud_sea_text(o):
    text = (f"山頂付近は雲{num(o['summit_cloud_pct'], '%', digits=0)}(ほぼ晴れ) / "
            f"下の{deck_label(o['deck_band'])}は雲{num(o['deck_cloud_pct'], '%', digits=0)}")
    if o.get("deck_humidity_pct") is not None:
        text += f"(湿度{num(o['deck_humidity_pct'], '%', digits=0)})"
    return text


def split_name(label):
    m = NAME_RE.match(label)
    return (m.group(1), f"{m.group(2)}m") if m else (label, "")


def region_table(days, n_dates):
    """山域ごと・日ごとの平均スコア(全山の集計。基準点未満の山も含める)。"""
    use = [d for d in days if d["all"]][:n_dates]
    regions = {}
    for d in use:
        by = {}
        for a in d["all"]:
            by.setdefault(a["region"], []).append(a["score"])
        for r, vals in by.items():
            regions.setdefault(r, {})[d["date"]] = sum(vals) / len(vals)
    cols = [{"key": "region", "label": "山域", "kind": "strong"}] + [
        {"key": d["date"], "label": " ".join(tab_parts(d["label"])), "kind": "num"} for d in use]
    rows = []
    for r, vals in sorted(regions.items(), key=lambda kv: -sum(kv[1].values()) / len(kv[1])):
        row = {"region": r}
        for d in use:
            v = vals.get(d["date"])
            row[d["date"]] = {"text": "-" if v is None else f"{v:.0f}",
                              "class": "" if v is None else "sc-good" if v >= 95 else "sc-mid" if v >= 85 else "sc-poor"}
        rows.append(row)
    return cols, rows, len(use)


def day_summary_explore(day, data, shown):
    allr, rows = day["all"], day["rows"]
    n_all = len(allr)
    n_min = len(rows)
    parts = [f"{n_all}座中、**{n_min}座が{data['min_score']:.0f}点以上**です。"] if n_all else []
    if rows:
        top = "、".join(f"{split_name(r['mountain'])[0]}({r['score']:.1f})" for r in rows[:3])
        parts.append(f"上位: {top}。")
        warn = sum(1 for r in rows if r["hazard_class"] == "warning")
        if warn:
            parts.append(f"うち警戒以上の危険信号がある山は{warn}座(危険信号はスコアとは別枠)。")
    if shown < len(rows):
        parts.append(f"表は上位{shown}座(基準点以上は全{len(rows)}座)。")
    if day.get("timing_note"):
        parts.append(day["timing_note"])
    return "".join(parts)


def explore_spec(data, cm, syn, n_days=7, top=15):
    days = data["days"]
    if cm.get("dates"):
        wanted = set(cm["dates"])
        sel = [d for d in days if d["date"] in wanted]
    else:
        sel = days[:n_days] if n_days else days
    sel = [d for d in sel if d["all"]]
    now_images, fc_images, attributions, failed_text = synoptic_images(syn)
    blocks = []
    if cm.get("overview"):
        blocks.append({"type": "analysis", "items": [{"tag": "総評", "text": cm["overview"]}]})
    if cm.get("alert"):
        blocks.append({"type": "alert", **cm["alert"]})
    blocks.append(synoptic_section(cm, now_images, failed_text, syn is not None, "概況",
                                   "気象庁 実況天気図・ひまわり画像(出典:気象庁ホームページ)"))
    fm = forecast_maps_section(cm, fc_images)
    if fm:
        blocks.append(fm)

    cols, rows, n_used = region_table(days if not cm.get("dates") else sel, n_days or len(days))
    blocks.append({"type": "section", "id": "regions", "title": "山域サマリー — どの山域がいつ安定しているか",
                   "note": f"{len(rows)}の山域について、日ごとの平均スコア(全{data['pool_size']}座の集計)を平均の高い順に並べました。"
                           "色は95点以上が緑、85〜94点が黄、それ未満が灰です。",
                   "content": [{"type": "table", "min_width": 480, "columns": cols, "rows": rows}]})

    rank_cols = [
        {"key": "rank", "label": "#", "kind": "muted"}, {"key": "name", "label": "山", "kind": "strong"},
        {"key": "region", "label": "山域", "kind": "muted"}, {"key": "access", "label": "アクセス", "kind": "nowrap"},
        {"key": "score", "label": "スコア", "kind": "score"},
        {"key": "hazard", "label": "危険信号", "kind": "badge"},
        {"key": "wind", "label": "稜線風速", "kind": "num"}, {"key": "cape", "label": "雷CAPE", "kind": "num"},
        {"key": "cloud", "label": "雲量%(較正/生)", "kind": "num"}, {"key": "vis", "label": "視程", "kind": "num"},
        {"key": "wet", "label": "雨天割合%", "kind": "num"}, {"key": "chill", "label": "体感温度", "kind": "num"},
        {"key": "mm", "label": "降水量(全日)", "kind": "num"}, {"key": "terrain", "label": "地形"},
    ]
    tabs = []
    for d in sel:
        shown_rows = d["rows"][:top] if top else d["rows"]
        label, wk = tab_parts(d["label"])
        trows = []
        for r in shown_rows:
            name, elev = split_name(r["mountain"])
            trows.append({
                "rank": r["rank"], "name": {"text": name, "sub": elev}, "region": r["region"], "access": r["access"],
                "score": r["score"], "hazard": hazard_cell(r),
                "wind": num(r["ridge_wind_ms"], " m/s"), "cape": num(r["cape"], digits=0), "cloud": r["cells"]["cloud"],
                "vis": num(r["ridge_visibility"], " m", thousands=True), "wet": r["cells"]["wet"],
                "chill": num(r["chill"], "℃"), "mm": num(r["day_total_precip_mm"], " mm"), "terrain": r["cells"]["terrain"],
            })
        content = [{"type": "summary", "text": day_summary_explore(d, data, len(shown_rows))}]
        if trows:
            content.append({"type": "table", "min_width": 1200, "columns": sized(rank_cols), "rows": trows})
        tabs.append({"label": label, "wk": wk, "heading": md(d["label"]), "content": content})
    blocks.append({"type": "section", "id": "ranking", "title": "日ごとのランキング",
                   "note": f"スコアの高い順(基準点{data['min_score']:.0f}点以上" + (f"、上位{top}座まで" if top else "") + ")。",
                   "content": [{"type": "tabs", "tabs": tabs}]})

    has_analysis = bool([a for a in (cm.get("synoptic") or {}).get("analysis", []) if a.get("text")])
    first, last = (md(sel[0]["label"]), md(sel[-1]["label"])) if sel else ("", "")
    return {
        "mode": "explore", "eyebrow": "探索モード・登山向けランキング", "title": cm.get("title") or "山の天気ランキング",
        "sub": cm.get("sub") or [f"対象: **{data['pool_size']}座**({data['region_label']})", f"{first}〜{last}",
                                 f"生成: **{data['generated']}**"],
        "blocks": blocks,
        "footer": footer(data, attributions, has_analysis, cm.get("footer_extra"), "explore"),
    }


def build_spec(data, commentary=None, synoptic=None, n_days=7, top=15):
    """数値データ(+任意の解説・天気図取得結果)→ render_report.py のレポート仕様。"""
    cm = commentary or {}
    if data.get("schema", "").split("/")[0] != "yamabiyori-report-data":
        raise ValueError("--data は --json-out で保存した数値データ(schema: yamabiyori-report-data)を指定してください")
    if data.get("mode") == "diagnosis":
        return diagnosis_spec(data, cm, synoptic)
    if data.get("mode") == "explore":
        return explore_spec(data, cm, synoptic, n_days=n_days, top=top)
    raise ValueError(f"未対応の mode: {data.get('mode')!r}")
