"""展望図の部品: 山頂の点・OSM の展望地(tourism=viewpoint)の選び方(段階2.5 の決まり)。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

import math
import re

from . import geometry  # noqa: E402


# ---- 元: experiments/panorama/stage21_osm_viewpoints.py ----

SAME_PLACE_M = 30.0

OTHER_TAGS = ("natural", "man_made", "tower:type", "description", "access")

ATTRIBUTION = "© OpenStreetMap contributors(ODbL)"

CARDINALS = {n: i * 22.5 for i, n in enumerate(
    ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"])}

_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")

def _one(tok: str):
    t = tok.strip().upper()
    if t in CARDINALS:
        return CARDINALS[t]
    if _NUM.match(t):
        return float(t) % 360.0 if float(t) != 360.0 else 360.0
    return None

def parse_direction(raw):
    """OSM の direction の値を、度の範囲のリストにする。読めなければ None。

    範囲は {"from": 始まり, "to": 終わり, "width": 幅} で、from から時計回りに to まで(0〜360)。
    - 1 つの値(数値・16 方位)は幅 0(向きだけ。扇の広さは作らない)。
    - "a-b" は a から時計回りに b まで("330-30" は 60 度、"S-W" は 180→270)。"0-360" は全周(幅 360)。
    - ";" か "," で区切った複数の値・範囲。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    out = []
    for part in re.split(r"[;,]", s):
        part = part.strip()
        if not part:
            continue
        # 負の数の "-" と範囲の "-" を分ける: 先頭の "-" は数の一部として扱う
        m = re.match(r"^\s*(-?[^-\s]+)\s*-\s*([^-\s]+)\s*$", part)
        if m:
            a, b = _one(m.group(1)), _one(m.group(2))
            if a is None or b is None:
                return None
            a = a % 360.0
            if b == 360.0 and a == 0.0:
                out.append({"from": 0.0, "to": 360.0, "width": 360.0})
                continue
            b = b % 360.0
            w = (b - a) % 360.0
            if w == 0.0:
                w = 360.0
            out.append({"from": a, "to": b if w < 360.0 else a, "width": w})
        else:
            v = _one(part)
            if v is None:
                return None
            v = v % 360.0
            out.append({"from": v, "to": v, "width": 0.0})
    return out or None

def direction_text(ranges) -> str:
    if not ranges:
        return ""
    parts = []
    for r in ranges:
        if r["width"] >= 360.0:
            parts.append("全周")
        elif r["width"] == 0.0:
            parts.append(f"{r['from']:g}°")
        else:
            parts.append(f"{r['from']:g}°→{r['to']:g}°(幅{r['width']:g}°)")
    return "・".join(parts)

AZ_NAMES = ["北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
            "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西"]

def az_name(az: float) -> str:
    return AZ_NAMES[int(((az % 360.0) + 11.25) // 22.5) % 16]

def element_point(el: dict):
    """Overpass の要素から (lat, lon)。way は out center の center。無ければ None。"""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    if c and "lat" in c and "lon" in c:
        return c["lat"], c["lon"]
    return None

def within(center: dict, elements: list, max_m: float) -> list:
    """center から max_m 以内の要素を、距離の近い順に。各要素に dist_m・az_deg を付けた写しを返す。"""
    lat0, lon0 = center["lat"], center["lon"]
    dlat = max_m / 111000.0 * 1.2 + 1e-6
    dlon = dlat / max(0.1, math.cos(math.radians(lat0)))
    out = []
    for el in elements:
        pt = element_point(el)
        if pt is None:
            continue
        la, lo = pt
        if abs(la - lat0) > dlat or abs(lo - lon0) > dlon:
            continue
        d, az = geometry.vincenty_inverse(lat0, lon0, la, lo)
        if d <= max_m:
            out.append(dict(el, lat=la, lon=lo, dist_m=float(d), az_deg=float(az) % 360.0))
    out.sort(key=lambda e: e["dist_m"])
    return out

def is_same_dist(dist_m: float, tol_m: float = SAME_PLACE_M) -> bool:
    return dist_m <= tol_m

def vp_record(h: dict, center: dict, elev) -> dict:
    tags = h.get("tags", {})
    rng = parse_direction(tags.get("direction"))
    rec = {"osm": f"{h['type']}/{h['id']}", "name": tags.get("name") or "名前なし", "has_name": bool(tags.get("name")),
           "name_ja": tags.get("name:ja"), "dist_m": round(h["dist_m"], 1), "az_deg": round(h["az_deg"], 1),
           "az_name": az_name(h["az_deg"]), "direction_raw": tags.get("direction"), "direction": rng,
           "direction_text": direction_text(rng) if rng else ("(読めない)" if tags.get("direction") else ""),
           "ele_tag": tags.get("ele"), "same_place": is_same_dist(h["dist_m"]),
           "tags": {k: tags[k] for k in OTHER_TAGS if k in tags}, "lat": h["lat"], "lon": h["lon"]}
    e0 = e1 = z = None
    if elev is not None:
        e0, e1, z = elev.pair(center["lat"], center["lon"], h["lat"], h["lon"])
    rec.update(dem_zoom=z, dem_center_m=None if e0 is None else round(e0, 1),
               dem_vp_m=None if e1 is None else round(e1, 1),
               dem_diff_m=None if e0 is None else round(e1 - e0, 1))
    return rec


# ---- 元: experiments/panorama/viewpoints.py ----

VP_RADIUS_M = 300.0     # 利用者の決定

VP_MIN_DIFF_M = -50.0   # 利用者の決定(50m を超えて低いものは使わない)


SRC_SUMMIT = "summit_gsi"

SRC_MOUNTAINS = "mountains_coord"

SRC_OSM = "osm_viewpoint"

def eligible(cands: list, radius_m: float = VP_RADIUS_M, min_diff_m: float = VP_MIN_DIFF_M) -> list:
    """cands: [{"name", "has_name", "dist_m", "dem_diff_m", ...}]。半径と標高差の条件に合うもの。
    標高差が未取得(None)のものは使わない(条件を確かめられないため)。"""
    out = []
    for v in cands:
        if v["dist_m"] > radius_m:
            continue
        if v.get("dem_diff_m") is None or v["dem_diff_m"] < min_diff_m:
            continue
        out.append(v)
    return out

def choose(cands: list, radius_m: float = VP_RADIUS_M, min_diff_m: float = VP_MIN_DIFF_M):
    """決まりのとおりに 1 つ選ぶ。返り値: (選んだ点 or None, 条件に合った点のリスト)。"""
    ok = eligible(cands, radius_m, min_diff_m)
    if not ok:
        return None, ok
    best = sorted(ok, key=lambda v: (0 if v["has_name"] else 1, v["dist_m"]))[0]
    return best, ok

def is_same_place(v, tol_m: float = SAME_PLACE_M) -> bool:
    return v is not None and v["dist_m"] <= tol_m

def source_text(vp: dict) -> str:
    """図に書く「視点の出どころ」。"""
    if vp["source"] == SRC_SUMMIT:
        s = "視点: 山頂(国土地理院の山頂の点)"
    elif vp["source"] == SRC_MOUNTAINS:
        s = "視点: ヤマビヨリの座標(山頂の点ではない)"
    else:
        base = "山頂" if vp.get("center_is_summit") else "ヤマビヨリの座標"
        s = f"視点: {vp['label']}(OpenStreetMap の展望地。{base}の{vp['az_name']} {vp['dist_m']:.0f}m)"
        if not vp.get("center_is_summit"):
            s += "。山頂の点から測った距離ではない"
    if vp.get("borrowed_name"):
        b = vp["borrowed_name"]
        s += f"。OpenStreetMap の展望地「{b['name']}」({b['dist_m']:.0f}m)と同じ場所"
    if vp.get("name_unverified"):
        s += "(展望地の名前の意味は未確認)"
    return s
