"""展望図の部品: 予報の取得(本体の get_with_retry)と雲の値・1 つの視点・1 時刻の場面(compute_scene)。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import mountain_weather_core as core
import os
import re
import time

from . import config, geometry, terrain, viewpoint, clouds  # noqa: E402


# ---- 元: experiments/panorama/stage3_weather.py ----

def r0(v):
    """表と facts.md で同じ丸め(整数の %)。"""
    return None if v is None else int(round(float(v)))

def short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


# ---- 元: experiments/panorama/stage35_fetch.py ----

GRID_AZ_STEP = 10.0                        # 指示書 3.2-2(超えそうなら 20°)

GRID_DISTS_KM = (20.0, 50.0, 100.0)        # 指示書 3.2-2(超えそうなら 100km を外す)

CLOUD_VARS = ["cloudcover", "cloudcover_low", "cloudcover_mid", "cloudcover_high"]

HOURLY35 = CLOUD_VARS + ["visibility"] + core.level_stack_vars(kinds=["cloudcover"])

LEVEL_CLOUD_VARS = [core.level_var("cloudcover", h) for h in core.LEVEL_STACK_HPA]

def grid_points(lat, lon, az_step=GRID_AZ_STEP, dists_km=GRID_DISTS_KM) -> list:
    """方角の格子: 方位 az_step ごと × 距離。返り値: [{"az", "dist_km", "lat", "lon"}](方位の順、同じ方位は近い順)。"""
    out = []
    n = int(round(360.0 / az_step))
    for i in range(n):
        az = i * az_step
        for dk in dists_km:
            la, lo = terrain.dest_points(lat, lon, az, dk * 1000.0)
            out.append({"az": az, "dist_km": dk, "lat": float(la), "lon": float(lo)})
    return out

def needs_ecmwf(msm: dict, hours: list) -> bool:
    """使う時刻(hours: "YYYY-MM-DDTHH:00" の list)に、MSM の雲の値(総・低・中・高・気圧面ごと)が 1 つでも欠けていれば True。"""
    if not msm:
        return True
    h = msm.get("hourly") or {}
    times = h.get("time") or []
    for t in hours:
        if t not in times:
            return True
        i = times.index(t)
        for var in CLOUD_VARS + LEVEL_CLOUD_VARS:
            col = h.get(var) or []
            if i >= len(col) or col[i] is None:
                return True
    return False

def merge35(msm: dict, ecmwf: dict) -> dict:
    """w.merge_models と同じ順(MSM の値が無い時間は ECMWF)。変数は HOURLY35。応答の elevation(格子の標高)も残す。"""
    mh, eh = (msm or {}).get("hourly") or {}, (ecmwf or {}).get("hourly") or {}
    times = mh.get("time") or eh.get("time") or []
    eidx = {t: i for i, t in enumerate(eh.get("time") or [])}
    out = {"time": list(times)}
    src = []
    for i, t in enumerate(times):
        src.append("MSM")
    for var in HOURLY35:
        mv, ev = mh.get(var) or [], eh.get(var) or []
        col = []
        for i, t in enumerate(times):
            v = mv[i] if i < len(mv) else None
            if v is None:
                j = eidx.get(t)
                v = ev[j] if j is not None and j < len(ev) else None
                if v is not None and var in CLOUD_VARS:
                    src[i] = "ECMWF"
            col.append(v)
        out[var] = col
    out["_model"] = src
    out["_grid_elevation_m"] = (msm or {}).get("elevation", (ecmwf or {}).get("elevation"))
    out["_grid_latlon"] = ((msm or ecmwf or {}).get("latitude"), (msm or ecmwf or {}).get("longitude"))
    return out


# ---- 元: experiments/panorama/stage35_sky.py ----

SKY_ROWS = ("viewer", 20.0, 50.0, 100.0)    # 上から下へ(下ほど遠い)

SEA_DISTS_KM = (20.0, 50.0)

def sky_cover(mid, high):
    vals = [v for v in (mid, high) if v is not None]
    return max(vals) if vals else None

def levels_at(hourly: dict, idx: int) -> list:
    """その時刻の気圧面ごとの (高さ m, 雲量 %, hPa, 高さは標準大気か)。高さの低い順。雲量の無い面は入れない。
    気圧面の高さ(geopotential_height)が無い面は標準大気の高さで置く(std=True)。"""
    out = []
    for hpa in core.LEVEL_STACK_HPA:
        cv = (hourly.get(core.level_var("cloudcover", hpa)) or [None] * (idx + 1))
        zv = (hourly.get(core.level_var(core.GPH_KIND, hpa)) or [None] * (idx + 1))
        c = cv[idx] if idx < len(cv) else None
        if c is None:
            continue
        z = zv[idx] if idx < len(zv) else None
        std = z is None
        if std:
            z = core.pressure_level_altitude_m(hpa)
        out.append((float(z), float(c), hpa, std))
    out.sort()
    return out

def cloud_at_altitude(hourly: dict, idx: int, alt_m: float):
    """本体の interp_at_altitude(線形、範囲の外は端の値)。"""
    return core.interp_at_altitude(hourly, "cloudcover", idx, alt_m)

def summit_cloud(hourly: dict, idx: int, alt_m: float):
    """展望図の山頂の雲量(2026-11-20 の決定): 山頂の高さをはさむ 2 つの気圧面の雲量のうち、大きいほう。
    山頂がいちばん下の面より低い・いちばん上の面より高いときは、これまでどおり端の面の値(本体の interp_at_altitude と同じ)。
    診断モードの文・--json-out の雲量(本体の計算)は変えない。展望図の中(名前の横の「雲 NN%」・名前のうすさ・
    山にかかる丸い雲の大きさと濃さ)だけで使う。"""
    pts = core.level_profile(hourly, "cloudcover", idx)
    if not pts:
        return None
    if alt_m <= pts[0][0]:
        return pts[0][1]
    if alt_m >= pts[-1][0]:
        return pts[-1][1]
    for (z0, v0), (z1, v1) in zip(pts, pts[1:]):
        if z0 <= alt_m <= z1:
            return max(v0, v1)
    return None

def sky_grid(values: dict, az_step: float = 10.0) -> dict:
    """values: {(段, 方位): 雲量 or None}。段は SKY_ROWS の要素(視点は方位に関わらず同じ値)。
    返り値: {"rows": [[雲量 or None]*方位の数]*4, "azs": [...]}(上の段から)。"""
    n = int(round(360.0 / az_step))
    azs = [i * az_step for i in range(n)]
    rows = []
    for r in SKY_ROWS:
        if r == "viewer":
            v = values.get(("viewer", None))
            rows.append([v] * n)
        else:
            rows.append([values.get((r, a)) for a in azs])
    return {"rows": rows, "azs": azs, "az_step": az_step}

def sky_value_at(grid: dict, az: float, row_f: float):
    """方位 az(度)・段 row_f(0 = いちばん上の段の中心、3 = いちばん下の段の中心。外は端に寄せる)の雲量。
    いちばん近い点(区画の持ち主)が予報なしなら None。そうでなければ、予報のある近くの点だけで線形に補間。"""
    step = grid["az_step"]
    n = len(grid["azs"])
    rows = grid["rows"]
    rf = max(0.0, min(len(rows) - 1.0, row_f))
    r0 = int(math.floor(rf))
    r1 = min(len(rows) - 1, r0 + 1)
    tr = rf - r0
    a = (az % 360.0) / step
    i0 = int(math.floor(a)) % n
    i1 = (i0 + 1) % n
    ta = a - math.floor(a)
    own_r = r0 if tr < 0.5 else r1
    own_i = i0 if ta < 0.5 else i1
    if rows[own_r][own_i] is None:
        return None
    acc, wsum = 0.0, 0.0
    for rr, wr in ((r0, 1 - tr), (r1, tr)):
        for ii, wi in ((i0, 1 - ta), (i1, ta)):
            v = rows[rr][ii]
            wgt = wr * wi
            if v is None or wgt <= 0:
                continue
            acc += v * wgt
            wsum += wgt
    return acc / wsum if wsum > 0 else rows[own_r][own_i]

def sky_color(cov):
    """雲量 0% = 青、100% = 白(段階3 と同じ色の式)。"""
    t = max(0.0, min(1.0, cov / 100.0))
    h, s, l = 207.0, 72.0 * (1 - t), 74.0 + (96.0 - 74.0) * t
    return f"hsl({h:.0f} {s:.0f}% {l:.0f}%)"

def sea_levels(levels: list, viewer_m: float, ground_m, start_pct: float) -> list:
    """視点より低い気圧面のうち、雲量が描き始めの値以上のもの。地面より低い面は below_ground=True。
    levels: levels_at の返り値。返り値: [{"alt_m", "c", "hpa", "std", "below_ground"}](低い順)。"""
    out = []
    for alt, c, hpa, std in levels:
        if alt >= viewer_m or c < start_pct:
            continue
        out.append({"alt_m": alt, "c": c, "hpa": hpa, "std": std,
                    "below_ground": ground_m is not None and alt < float(ground_m)})
    return out

def sea_top(decks: list, include_below_ground: bool = False):
    """雲海の上面の目安 = 雲のある面のうちいちばん高い面の高さ(気圧面の刻みが粗いので目安)。無ければ None。"""
    v = [d["alt_m"] for d in decks if include_below_ground or not d["below_ground"]]
    return max(v) if v else None

def under_sea(summit_m: float, top_m) -> bool:
    return top_m is not None and summit_m < top_m

def los_altitude(viewer_m: float, summit_m: float, d_km: float, dist_km: float) -> float:
    """視線の高さ: 視点の高さと山頂の高さを、距離で按分(地球の丸みは入れない。指示書 5.3 のとおり)。"""
    f = max(0.0, min(1.0, d_km / dist_km))
    return viewer_m + (summit_m - viewer_m) * f

def nearest_grid_az(az: float, step: float = 10.0) -> float:
    return (round((az % 360.0) / step) * step) % 360.0

def path_cloud(viewer_m: float, summit_m: float, dist_km: float, points: list):
    """points: [(格子の距離 km, その点の hourly, 時刻の index)](いちばん近い方位の点)。山より手前の点だけ使う。
    返り値: {"max": 最大 or None, "used": [(距離, 視線の高さ, 雲量)]}。手前の点が無ければ max=None(途中の点なし)。"""
    used = []
    for d_km, hourly, idx in points:
        if d_km >= dist_km or hourly is None:
            continue
        alt = los_altitude(viewer_m, summit_m, d_km, dist_km)
        c = cloud_at_altitude(hourly, idx, alt)
        used.append((d_km, round(alt, 1), c))
    vals = [c for _, _, c in used if c is not None]
    return {"max": max(vals) if vals else None, "used": used}


# ---- 元: experiments/panorama/stage35_run.py ----

CREDIT35 = "気象データ出典: Open-Meteo(気象庁MSM、無い時間は ECMWF IFS 0.25)"

def source_text(v):
    return viewpoint.source_text(v)

def idx_of(fc, hour):
    if fc is None:
        return None
    try:
        return fc["time"].index(hour)
    except ValueError:
        return None

def grid_forecasts(lat, lon, day, hour):
    """格子の 108 点の予報。{(距離, 方位): (hourly, idx)}。"""
    out = {}
    for g in grid_points(lat, lon):
        fc = get_fc(g["lat"], g["lon"], day, [hour])
        out[(g["dist_km"], g["az"])] = (fc, idx_of(fc, hour))
    return out

def point_summary(fc, idx):
    if fc is None or idx is None:
        return None
    h = fc
    sc = sky_cover(h["cloudcover_mid"][idx], h["cloudcover_high"][idx])
    return {"total": h["cloudcover"][idx], "low": h["cloudcover_low"][idx], "mid": h["cloudcover_mid"][idx],
            "high": h["cloudcover_high"][idx], "sky": sc, "model": (h.get("_model") or ["MSM"] * (idx + 1))[idx],
            "levels": levels_at(h, idx), "ground_m": h.get("_grid_elevation_m")}

def _scene_base(viewer_lat, viewer_lon, viewer_m, day, hour, grid_center, mountains, start_pct=clouds.START_PCT):
    """mountains: [{"key", "label", "lat", "lon", "elev_m", "az", "dist_km"}](az は視点から、0〜360)。
    返り値: 視点・空の 4 段・雲海・山ごとの値(表と図の材料)。"""
    vfc = get_fc(viewer_lat, viewer_lon, day, [hour])
    vi = idx_of(vfc, hour)
    vs = point_summary(vfc, vi)
    gfc = grid_forecasts(grid_center[0], grid_center[1], day, hour)
    vals = {("viewer", None): None if vs is None else vs["sky"]}
    for (d, a), (fc, i) in gfc.items():
        ps = point_summary(fc, i)
        vals[(d, a)] = None if ps is None else ps["sky"]
    grid = sky_grid(vals)
    viewer_cloud = None if vfc is None or vi is None else cloud_at_altitude(vfc, vi, viewer_m)
    # 雲海(20・50km)
    sea = {}
    for (d, a), (fc, i) in gfc.items():
        if d not in SEA_DISTS_KM:
            continue
        ps = point_summary(fc, i)
        if ps is None:
            sea[(d, a)] = None
            continue
        sea[(d, a)] = {"decks": sea_levels(ps["levels"], viewer_m, ps["ground_m"], start_pct),
                       "ground_m": ps["ground_m"], "levels": ps["levels"]}
    rows = []
    for mt in mountains:
        fc = get_fc(mt["lat"], mt["lon"], day, [hour]) if mt.get("lat") is not None else None
        i = idx_of(fc, hour)
        ps = point_summary(fc, i)
        summit = None if ps is None else summit_cloud(fc, i, mt["elev_m"])
        gaz = nearest_grid_az(mt["az"])
        path_pts = [(d, gfc[(d, gaz)][0], gfc[(d, gaz)][1]) for d in GRID_DISTS_KM if (d, gaz) in gfc]
        pc = path_cloud(viewer_m, mt["elev_m"], mt["dist_km"], path_pts)
        # 雲海の上面: 山にいちばん近い方位・距離(20/50km)の格子の点
        dnear = min(SEA_DISTS_KM, key=lambda d: abs(d - mt["dist_km"]))
        sp = sea.get((dnear, gaz))
        top = None if not sp else sea_top(sp["decks"])
        top_all = None if not sp else sea_top(sp["decks"], include_below_ground=True)
        rows.append(dict(mt, model=None if ps is None else ps["model"], summit_cloud=summit,
                         total=None if ps is None else ps["total"], low=None if ps is None else ps["low"],
                         mid=None if ps is None else ps["mid"], high=None if ps is None else ps["high"],
                         sky=None if ps is None else ps["sky"], levels=None if ps is None else ps["levels"],
                         path=pc, sea_point=(dnear, gaz), sea_top=top, sea_top_with_underground=top_all,
                         under_sea=under_sea(mt["elev_m"], top), _sea_point_data=sp))
    sea_any = any(sp and any(not dk["below_ground"] for dk in sp["decks"]) for sp in sea.values())
    sea_any_ug = any(sp and sp["decks"] for sp in sea.values())
    return {"hour": hour, "viewer": {"elev_m": viewer_m, "summary": vs, "cloud": viewer_cloud},
            "grid": grid, "grid_fc": gfc, "sea": sea, "sea_any": sea_any, "sea_any_with_underground": sea_any_ug,
            "rows": rows}

def figure_mountains(parts):
    out = []
    for p in parts["peaks"]:
        if p["tier"] not in ("大", "中"):
            continue
        pt = parts["points"].get(p["row_id"])
        out.append({"key": p["row_id"], "label": p["label"], "lat": pt["lat"] if pt else None,
                    "lon": pt["lon"] if pt else None, "elev_m": pt["elev_m"] if pt else None,
                    "az": p["az"] % 360.0, "dist_km": p["dist_km"], "fig_az": p["az"], "draw_angle": p["draw_angle"]})
    return out

NO_FORECAST_COLOR = "#d9d9d9"


# ---- 元: experiments/panorama/stage36_sky.py ----

SEA_BELOW_GAP_M = 300.0     # 「眼下に雲海」: 雲のいちばん上の面が視点よりこれ以上低い(仮)

TEXT_IN = "視点が雲の中の見込み"

TEXT_NEAR = "視点は雲海の上面の近く(雲の上に出ているか、雲の中かは、予報の高さの刻みでは分かりません)"

TEXT_BELOW = "眼下に雲海の見込み"

def above_ground(levels: list, ground_m) -> list:
    return [lv for lv in levels or [] if ground_m is None or lv[0] >= float(ground_m)]

def sky_value_above(levels: list, mid, high, viewer_m: float, ground_m=None):
    """視点より上の雲すべての最大。levels: [(高さ, 雲量, hPa, ...)]。値が 1 つも無ければ None。"""
    vals = [float(c) for alt, c, *_ in above_ground(levels, ground_m) if c is not None and alt > viewer_m]
    vals += [float(v) for v in (mid, high) if v is not None]
    return max(vals) if vals else None

def viewer_state(levels: list, viewer_m: float, ground_m, start_pct: float) -> dict:
    """返り値: {"code": "in" / "near_top" / "below" / None, "text", "lower", "upper"(面の (高さ, 雲量, hPa))}。"""
    lv = sorted((float(a), None if c is None else float(c), h) for a, c, h, *_ in above_ground(levels, ground_m))
    lower = [x for x in lv if x[0] <= viewer_m]
    upper = [x for x in lv if x[0] > viewer_m]
    lo = lower[-1] if lower else None
    up = upper[0] if upper else None

    def cl(x):
        return x is not None and x[1] is not None and x[1] >= start_pct
    out = {"lower": lo, "upper": up, "code": None, "text": ""}
    if cl(lo) and cl(up):
        out.update(code="in", text=TEXT_IN)
    elif cl(lo) and up is not None and up[1] is not None and not cl(up):
        out.update(code="near_top", text=TEXT_NEAR)
    else:
        below_cloud = [x for x in lower if cl(x)]
        above_cloud = [x for x in upper if cl(x)]
        if below_cloud and not above_cloud and viewer_m - below_cloud[-1][0] >= SEA_BELOW_GAP_M:
            out.update(code="below", text=TEXT_BELOW)
    return out

def compare_grids(old: dict, new: dict) -> dict:
    """段階3.5 の空の値(old)と新しい値(new)の比べ。区画(段 × 方位。真上は 1 つ)ごと。"""
    changed, same, none, diffs = 0, 0, 0, []
    rows = []
    for r in range(len(new["rows"])):
        n_az = 1 if r == 0 else len(new["azs"])
        ch = 0
        for i in range(n_az):
            a, b = old["rows"][r][i], new["rows"][r][i]
            if a is None or b is None:
                none += 1
                continue
            d = b - a
            if abs(d) > 1e-9:
                changed += 1
                ch += 1
                diffs.append(d)
            else:
                same += 1
        rows.append(ch)
    return {"cells": changed + same + none, "changed": changed, "same": same, "no_value": none,
            "changed_by_row": rows, "mean_increase": round(sum(diffs) / len(diffs), 1) if diffs else 0.0,
            "max_increase": round(max(diffs), 1) if diffs else 0.0,
            "changed_ge_30": sum(1 for d in diffs if d >= 30.0)}


# ---- 元: experiments/panorama/stage36_run.py ----

def _scene_add36(sc, viewer_lat, viewer_lon, viewer_m, day, hour, grid_center, mountains, start_pct=clouds.START_PCT):
    vs = sc["viewer"]["summary"]
    # 空の値と視点の状態は、応答の elevation(地点の標高。モデルの地面ではない)で面を外さない(指示書 5.1・5.3 のとおり)
    vals = {("viewer", None): None if vs is None else sky_value_above(vs["levels"], vs["mid"], vs["high"], viewer_m)}
    pts = {}
    for (d, a), (fc, i) in sc["grid_fc"].items():
        ps = point_summary(fc, i)
        vals[(d, a)] = None if ps is None else sky_value_above(ps["levels"], ps["mid"], ps["high"], viewer_m)
        pts.setdefault(a, [])
        if ps is not None:
            pts[a].append((ps["levels"], ps["ground_m"]))
    sc["grid_old"] = sc["grid"]
    sc["grid"] = sky_grid(vals)
    sc["sky_compare"] = compare_grids(sc["grid_old"], sc["grid"])
    sc["profile"] = clouds.level_profile(pts, core.LEVEL_STACK_HPA)
    sc["viewer_state"] = (viewer_state(vs["levels"], viewer_m, None, start_pct) if vs is not None
                          else {"code": None, "text": "", "lower": None, "upper": None})
    sc["start_pct"] = start_pct
    sc["bands_all"] = clouds.build_bands(sc["profile"], start_pct)
    covers = clouds.cover_by_az(sc["bands_all"], len(sc["profile"]["azs"]))
    for row in sc["rows"]:
        j = sc["profile"]["azs"].index(nearest_grid_az(row["az"]))
        row["band_cover"] = covers[j]
        row["hidden_by_band"] = (row["elev_m"] is not None and clouds.hidden_by_band(covers[j], row["elev_m"], viewer_m))
    sc["viewer_m"] = viewer_m
    return sc

def alt_at_angle(obs_m, angle_deg, d_m):
    """c2.angle_deg の逆(その距離でその見上げ角になる高さ)。"""
    import mountain_terrain as mt
    drop = (1 - mt.REFRACTION_K) * d_m * d_m / (2 * mt.EARTH_RADIUS_M)
    return obs_m + drop + d_m * math.tan(math.radians(angle_deg))

def depth_index(dist_m, bands=terrain.DEFAULT_BANDS):
    """その距離の雲を、どの層の多角形の直前に入れるか: 層の端が dist_m より近い層のうち、いちばん遠い層。
    無ければ -1(すべての層の後 = 地形のいちばん手前の上)。"""
    ok = [b for b, e in enumerate(bands) if e < dist_m]
    return ok[-1] if ok else -1

POLY_RE = re.compile(r'<path d="[^"]*" class="(near|B\d+)"/>')   # 地形の帯(draw.render_v2 の path)


# ---- 元: experiments/panorama/stage37_run.py ----

DEFAULT_HOURS = (6, 8, 10, 12)          # 4.1 時刻の並び(設定で変えられる)

DEFAULT_HOUR = 8

TERRAIN = "terrain"

TERRAIN_SKY = 0.0                       # 「地形だけ」の空 = 雲量 0 の色


def state_of(hour_str):
    return "h" + hour_str[-5:-3]

def _scene_add37(sc, viewer_lat, viewer_lon, viewer_m, day, hour, grid_center, mountains, start_pct=clouds.START_PCT):
    vs = sc["viewer"]["summary"]
    # 3.5 空の分からない範囲
    sc["grid36"] = sc["grid"]
    vals = {("viewer", None): None if vs is None else clouds.sky_with_unknown(
        sky_value_above(vs["levels"], vs["mid"], vs["high"], viewer_m), vs["levels"], viewer_m, start_pct)}
    unknown_cells = 1 if vs is not None and clouds.unknown_add(vs["levels"], viewer_m, start_pct) > 0 else 0
    by_d = {d: {} for d in clouds.GRID_DISTS_KM}
    for (d, a), (fc, i) in sc["grid_fc"].items():
        ps = point_summary(fc, i)
        if ps is None:
            vals[(d, a)] = None
            by_d[d][a] = []
            continue
        base = sky_value_above(ps["levels"], ps["mid"], ps["high"], viewer_m)
        vals[(d, a)] = clouds.sky_with_unknown(base, ps["levels"], viewer_m, start_pct)
        unknown_cells += clouds.unknown_add(ps["levels"], viewer_m, start_pct) > 0
        by_d[d][a] = [(ps["levels"], ps["ground_m"])]
    sc["grid"] = sky_grid(vals)
    sc["sky_unknown_cells"] = unknown_cells
    sc["sky_compare37"] = compare_grids(sc["grid36"], sc["grid"])
    # 3.1 距離ごとの雲の面(視点 = 0km。視点の点は地面で面を外さない)
    azs = sc["profile"]["azs"]
    profs = {0.0: clouds.level_profile({a: [(vs["levels"] if vs else [], None)] for a in azs}, core.LEVEL_STACK_HPA)}
    for d in clouds.GRID_DISTS_KM:
        profs[d] = clouds.level_profile({a: by_d[d].get(a, []) for a in azs}, core.LEVEL_STACK_HPA)
    sea = []
    for bi, D in enumerate(clouds.SEA_BOUNDARIES_M):
        prof = clouds.interp_profile(profs, D / 1000.0)
        sea.append({"bi": bi, "D_m": D, "runs": clouds.sea_runs(prof, viewer_m, start_pct)})
    sc["sea_bands"] = sea
    sc["upper_bands_all"] = clouds.build_bands_filtered(sc["profile"], start_pct, None, clouds.upper_segments_filter(viewer_m))
    # 3.4 名前のうすさ・山ごとの雲の「包む」範囲
    upper_cover = clouds.cover_by_az(sc["upper_bands_all"], len(azs))
    for row in sc["rows"]:
        j = azs.index(nearest_grid_az(row["az"]))
        d_m = row["dist_km"] * 1000.0
        hits, sea_cover = [], []
        for sb in sea:
            if sb["D_m"] >= d_m:
                continue
            col = next((c for run in sb["runs"] for c in run["cols"] if c["j"] == j), None)
            if col is None:
                continue
            sea_cover.append((-1e9, col["top"], col["thin_top"] if col["thin_top"] is not None else col["top"]))
            if row["elev_m"] is not None and terrain.angle_deg(viewer_m, col["top"], sb["D_m"]) > \
                    terrain.angle_deg(viewer_m, row["elev_m"], d_m):
                hits.append(col["c"])
        row["sea_hits"] = [r0(h) for h in hits]
        row["hide_amount"] = clouds.hide_amount(row["summit_cloud"], hits)
        row["name_opacity"] = clouds.name_opacity(row["hide_amount"])
        row["upper_cover"] = upper_cover[j]
        row["sea_cover"] = sea_cover
    return sc

SUN_RE = re.compile(r"ecmwf_ifs025_(-?\d+\.\d+)_(-?\d+\.\d+)_\d+d\.json$")

def sunrise_from_main_cache(lat, lon, day, max_km=40.0):
    """本体の cache/ の ECMWF の応答(daily.sunrise を含む)のうち、いちばん近い地点のその日の日の出。無ければ None。"""
    best = None
    for p in glob.glob(os.path.join(terrain.ROOT, "cache", "ecmwf_ifs025_*_*d.json")):
        m = SUN_RE.search(os.path.basename(p))
        if not m:
            continue
        la, lo = float(m.group(1)), float(m.group(2))
        dkm = geometry.vincenty_inverse(lat, lon, la, lo)[0] / 1000.0
        if dkm > max_km or (best and dkm >= best[0]):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh).get("daily") or {}
        except (OSError, ValueError):
            continue
        times, sr = d.get("time") or [], d.get("sunrise") or []
        if day in times and times.index(day) < len(sr) and sr[times.index(day)]:
            best = (dkm, sr[times.index(day)])
    return None if best is None else best[1]


# ---- 元: experiments/panorama/stage37_report.py ----

def hour_label(h):
    return f"{int(h[-5:-3])} 時"

def make_states(day, hours):
    """[(状態, ボタンの文字, 時刻)]。先頭は「地形だけ」。時刻は hours(6・8・10・12)。"""
    hs = sorted({f"{day}T{h:02d}:00" for h in hours})
    return [(TERRAIN, "地形だけ", None)] + [(state_of(h), hour_label(h), h) for h in hs]


# ---- 元: experiments/panorama/stage38_run.py ----




def _scene_add38(sc, viewer_lat, viewer_lon, viewer_m, day, hour, grid_center, mountains, start_pct=clouds.START_PCT):
    vs = sc["viewer"]["summary"]
    azs = sc["profile"]["azs"]
    by_d = {d: {} for d in clouds.GRID_DISTS_KM}
    for (d, a), (fc, i) in sc["grid_fc"].items():
        ps = point_summary(fc, i)
        by_d[d][a] = [] if ps is None else [(ps["levels"], ps["ground_m"])]
    profs = {0.0: clouds.level_profile({a: [(vs["levels"] if vs else [], None)] for a in azs}, core.LEVEL_STACK_HPA)}
    for d in clouds.GRID_DISTS_KM:
        profs[d] = clouds.level_profile({a: by_d[d].get(a, []) for a in azs}, core.LEVEL_STACK_HPA)
    for sb in sc["sea_bands"]:
        prof = clouds.interp_profile(profs, sb["D_m"] / 1000.0)
        clouds.attach_neighbors_sea(sb["runs"], prof, viewer_m)
    return sc

def compute_scene(viewer_lat, viewer_lon, viewer_m, day, hour, grid_center, mountains, start_pct=clouds.START_PCT):
    """1 つの視点・1 時刻の場面(段階3.5 の基本の値に、3.6 の雲の面・視点の状態、3.7 の空の分からない範囲・雲海の帯・
    名前のうすさ、3.8 の帯の外の隣の方位の雲量を、この順に足す)。"""
    args = (viewer_lat, viewer_lon, viewer_m, day, hour, grid_center, mountains, start_pct)
    sc = _scene_base(*args)
    for add in (_scene_add36, _scene_add37, _scene_add38):
        sc = add(sc, *args)
    return sc


def scene_set(lat, lon, day, states, viewer_m, center, mountains):
    out = {}
    for st, _, h in states:
        if h is None:
            continue
        sc = compute_scene(lat, lon, viewer_m, day, h, center, mountains)
        out[st] = sc if sc["viewer"]["summary"] is not None else None
    return out


# ---------------------------------------------------------------------------
# 予報の取得(診断モードの --panorama 用に手で書いたもの)
# - 視点ごとの方位 × 距離の点(grid_points)・視点・名前の出る山の点を、1 点ずつ 1 日分取る(段階3.7 までの取り方)。
# - 取得は本体の get_with_retry(429 の扱い・通信の間隔の調整もそのまま)。保存は config.weather_dir()。
# - 診断する山の点(1km 以内)は、診断モードが取った予報(本体の cache/。新しいものだけ)を読めるなら読む(本体の関数は変えない)。
# - どこかで取れなければ WeatherUnavailable(呼ぶ側は「地形だけ」の図にする)。
# ---------------------------------------------------------------------------
def point_key(lat, lon):
    return f"{lat:.4f}_{lon:.4f}"


HOURLY_PANO = CLOUD_VARS + core.level_stack_vars(kinds=["cloudcover"])    # 雲に要る 18 変数(視程は使わないので取らない)
MAIN_REUSE_KM = 1.0


class WeatherUnavailable(Exception):
    """予報が取れない(通信の失敗・利用上限・回数の上限)。"""


class FetchContext:
    def __init__(self, allow_fetch=True, max_calls=None, ttl_sec=None, main_point=None, main_days=None):
        self.allow_fetch = allow_fetch
        self.max_calls = max_calls
        self.ttl_sec = core.CACHE_TTL_SECONDS if ttl_sec is None else ttl_sec
        self.main_point = main_point       # (lat, lon): 診断した山の MOUNTAINS の座標
        self.main_days = main_days         # 診断モードの予報日数(本体のキャッシュの名前)
        self.calls = 0
        self.weight = 0.0
        self.from_cache = 0
        self.from_main = 0


CTX = FetchContext(allow_fetch=False)


def pano_cache_path(model, lat, lon, day):
    return os.path.join(config.weather_dir(), f"{model}_{point_key(lat, lon)}_d{day}.json")


def pano_params(lat, lon, model, day):
    return {"latitude": lat, "longitude": lon, "start_date": day, "end_date": day, "timezone": "Asia/Tokyo",
            "hourly": list(HOURLY_PANO), "models": model}


def _fresh(data, ttl_sec):
    if ttl_sec is None or ttl_sec <= 0:
        return True
    t = data.get("_fetched_epoch")
    return t is not None and time.time() - float(t) < ttl_sec


def _main_cache_day(lat, lon, model, day):
    """診断した山の点なら、本体の cache/ の応答(診断モードが取ったもの)からその日の分を切り出す。無ければ None。"""
    c = CTX
    if c.main_point is None or c.main_days is None:
        return None
    d, _ = geometry.vincenty_inverse(lat, lon, c.main_point[0], c.main_point[1])
    if d > MAIN_REUSE_KM * 1000.0:
        return None
    data = core._load_cache(core._cache_path(c.main_point[0], c.main_point[1], model, c.main_days))
    if not data or time.time() - data.get("_fetched_at", 0) >= core.CACHE_TTL_SECONDS:
        return None
    h = data.get("hourly") or {}
    idx = [i for i, t in enumerate(h.get("time") or []) if t.startswith(day)]
    if not idx or any(v not in h for v in HOURLY_PANO):
        return None
    hourly = {k: [h[k][i] for i in idx] for k in ["time"] + list(HOURLY_PANO)}
    c.from_main += 1
    return {"latitude": data.get("latitude"), "longitude": data.get("longitude"), "elevation": data.get("elevation"),
            "hourly": hourly}


def fetch_day_pano(lat, lon, model, day):
    """1 地点・1 モデル・1 日。保存があれば読む。無ければ取る(取れなければ WeatherUnavailable)。"""
    c = CTX
    path = pano_cache_path(model, lat, lon, day)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if _fresh(data, c.ttl_sec):
                c.from_cache += 1
                return data
        except (OSError, ValueError):
            pass
    main = _main_cache_day(lat, lon, model, day)
    if main is not None:
        return main
    if not c.allow_fetch:
        raise WeatherUnavailable("予報の保存が無い(通信しない設定)")
    if c.max_calls is not None and c.calls >= c.max_calls:
        raise WeatherUnavailable(f"1 回の展望図の通信の上限 {c.max_calls} 回に達した")
    params = pano_params(lat, lon, model, day)
    before = core.open_meteo_run_stats().requests
    try:
        resp = core.get_with_retry(core.OPEN_METEO_URL, params)
    except core.OpenMeteoRateLimitError as e:
        raise WeatherUnavailable(f"Open-Meteo の利用上限(種類 {e.kind})") from e
    except Exception as e:  # noqa: BLE001
        raise WeatherUnavailable(f"予報の取得に失敗した({type(e).__name__})") from e
    finally:
        n = core.open_meteo_run_stats().requests - before
        c.calls += max(1, n)
        c.weight += core.request_weight(params) * max(1, n)
    if resp.status_code != 200:
        raise WeatherUnavailable(f"予報の取得に失敗した(HTTP {resp.status_code})")
    data = resp.json()
    data["_fetched_epoch"] = time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return data


def get_fc(lat, lon, day, hours, *args, **kwargs):
    """段階3.5 の get_fc と同じ返り値(merge35 の形)。MSM の雲の値が欠ける時刻があれば ECMWF も読む。"""
    msm = fetch_day_pano(lat, lon, "jma_msm", day)
    ec = fetch_day_pano(lat, lon, "ecmwf_ifs025", day) if needs_ecmwf(msm, hours) else None
    if msm is None and ec is None:
        return None
    return merge35(msm, ec)
