"""展望図の山頂の点のファイル panorama/data/summits.json を作る(手元に「日本の主な山岳標高」の表があるときだけ動く)。

国土地理院「日本の主な山岳標高」(1003 山)の GeoJSON(cache/panorama/gsi_1003/*.geojson。利用者が自分で置いたもの)から、
日本百名山とヤマビヨリの 76 座に当たる山頂の点だけを抜き出し、図に出す呼び名を付けて書く。表の全部は書かない。
- 対応は山名辞書(panorama/data/hyakumeizan.json・mountains76.json)の material(山名・標高・都道府県)で決める。
  表の中に当たる行が 0 行・2 行以上の山は入れず、画面に出す。
- 緯度・経度は表の点の座標(geometry.coordinates)、標高は「標高値(m)」を、表の値のまま書く(丸めない・足さない)。
  図の計算(panorama.names.load_gsi)が使うのと同じ値。
- 呼び名は、表の全部を材料にしたときに図に出る呼び名(panorama.terrain.build_ctx と同じ決め方)。

使い方: ./venv/Scripts/python.exe -X utf8 tools/build_panorama_summits.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from panorama import config, names  # noqa: E402

SOURCE = "「日本の主な山岳標高」(国土地理院)をもとに作成"
PROCESSING = "加工: 日本百名山とヤマビヨリの 76 座に当たる山頂の点を抜き出し、呼び名を付けた"


def main() -> int:
    paths = sorted(glob.glob(os.path.join(config.gsi_dir(), "*.geojson")))
    if not paths:
        print(f"表がありません({config.gsi_dir()} に「日本の主な山岳標高」の .geojson が無い)。何も書きません。")
        return 1
    with open(paths[0], encoding="utf-8-sig") as fh:
        gj = json.load(fh)
    feats = gj["features"]
    by_key = {}
    for f in feats:
        p = f["properties"]
        by_key.setdefault((p["山名＜山頂名＞"], int(round(float(p["標高値(m)"]))), p["都道府県"]), []).append(f)
    D = names.PeakDictionary()
    want = {}          # 連番 → {"hy": 百名山の項目 or None, "m76": [76 座の項目]}
    problems = []
    for h in D.hy:
        k = (h["material"]["name"], h["material"]["ele_m"], h["material"]["pref"])
        hits = by_key.get(k, [])
        if len(hits) != 1:
            problems.append(f"百名山 {h['no']} {h['name']}: 表に当たる行が {len(hits)} 行")
            continue
        want.setdefault(hits[0]["properties"]["連番"], {"hy": None, "m76": []})["hy"] = h
    n76_none = []
    for e in D.m76:
        if not e["material"]:
            n76_none.append(f"{e['id']} {e['name']}")
            continue
        k = (e["material"]["name"], e["material"]["ele_m"], e["material"]["pref"])
        hits = by_key.get(k, [])
        if len(hits) != 1:
            problems.append(f"76 座 {e['id']} {e['name']}: 表に当たる行が {len(hits)} 行")
            continue
        want.setdefault(hits[0]["properties"]["連番"], {"hy": None, "m76": []})["m76"].append(e)
    # 呼び名: 表の全部を材料にしたときの図の呼び名(build_ctx と同じ)
    materials, _ = names.load_materials()
    marks25, _ = names.apply(D.attach(materials), materials, D)
    marks, _ = names.apply_overrides(marks25)
    feat_by_no = {f["properties"]["連番"]: f for f in feats}
    rows = []
    for no in sorted(want):
        f = feat_by_no[no]
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        w = want[no]
        mk = marks.get(f"gsi{no}", {})
        tiers = (["百名山"] if w["hy"] else []) + (["76座"] if w["m76"] else [])
        rows.append({"連番": no, "name": p["山名＜山頂名＞"], "yomi": p["山名よみ＜山頂名よみ＞"], "display": mk.get("display"),
                     "lat": lat, "lon": lon, "ele_m": p["標高値(m)"], "kind": p["種別"], "pref": p["都道府県"],
                     "tiers": tiers, "hyakumeizan_no": w["hy"]["no"] if w["hy"] else None,
                     "mountain_ids": [e["id"] for e in w["m76"]]})
    n_hy = sum(1 for r in rows if "百名山" in r["tiers"])
    n_76 = sum(len(r["mountain_ids"]) for r in rows)
    n_76_pts = sum(1 for r in rows if "76座" in r["tiers"])
    n_both = sum(1 for r in rows if len(r["tiers"]) == 2)
    out = {"source": SOURCE, "processing": PROCESSING, "table_file": os.path.basename(paths[0]),
           "extracted": date.today().isoformat(),
           "notes": "緯度・経度は表の点の座標(geometry.coordinates)、標高は表の「標高値(m)」のまま。"
                    "76 座のうち表に当たる点の無い山(mountains76.json の material が null)は入れていない"
                    "(図では MOUNTAINS の座標を使い、「山頂の点は目安です」と書く)。",
           "counts": {"points": len(rows), "hyakumeizan": n_hy, "m76_points": n_76_pts, "m76_mountains": n_76,
                      "both": n_both},
           "summits": rows}
    path = os.path.join(config.DATA_DIR, "summits.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"書きました: {path}")
    print(f"点 {len(rows)}(百名山 {n_hy} 座・76 座 {n_76} 座({n_76_pts} 点)・重なり {n_both} 点)")
    print(f"表に点の無い 76 座(入れない): {len(n76_none)} 座 {'、'.join(n76_none)}")
    for x in problems:
        print("入れなかった:", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
