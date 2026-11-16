"""展望図の実証 段階1: 大菩薩嶺(雷岩付近)から「何が見えるか」を計算し、PeakFinder の写真のラベルと答え合わせする。

使い方(リポジトリのルートで):
    ./venv/Scripts/python.exe -X utf8 experiments/panorama/stage1_daibosatsu.py [--data-dir C:/temp/daibosatsu]
        [--no-fetch] [--max-new-tiles 400]

入力(読むだけ・リポジトリに入れない):
  --data-dir の peakfinder_labels.json(写真ごとの撮影地点・ラベル)と GPX。写真は確認ページから参照するだけ。
  cache/panorama/gsi_1003/*.geojson(国土地理院「日本の主な山岳標高」)、mountain_weather_core.MOUNTAINS。
通信: 地理院の標高タイルだけ(mountain_terrain.DemTileStore 経由。terrain_cache/ に保存)。
出力: out/panorama/daibosatsu/(git の管理外)。

撮影地点の座標や時刻はこのファイルに書かない(実行時に labels JSON から読む)。
"""
from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import requests  # noqa: E402

import geometry as g  # noqa: E402
import mountain_terrain as mt  # noqa: E402
from mountain_weather_core import MOUNTAINS  # noqa: E402

OUT_DIR = os.path.join(ROOT, "out", "panorama", "daibosatsu")
GSI_DIR = os.path.join(ROOT, "cache", "panorama", "gsi_1003")
IMAGE_W, IMAGE_H = 1600, 1200

SKY_AZ = (150.0, 310.0, 0.1)
SKY_MAX_M = 150000.0
FAR_MAX_M = 200000.0
ELE_TOL_M = 10          # ラベルの標高と材料の標高の差の許容
X_TOL_PX = 20           # 合格の目安: 横位置の残差
FAMOUS = ["三ツ峠山", "富士山", "毛無山", "身延山", "聖岳", "悪沢岳", "間ノ岳", "北岳", "仙丈ヶ岳",
          "甲斐駒ヶ岳", "御嶽山", "牛奥ノ雁ヶ腹摺山"]
# 5.4 の 1〜3 で、同じ名前の山が複数あるときに使う標高(利用者の写真の PeakFinder 表示・指示書の記述による)
FAMOUS_HINT_ELE = {"甲斐駒ヶ岳": 2967, "木曽駒ヶ岳": 2956, "毛無山": 1964, "富士山": 3776, "御嶽山": 3067}


# ---------------------------------------------------------------------------
# 標高: 地理院タイル(上限つき)
# ---------------------------------------------------------------------------
class StatusSession(requests.Session):
    """DemTileStore に渡す Session。応答の状態コードを記録するだけ(User-Agent は DemTileStore が付ける)。"""

    def __init__(self):
        super().__init__()
        self.statuses = []

    def get(self, url, **kw):
        try:
            r = super().get(url, **kw)
        except requests.RequestException as e:
            self.statuses.append(type(e).__name__)
            raise
        self.statuses.append(r.status_code)
        return r


class BudgetedDem:
    """新しく取るタイルの枚数に上限をかけた標高関数。上限・429・同じ失敗5回で取得をやめる。"""

    def __init__(self, max_new_tiles: int, allow_fetch: bool = True):
        self.session = StatusSession()
        self.store = mt.DemTileStore(session=self.session)
        self.max_new = max_new_tiles
        self.allow_fetch = allow_fetch
        self.stopped_reason = None
        self.refused = 0
        self.fail_streak = {}

    def files_on_disk(self) -> int:
        return len(list(self.store.cache_dir.glob(f"{self.store.zoom}_*.npy")))

    def _tile(self, x: int, y: int):
        if (x, y) in self.store._mem or self.store._disk_path(x, y).exists():
            return self.store.get_tile(x, y)
        if not self.allow_fetch or self.stopped_reason:
            self.refused += 1
            return None
        if self.files_on_disk() >= self.max_new:
            self.stopped_reason = f"上限 {self.max_new} 枚に達した"
            self.refused += 1
            return None
        before = len(self.session.statuses)
        t = self.store.get_tile(x, y)
        st = self.session.statuses[before:] or ["?"]
        last = st[-1]
        if t is None:
            self.fail_streak[last] = self.fail_streak.get(last, 0) + 1
            if last == 429:
                self.stopped_reason = "HTTP 429"
            elif self.fail_streak[last] >= 5:
                self.stopped_reason = f"同じ種類の失敗が5回 ({last})"
        else:
            self.fail_streak = {}
        return t

    def tile_ready(self, x, y) -> bool:
        return self._tile(x, y) is not None

    def __call__(self, lat: float, lon: float):
        xf, yf = mt.deg2tile_frac(lat, lon, self.store.zoom)
        x, y = int(math.floor(xf)), int(math.floor(yf))
        tile = self._tile(x, y)
        if tile is None:
            v = None
        else:
            px = min(int((xf - x) * 256), 255)
            py = min(int((yf - y) * 256), 255)
            h = tile[py, px]
            v = 0.0 if np.isnan(h) else float(h)   # 海・無効値は 0m(既存の get_elevation と同じ扱い)
        return v


def tiles_on_ray(viewer, az, max_m):
    s = []
    seen = set()
    for d in g.sample_distances(max_m, g.DEFAULT_STEPS, g.NEAR_SKIP_M):
        la, lo = mt.vincenty_direct(viewer["lat"], viewer["lon"], az, d)
        xf, yf = mt.deg2tile_frac(la, lo, mt.DEM_ZOOM)
        t = (int(xf), int(yf))
        if t not in seen:
            seen.add(t)
            s.append(t)
    return s


# ---------------------------------------------------------------------------
# 材料
# ---------------------------------------------------------------------------
def norm(s: str) -> str:
    s = s.strip()
    for a, b in (("ケ", "ヶ"), ("ッ", "ツ"), ("峯", "峰"), ("の", "ノ"), ("　", ""), (" ", "")):
        s = s.replace(a, b)
    return s


def name_variants(name: str) -> set:
    """「大雪山（ヌタプカウシペ）＜黒岳＞」「乗鞍岳(剣ヶ峰)」などから呼び名の候補を作る。"""
    out = set()
    base = re.sub(r"[（(].*?[）)]", "", name)
    base = re.sub(r"＜.*?＞", "", base)
    out.add(base)
    out.update(re.findall(r"[（(](.*?)[）)]", name))
    out.update(re.findall(r"＜(.*?)＞", name))
    return {norm(v) for v in out if v.strip()}


def load_gsi():
    paths = glob.glob(os.path.join(GSI_DIR, "*.geojson"))
    if not paths:
        return [], None
    gj = json.load(open(paths[0], encoding="utf-8-sig"))
    out = []
    for f in gj["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        out.append({"src": "gsi1003", "id": f"gsi{p['連番']}", "name": p["山名＜山頂名＞"],
                    "yomi": p["山名よみ＜山頂名よみ＞"], "ele_m": float(p["標高値(m)"]),
                    "lat": float(lat), "lon": float(lon), "kind": p["種別"], "pref": p["都道府県"],
                    "variants": name_variants(p["山名＜山頂名＞"])})
    return out, os.path.basename(paths[0])


def load_materials():
    gsi, gsi_file = load_gsi()
    mts = []
    for m in MOUNTAINS:
        mts.append({"src": "MOUNTAINS", "id": m["id"], "name": m["name"], "ele_m": float(m["elevation_m"]),
                    "lat": m["lat"], "lon": m["lon"], "variants": name_variants(m["name"])})
    # MOUNTAINS と 1003山の対応(名前が重なり 3km 以内、なければ 500m 以内の最寄り)
    pairs = []
    only_mts = []
    for m in mts:
        best = None
        for q in gsi:
            d, _ = g.vincenty_inverse(m["lat"], m["lon"], q["lat"], q["lon"])
            named = bool(m["variants"] & q["variants"]) or any(
                v in w or w in v for v in m["variants"] for w in q["variants"] if len(v) >= 2 and len(w) >= 2)
            if (named and d <= 3000) or d <= 500:
                score = d - (10000 if named else 0)
                if best is None or score < best[0]:
                    best = (score, q, d, named)
        if best:
            q, d = best[1], best[2]
            m["gsi_id"] = q["id"]
            q.setdefault("mountains_ids", []).append(m["id"])
            pairs.append({"mountains": f"{m['id']} {m['name']}", "gsi": q["name"], "offset_m": round(d, 1),
                          "ele_mountains": m["ele_m"], "ele_gsi": q["ele_m"], "name_match": best[3]})
        else:
            only_mts.append(m)
    materials = gsi + only_mts   # 両方にある山は 1003山を優先
    return materials, pairs, gsi_file, len(gsi)


def load_gps_track(path):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    pts = []
    for p in ET.parse(path).getroot().iter("{http://www.topografix.com/GPX/1/1}trkpt"):
        t = p.find("g:time", ns).text
        pts.append((datetime.fromisoformat(t.replace("Z", "+00:00")), float(p.get("lat")), float(p.get("lon")),
                    float(p.find("g:ele", ns).text)))
    return pts


# ---------------------------------------------------------------------------
# ラベルと材料の対応
# ---------------------------------------------------------------------------
CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def label_tokens(text: str) -> list:
    """「黒岳 釈迦ヶ岳 - くろたけ しゃかがたけ」→ ["黒岳釈迦ヶ岳", "黒岳", "釈迦ヶ岳"](漢字を含む側だけ)。"""
    parts = [p.strip() for p in text.split(" - ")]
    toks = []
    for p in parts:
        p = re.sub(r"[（(].*?[）)]", "", p)
        if not re.search(r"[\u4e00-\u9fff]", p):
            continue
        words = [w for w in p.split() if CJK.search(w)]
        if words:
            toks.append(norm("".join(words)))
            toks.extend(norm(w) for w in words)
    return list(dict.fromkeys(toks))


def match_label(label, materials, viewer, *, used_bearing=None):
    """返り値: (材料 or None, 根拠, 確かさ, 候補数)。確かさ: 確か / 表記ゆれ / 不確か / 未取得。"""
    toks = label_tokens(label["text"])
    ele = label["ele_m"]
    cands = []
    for m in materials:
        names = m["variants"]
        exact = any(t in names for t in toks)
        loose = not exact and any(t.replace("ヶ", "") == n.replace("ヶ", "") for t in toks for n in names)
        if not (exact or loose):
            continue
        d, az = g.vincenty_inverse(viewer["lat"], viewer["lon"], m["lat"], m["lon"])
        if d > 250000:
            continue
        cands.append((m, exact, abs(m["ele_m"] - ele), d, az))
    if not toks:
        return None, "漢字の名前が無い(ラベルの名前が欠けている)", "未取得", 0
    if not cands:
        return None, f"名前『{'/'.join(toks)}』が材料に無い", "未取得", 0
    ele_ok = [c for c in cands if c[2] <= ELE_TOL_M]
    if not ele_ok:
        best = min(cands, key=lambda c: c[2])
        return None, (f"名前は『{best[0]['name']}』({best[0]['src']})が一致するが標高が {best[0]['ele_m']:.0f}m で"
                      f"ラベルの {ele}m と合わない"), "未取得", len(cands)
    # 標高まで合う候補が複数なら、より厳密な名前の一致を優先し、なお複数なら「不確か」
    exact_ok = [c for c in ele_ok if c[1]] or ele_ok
    exact_ok.sort(key=lambda c: (c[2], c[3]))
    m, exact, de, d, az = exact_ok[0]
    how = "名前が一致" if exact else "名前が表記ゆれ(ヶの有無)で一致"
    basis = f"{how}・標高 {m['ele_m']:.0f}m(ラベル {ele}m、差 {de:.0f}m)・{m['src']}『{m['name']}』"
    if len(exact_ok) > 1:
        others = ", ".join(f"{c[0]['name']} {c[0]['ele_m']:.0f}m {c[3]/1000:.0f}km" for c in exact_ok[1:])
        return m, basis + f"。名前と標高が合う候補がほかにもある({others})ため距離の近い方を採用", "不確か", len(cands)
    return m, basis, ("確か" if exact else "表記ゆれ"), len(cands)


# ---------------------------------------------------------------------------
# 出力の補助
# ---------------------------------------------------------------------------
def r(v, n=3):
    return None if v is None else round(float(v), n)


def vis_row(name, src, ele, res):
    return {"name": name, "src": src, "ele_m": ele, "azimuth_deg": r(res["azimuth_deg"], 3),
            "distance_km": r(res["distance_km"], 2), "summit_angle_deg": r(res["summit_angle_deg"], 3),
            "max_terrain_angle_deg": r(res["max_terrain_angle_deg"], 3),
            "max_terrain_at_km": r(res["max_terrain_at_km"], 2), "max_terrain_ele_m": r(res["max_terrain_ele_m"], 0),
            "margin_deg": r(res["margin_deg"], 3), "visible": res["visible"],
            "missing_samples": res["missing_samples"]}


def median(xs):
    return float(np.median(xs)) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="C:/temp/daibosatsu")
    ap.add_argument("--no-fetch", action="store_true", help="標高タイルを新しく取らない(手元のタイルだけで計算)")
    ap.add_argument("--max-new-tiles", type=int, default=400,
                    help="terrain_cache/dem_z11 のタイルの総数の上限(この作業の前は空だったので、新しく取った枚数と同じ)")
    ap.add_argument("--skip-bonus", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    log = []

    def say(msg):
        print(msg)
        log.append(msg)

    labels_path = os.path.join(args.data_dir, "peakfinder_labels.json")
    lab = json.load(open(labels_path, encoding="utf-8"))
    gps = load_gps_track(os.path.join(args.data_dir, lab["gpx"]["use"]))
    materials, pairs, gsi_file, n_gsi = load_materials()
    say(f"材料: 1003山 {n_gsi} 件({gsi_file})、MOUNTAINS {len(MOUNTAINS)} 座(うち 1003山に無い {len(materials) - n_gsi} 座)")

    dem = BudgetedDem(args.max_new_tiles, allow_fetch=not args.no_fetch)
    tiles_before = dem.files_on_disk()

    # ---- 視点 ----
    raiiwa_pt = next(p for p in lab["gpx"]["named_points"] if p["name"] == "雷岩")
    photos = [p for p in lab["photos"] if p["role"] == "primary"]
    bonus = [p for p in lab["photos"] if p["role"] == "next_stage"]
    raiiwa_photo = next((p for p in photos if g.vincenty_inverse(p["viewpoint"]["lat"], p["viewpoint"]["lon"],
                                                                raiiwa_pt["lat"], raiiwa_pt["lon"])[0] < 30), None)

    def gps_ele_at(time_jst):
        t = datetime.fromisoformat(time_jst)
        best = min(gps, key=lambda q: abs((q[0] - t).total_seconds()))
        return best[3], abs((best[0] - t).total_seconds())

    viewpoints = {}
    for p in photos + (bonus if not args.skip_bonus else []):
        key = p["file"].rsplit(".", 1)[0]
        ge, gdt = gps_ele_at(p["time_jst"])
        viewpoints[key] = {"lat": p["viewpoint"]["lat"], "lon": p["viewpoint"]["lon"],
                           "ele_photo": float(p["viewpoint"]["ele_m"]), "ele_gps": ge, "gps_dt_s": gdt,
                           "note": p["viewpoint"].get("note", ""), "photo": p}
    viewpoints["raiiwa"] = {"lat": raiiwa_pt["lat"], "lon": raiiwa_pt["lon"],
                            "ele_photo": float(raiiwa_photo["viewpoint"]["ele_m"]) if raiiwa_photo else None,
                            "ele_gps": float(raiiwa_pt["ele_gps_m"]), "gps_dt_s": 0,
                            "note": "雷岩(GPX の地点名)", "photo": None}

    # ---- タイルの計画と取得(雷岩からの扇形 → 候補の山への線の順) ----
    rv = {"lat": raiiwa_pt["lat"], "lon": raiiwa_pt["lon"]}
    plan = []
    seen = set()

    def add(ts):
        for t in ts:
            if t not in seen:
                seen.add(t)
                plan.append(t)
    add(tiles_on_ray(rv, 0, 200))
    for i in range(int(round((SKY_AZ[1] - SKY_AZ[0]) / SKY_AZ[2])) + 1):
        add(tiles_on_ray(rv, SKY_AZ[0] + i * SKY_AZ[2], SKY_MAX_M))
    n_sector = len(plan)
    far = []
    for m in materials:
        d, az = g.vincenty_inverse(rv["lat"], rv["lon"], m["lat"], m["lon"])
        if d <= FAR_MAX_M:
            far.append((d, az, m))
    far.sort(key=lambda t: t[0])
    for d, az, m in far:
        add(tiles_on_ray(rv, az, d))
    missing_plan = [t for t in plan if not dem.store._disk_path(*t).exists()]
    say(f"タイルの計画: 扇形(方位{SKY_AZ[0]:.0f}〜{SKY_AZ[1]:.0f}°・{SKY_MAX_M/1000:.0f}km){n_sector} 枚 + "
        f"200km 以内の候補 {len(far)} 座への線 {len(plan) - n_sector} 枚 = {len(plan)} 枚。"
        f"手元に無いもの {len(missing_plan)} 枚、すでにある {tiles_before} 枚、上限 {args.max_new_tiles} 枚")
    for i, t in enumerate(plan):
        dem.tile_ready(*t)
        if dem.stopped_reason:
            say(f"タイルの取得をやめた: {dem.stopped_reason}({i}/{len(plan)} 枚目)")
            break
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(plan)} 枚目(新しく取った {dem.store.downloaded_count} 枚)", flush=True)
    say(f"標高タイル: 新しく取った {dem.store.downloaded_count} 枚、失敗 {dem.store.failed_count} 回、"
        f"手元の総数 {dem.files_on_disk()} 枚(この作業の前は {tiles_before} 枚)")

    # ---- 視点の標高(3通り) ----
    for key, v in viewpoints.items():
        v["ele_dem"] = dem(v["lat"], v["lon"])
        if v["ele_photo"] is None:
            v["ele_photo"] = v["ele_gps"]
        v["ele_used"] = v["ele_photo"]
        v["ele_used_src"] = "写真の表示値" if key != "raiiwa" else "写真(雷岩で撮った1枚)の表示値"

    def vp(key, which="used"):
        v = viewpoints[key]
        return {"lat": v["lat"], "lon": v["lon"], "ele_m": v[f"ele_{which}"]}

    # ---- 答え合わせ(写真ごと) ----
    checks = {}
    targets_out = {}
    variant_changes = []
    labeled_ids = {}
    for p in photos + (bonus if not args.skip_bonus else []):
        key = p["file"].rsplit(".", 1)[0]
        V = vp(key)
        rows = []
        for i, L in enumerate(p["labels"]):
            m, basis, conf, ncand = match_label(L, materials, V)
            row = {"i": i, "label": L["text"], "label_ele_m": L["ele_m"], "x_px": L["x_px"],
                   "match": None, "basis": basis, "confidence": conf, "n_name_candidates": ncand}
            if m is not None:
                res = g.target_visibility(V, m, dem)
                row["match"] = {"id": m["id"], "src": m["src"], "name": m["name"], "ele_m": m["ele_m"]}
                row.update(vis_row(m["name"], m["src"], m["ele_m"], res))
                # 視点の標高 3 通り
                concl = {}
                for which in ("photo", "gps", "dem"):
                    if viewpoints[key][f"ele_{which}"] is None:
                        continue
                    rr = g.target_visibility(vp(key, which), m, dem)
                    concl[which] = {"margin_deg": r(rr["margin_deg"]), "visible": rr["visible"]}
                row["by_viewpoint_ele"] = concl
                if len({c["visible"] for c in concl.values()}) > 1:
                    variant_changes.append({"viewpoint": key, "name": m["name"], **concl})
            rows.append(row)
        matched = [x for x in rows if x["match"]]
        labeled_ids[key] = {x["match"]["id"] for x in matched}
        chk = {"photo": p["file"], "role": p["role"], "direction": p["direction"],
               "viewpoint": {k: viewpoints[key][k] for k in ("lat", "lon", "ele_photo", "ele_gps", "gps_dt_s",
                                                            "ele_dem", "ele_used", "ele_used_src", "note")},
               "n_labels": len(rows), "n_matched": len(matched), "rows": rows}
        if len(matched) >= 3:
            az = [x["azimuth_deg"] for x in matched]
            xs = [x["x_px"] for x in matched]
            fits = {"tan": g.fit_camera(az, xs), "tan_cx800": g.fit_camera(az, xs, cx_fixed=IMAGE_W / 2),
                    "linear": g.fit_linear(az, xs)}
            for k2 in ("tan", "tan_cx800"):
                fits[k2]["hfov_deg"] = g.horizontal_fov_deg(fits[k2]["f"], IMAGE_W)
            fits["linear"]["hfov_deg"] = IMAGE_W / fits["linear"]["b_px_per_deg"]
            for k2, fit in fits.items():
                for x, rp, rd in zip(matched, fit["residual_px"], fit["residual_deg"]):
                    x.setdefault("residual", {})[k2] = {"px": round(rp, 1), "deg": round(rd, 3)}
            chk["fits"] = fits
            mism = g.order_mismatches([(x["name"], x["x_px"], x["azimuth_deg"]) for x in matched])
            chk["order_mismatches"] = [{"left_in_photo": a, "right_in_photo": b, "az_left": r(c1), "az_right": r(c2)}
                                       for a, b, c1, c2 in mism]

            def rates(sel):
                n = len(sel)
                if not n:
                    return None
                res_px = [abs(x["residual"]["tan"]["px"]) for x in sel]
                vis = [x for x in sel if x["visible"]]
                return {"n": n, "residual_px_median": round(median(res_px), 1), "residual_px_max": round(max(res_px), 1),
                        "within_20px": sum(1 for v in res_px if v <= X_TOL_PX),
                        "within_20px_rate": round(sum(1 for v in res_px if v <= X_TOL_PX) / n, 3),
                        "visible": len(vis), "visible_rate": round(len(vis) / n, 3)}
            ok_sel = [x for x in matched if x["confidence"] != "不確か"]
            n_pairs = max(len(matched) - 1, 1)
            chk["scores"] = {
                "matched_over_labels": f"{len(matched)}/{len(rows)}",
                "order_pairs_ok": f"{n_pairs - len(mism)}/{n_pairs}",
                "order_rate": round((n_pairs - len(mism)) / n_pairs, 3),
                "all_matched": rates(matched), "without_uncertain": rates(ok_sel),
                "hfov_deg": {k2: round(f2["hfov_deg"], 2) for k2, f2 in fits.items()},
            }
            # 写る範囲(当てはめから)
            fit = fits["tan"]
            half = math.degrees(math.atan((IMAGE_W - fit["cx"]) / fit["f"]))
            halfl = math.degrees(math.atan(fit["cx"] / fit["f"]))
            chk["view_range_deg"] = [r((fit["az0_deg"] - halfl) % 360, 2), r((fit["az0_deg"] + half) % 360, 2)]
        checks[key] = chk
        say(f"[{key}] 対応 {len(matched)}/{len(rows)}" + (
            f"、並び順 {chk['scores']['order_pairs_ok']}、残差中央値 {chk['scores']['all_matched']['residual_px_median']}px、"
            f"画角 {chk['scores']['hfov_deg']['tan']}°" if "scores" in chk else ""))

    # ---- 写る範囲の中の候補(計算 A)と、PeakFinder が出さなかった山 ----
    for key, chk in checks.items():
        if "view_range_deg" not in chk:
            continue
        V = vp(key)
        lo_az, hi_az = chk["view_range_deg"]
        span = g.angle_diff_deg(hi_az, lo_az)
        fit = chk["fits"]["tan"]
        tgt = []
        for m in materials:
            d, az = g.vincenty_inverse(V["lat"], V["lon"], m["lat"], m["lon"])
            if d > FAR_MAX_M or d < 500:
                continue
            if not (0 <= g.angle_diff_deg(az, lo_az) <= span):
                continue
            res = g.target_visibility(V, m, dem)
            row = vis_row(m["name"], m["src"], m["ele_m"], res)
            row["id"] = m["id"]
            row["in_mountains"] = m["src"] == "MOUNTAINS" or bool(m.get("mountains_ids"))
            row["labeled"] = m["id"] in labeled_ids[key]
            row["x_pred_px"] = r(float(g.project_x([res["azimuth_deg"]], fit["cx"], fit["f"], fit["az0_deg"])[0]), 1)
            tgt.append(row)
        tgt.sort(key=lambda t: t["azimuth_deg"])
        targets_out[key] = tgt
        chk["not_labeled_but_visible"] = [t for t in tgt if t["visible"] and not t["labeled"]
                                          and (t["in_mountains"] or t["ele_m"] >= 2500)]

    # ---- 計算 B: 輪郭 ----
    skylines = {}
    for key in list(checks.keys()) + ["raiiwa"]:
        if key in checks and checks[key]["role"] == "next_stage":
            continue
        sk = g.skyline(vp(key), SKY_AZ[0], SKY_AZ[1], SKY_AZ[2], SKY_MAX_M, dem)
        skylines[key] = sk
        fn = f"skyline_{'raiiwa' if key == 'raiiwa' else key}.csv"
        with open(os.path.join(OUT_DIR, fn), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["azimuth_deg", "angle_deg", "distance_km", "ele_m", "missing_samples"])
            for s in sk:
                w.writerow([s["azimuth_deg"], r(s["angle_deg"], 4), r(s["distance_km"], 2), r(s["ele_m"], 0),
                            s["missing_samples"]])
    say(f"輪郭: {len(skylines)} 視点 × {len(skylines['raiiwa'])} 方位")

    def sky_at(key, az):
        sk = skylines.get(key)
        if not sk:
            return None
        i = int(round((az - SKY_AZ[0]) / SKY_AZ[2]))
        if 0 <= i < len(sk):
            return sk[i]
        return None

    for key, chk in checks.items():
        for x in chk["rows"]:
            if not x.get("match"):
                continue
            s = sky_at(key, x["azimuth_deg"])
            if s is None or s["angle_deg"] is None:
                continue
            rel = s["distance_km"] / x["distance_km"]
            if abs(s["distance_km"] - x["distance_km"]) <= max(3.0, 0.05 * x["distance_km"]):
                verdict = "その山が輪郭を作っている"
            elif s["distance_km"] > x["distance_km"]:
                verdict = "山頂は手前にあり、輪郭はその奥のもっと遠い山が作っている(山は輪郭の下に重なって見える)"
            else:
                verdict = "手前の地形が輪郭を作っている"
            x["skyline_at_az"] = {"angle_deg": r(s["angle_deg"]), "distance_km": s["distance_km"],
                                  "ele_m": s["ele_m"], "verdict": verdict, "dist_ratio": r(rel, 2)}

    # ---- 雷岩からの計算(5.4) ----
    RV = vp("raiiwa")
    rai_all = []
    for d, az, m in far:
        res = g.target_visibility(RV, m, dem)
        row = vis_row(m["name"], m["src"], m["ele_m"], res)
        row["id"] = m["id"]
        row["in_mountains"] = m["src"] == "MOUNTAINS" or bool(m.get("mountains_ids"))
        rai_all.append(row)
    targets_out["raiiwa"] = rai_all

    def find_named(qname, hint_ele=None):
        q = norm(qname)
        hits = [m for m in materials if q in m["variants"] or any(q == norm(v) for v in m["variants"])
                or norm(m["name"]).startswith(q)]
        if qname == "甲斐駒ヶ岳" or qname == "木曽駒ヶ岳":
            hits += [m for m in materials if "駒ヶ岳" in m["variants"]]
        if qname == "悪沢岳":
            hits += [m for m in materials if "悪沢岳" in m["variants"]]
        if not hits:
            return None, "未取得", []
        if hint_ele:
            close = [m for m in hits if abs(m["ele_m"] - hint_ele) <= ELE_TOL_M]
            if close:
                hits = close
        hits = list({m["id"]: m for m in hits}.values())
        hits.sort(key=lambda m: g.vincenty_inverse(RV["lat"], RV["lon"], m["lat"], m["lon"])[0])
        return hits[0], ("一意" if len(hits) == 1 else "同名あり・最寄りを採用"), hits

    famous = []
    for qn in ["甲斐駒ヶ岳", "木曽駒ヶ岳", "牛奥ノ雁ヶ腹摺山", "雁ヶ腹摺山", "笹子雁ヶ腹摺山"] + FAMOUS:
        m, how, hits = find_named(qn, FAMOUS_HINT_ELE.get(qn))
        row = {"query": qn, "how": how}
        if m:
            res = g.target_visibility(RV, m, dem)
            row.update(vis_row(m["name"], m["src"], m["ele_m"], res))
            row["id"] = m["id"]
            row["other_same_name"] = [f"{h['name']} {h['ele_m']:.0f}m" for h in hits[1:4]]
        famous.append(row)

    def frow(q):
        return next(f for f in famous if f["query"] == q)
    kai, kiso = frow("甲斐駒ヶ岳"), frow("木曽駒ヶ岳")
    q1 = {"kai": kai, "kiso": kiso}
    if kai.get("azimuth_deg") is not None and kiso.get("azimuth_deg") is not None:
        dd = g.angle_diff_deg(kiso["azimuth_deg"], kai["azimuth_deg"])
        q1["kiso_relative_to_kai"] = {"deg": round(dd, 2), "side": "右" if dd > 0 else "左"}
    # 雁ヶ腹摺山の問い(5.4 の 2)は、富士山のラベルがある写真の写る範囲で答える
    photo_fuji = next((k for k, c in checks.items() if c["role"] == "primary"
                       and any("富士山" in x["label"] for x in c["rows"])), None)
    q2 = {"photo_with_fuji": checks[photo_fuji]["direction"] if photo_fuji else None,
          "view_range_deg": checks[photo_fuji].get("view_range_deg") if photo_fuji else None,
          "items": [frow(q) for q in ("牛奥ノ雁ヶ腹摺山", "雁ヶ腹摺山", "笹子雁ヶ腹摺山")]}
    vis_far = [x for x in rai_all if x["visible"]]
    vis_far_sorted = sorted(vis_far, key=lambda x: -x["summit_angle_deg"])
    top = vis_far_sorted[:60]
    top.sort(key=lambda x: x["azimuth_deg"])

    # ---- 視点の標高で結論が変わる目印(雷岩・200km 以内の全候補) ----
    for d, az, m in far:
        concl = {}
        for which in ("photo", "gps", "dem"):
            if viewpoints["raiiwa"][f"ele_{which}"] is None:
                continue
            rr = g.target_visibility(vp("raiiwa", which), m, dem)
            concl[which] = {"margin_deg": r(rr["margin_deg"]), "visible": rr["visible"]}
        if len({c["visible"] for c in concl.values()}) > 1:
            variant_changes.append({"viewpoint": "raiiwa", "name": m["name"], **concl})

    # ---- 書き出し ----
    with open(os.path.join(OUT_DIR, "targets.json"), "w", encoding="utf-8") as fh:
        json.dump({"note": "視点ごと・目印ごとの計算結果(5.1)。視点の標高は写真の表示値+目の高さ1.6m。",
                   "params": {"eye_m": g.EYE_HEIGHT_M, "k": mt.REFRACTION_K, "steps": g.DEFAULT_STEPS,
                              "near_skip_m": g.NEAR_SKIP_M, "target_end_skip_m": g.TARGET_END_SKIP_M,
                              "dem_zoom": mt.DEM_ZOOM},
                   "viewpoints": {k: {kk: vv for kk, vv in v.items() if kk != "photo"} for k, v in viewpoints.items()},
                   "targets": targets_out, "viewpoint_elevation_changes": variant_changes},
                  fh, ensure_ascii=False, indent=1, default=str)
    for key, chk in checks.items():
        with open(os.path.join(OUT_DIR, f"check_{key}.json"), "w", encoding="utf-8") as fh:
            json.dump(chk, fh, ensure_ascii=False, indent=1, default=str)
    answers = {"q1_komagatake": q1, "q2_gangahara": q2, "q3_famous": [frow(q) for q in FAMOUS],
               "q4_visible_within_200km": {"n_visible": len(vis_far), "n_candidates": len(rai_all), "top60_by_azimuth": top},
               "q5_not_labeled": {k: c.get("not_labeled_but_visible", []) for k, c in checks.items()},
               "materials_pairs": pairs, "pairs_over_300m": [p_ for p_ in pairs if p_["offset_m"] > 300],
               "viewpoint_elevation_changes": variant_changes,
               "tiles": {"before": tiles_before, "new": dem.store.downloaded_count, "failed": dem.store.failed_count,
                         "refused": dem.refused, "stopped": dem.stopped_reason, "on_disk": dem.files_on_disk(),
                         "statuses": {str(s): dem.session.statuses.count(s) for s in set(dem.session.statuses)}},
               "elapsed_s": round(time.time() - t0, 1), "log": log}
    with open(os.path.join(OUT_DIR, "answers.json"), "w", encoding="utf-8") as fh:
        json.dump(answers, fh, ensure_ascii=False, indent=1, default=str)

    for key, chk in checks.items():
        if "fits" in chk:
            write_overlay(key, chk, targets_out.get(key, []), args.data_dir)
    write_skyline_svg(skylines["raiiwa"], [frow(q) for q in FAMOUS] + [kiso], top)
    say(f"書き出し: {OUT_DIR}(所要 {time.time() - t0:.0f} 秒)")


# ---------------------------------------------------------------------------
# 確認ページ(1ファイルで完結。写真は data-dir のファイルを file:/// で参照する)
# ---------------------------------------------------------------------------
def write_overlay(key, chk, targets, data_dir):
    fit = chk["fits"]["tan"]
    photo_url = Path(os.path.join(data_dir, chk["photo"])).resolve().as_uri()
    lines = []
    lab_y = 40
    # PeakFinder のラベル位置(灰色の破線)
    for x in chk["rows"]:
        lines.append(f'<line x1="{x["x_px"]}" y1="560" x2="{x["x_px"]}" y2="760" class="pf"/>')
    # 計算した位置(対応が付いた山)
    k = 0
    for x in chk["rows"]:
        if not x.get("match"):
            continue
        xp = float(g.project_x([x["azimuth_deg"]], fit["cx"], fit["f"], fit["az0_deg"])[0])
        y = 760 + (k % 4) * 26
        cls = "calc" if x["visible"] else "calcneg"
        lines.append(f'<line x1="{xp:.1f}" y1="600" x2="{xp:.1f}" y2="{y}" class="{cls}"/>'
                     f'<text x="{xp + 3:.1f}" y="{y + 16}" class="{cls}t">{html.escape(x["name"])} '
                     f'{x["ele_m"]:.0f}m ({x["residual"]["tan"]["px"]:+.0f}px)</text>')
        k += 1
    # 計算では見えるが PeakFinder のラベルが無い山
    k = 0
    for t in chk.get("not_labeled_but_visible", []):
        xp = t["x_pred_px"]
        y = 470 - (k % 5) * 24
        lines.append(f'<line x1="{xp:.1f}" y1="{y + 4}" x2="{xp:.1f}" y2="700" class="extra"/>'
                     f'<text x="{xp + 3:.1f}" y="{y}" class="extrat">{html.escape(t["name"])} {t["ele_m"]:.0f}m '
                     f'{t["distance_km"]:.0f}km</text>')
        k += 1
    s = chk["scores"]
    rows_html = "".join(
        f"<tr><td>{x['x_px']}</td><td>{html.escape(x['label'])}</td><td>{html.escape(x['match']['name']) if x.get('match') else '—'}</td>"
        f"<td>{x['confidence']}</td><td>{'' if not x.get('match') else format(x['azimuth_deg'], '.2f')}</td>"
        f"<td>{'' if not x.get('match') else format(x['distance_km'], '.1f')}</td>"
        f"<td>{'' if not x.get('match') else format(x['margin_deg'], '+.3f')}</td>"
        f"<td>{'' if not x.get('match') else format(x['residual']['tan']['px'], '+.0f')}</td>"
        f"<td>{html.escape(x['basis'])}</td></tr>" for x in chk["rows"])
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>答え合わせ {html.escape(key)}</title>
<style>
:root{{--bg:#fff;--fg:#111;--mut:#666}}
@media (prefers-color-scheme: dark){{:root{{--bg:#111;--fg:#eee;--mut:#aaa}}}}
body{{background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;margin:16px;max-width:1640px}}
.wrap{{position:relative;width:100%;max-width:1600px}}
.wrap img{{width:100%;display:block}}
.wrap svg{{position:absolute;left:0;top:0;width:100%;height:100%}}
line.pf{{stroke:#fff;stroke-width:3;stroke-dasharray:8 6;opacity:.9}}
line.calc{{stroke:#ff3b30;stroke-width:2}} text.calct{{fill:#ff3b30;font-size:18px;font-weight:700;paint-order:stroke;stroke:#000;stroke-width:3px}}
line.calcneg{{stroke:#ff9500;stroke-width:2;stroke-dasharray:3 3}} text.calcnegt{{fill:#ff9500;font-size:18px;font-weight:700;paint-order:stroke;stroke:#000;stroke-width:3px}}
line.extra{{stroke:#34c759;stroke-width:2}} text.extrat{{fill:#34c759;font-size:17px;font-weight:700;paint-order:stroke;stroke:#000;stroke-width:3px}}
table{{border-collapse:collapse;font-size:13px;margin-top:12px}} td,th{{border:1px solid #8884;padding:2px 6px}}
.legend span{{margin-right:16px}} .mut{{color:var(--mut)}}
</style></head><body>
<h1 style="font-size:20px">答え合わせ: {html.escape(chk['direction'])}</h1>
<p class="legend"><span style="color:#888">白の破線 = PeakFinder の引き出し線(目測の x_px)</span>
<span style="color:#ff3b30">赤 = 計算した方位から出した位置(材料の山名・残差)</span>
<span style="color:#ff9500">橙の点線 = 計算では手前の地形に隠れる</span>
<span style="color:#34c759">緑 = 計算では見えるが PeakFinder のラベルが無い山(MOUNTAINS の山か 2500m 以上)</span></p>
<p class="mut">当てはめ x = cx + f·tan(方位 − 中心): cx={fit['cx']:.0f}, f={fit['f']:.0f}, 中心の方位={fit['az0_deg']:.2f}°, 横の画角={fit['hfov_deg']:.1f}°。
対応 {s['matched_over_labels']}、並び順 {s['order_pairs_ok']}、残差 中央値 {s['all_matched']['residual_px_median']}px・最大 {s['all_matched']['residual_px_max']}px。
写真は {html.escape(photo_url)} を参照(見えなければ、写真のフォルダが動いていないか確かめる)。</p>
<div class="wrap"><img src="{html.escape(photo_url)}" alt="写真"><svg viewBox="0 0 {IMAGE_W} {IMAGE_H}" preserveAspectRatio="none">{''.join(lines)}</svg></div>
<table><tr><th>x_px</th><th>PeakFinder のラベル</th><th>材料の山</th><th>確かさ</th><th>方位°</th><th>距離km</th><th>余裕°</th><th>残差px</th><th>根拠</th></tr>{rows_html}</table>
</body></html>"""
    with open(os.path.join(OUT_DIR, f"overlay_{key}.html"), "w", encoding="utf-8") as fh:
        fh.write(page)


def write_skyline_svg(sk, named, top):
    W, H = 1600, 420
    az0, az1 = SKY_AZ[0], SKY_AZ[1]
    angs = [s["angle_deg"] for s in sk if s["angle_deg"] is not None]
    amin, amax = min(angs) - 0.3, max(angs) + 1.6

    def X(az):
        return 40 + (az - az0) / (az1 - az0) * (W - 80)

    def Y(a):
        return H - 30 - (a - amin) / (amax - amin) * (H - 60)
    pts = " ".join(f"{X(s['azimuth_deg']):.1f},{Y(s['angle_deg']):.1f}" for s in sk if s["angle_deg"] is not None)
    marks = []
    seen = set()
    k = 0
    for n in named:
        if n.get("azimuth_deg") is None or n["id"] in seen:
            continue
        seen.add(n["id"])
        if not (az0 <= n["azimuth_deg"] <= az1):
            continue
        x, y = X(n["azimuth_deg"]), Y(n["summit_angle_deg"])
        col = "#c00" if n["visible"] else "#999"
        ty = 24 + (k % 5) * 16
        marks.append(f'<line x1="{x:.1f}" y1="{ty + 3}" x2="{x:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="0.8"/>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{col}"/>'
                     f'<text x="{x + 2:.1f}" y="{ty}" font-size="12" fill="{col}">{html.escape(n["name"])} {n["distance_km"]:.0f}km</text>')
        k += 1
    ticks = "".join(f'<line x1="{X(a):.1f}" y1="{H - 30}" x2="{X(a):.1f}" y2="{H - 24}" stroke="#888"/>'
                    f'<text x="{X(a):.1f}" y="{H - 10}" font-size="11" text-anchor="middle" fill="#888">{a}°</text>'
                    for a in range(int(az0), int(az1) + 1, 10))
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<rect width="{W}" height="{H}" fill="#fff"/>
<text x="40" y="14" font-size="12" fill="#333">雷岩からの稜線の輪郭(方位 {az0:.0f}〜{az1:.0f}°、{SKY_MAX_M/1000:.0f}km まで、縦は見上げ角を強調。赤=見える・灰=隠れる)</text>
<polyline points="{pts}" fill="none" stroke="#246" stroke-width="1.2"/>
<line x1="40" y1="{Y(0):.1f}" x2="{W - 40}" y2="{Y(0):.1f}" stroke="#aaa" stroke-dasharray="4 4"/>
<text x="{W - 38}" y="{Y(0) - 3:.1f}" font-size="10" fill="#888">水平 0°</text>
{ticks}{''.join(marks)}
</svg>"""
    with open(os.path.join(OUT_DIR, "skyline_raiiwa.svg"), "w", encoding="utf-8") as fh:
        fh.write(svg)


if __name__ == "__main__":
    main()
