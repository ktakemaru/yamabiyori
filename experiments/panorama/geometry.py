"""展望図の実証 段階1: 「何が見えるか」の計算部品(通信しない)。

標高は `elev_fn(lat, lon) -> float | None` として受け取る(None は「取れなかった」)。
テストでは平らな地面や作った山を渡し、実行時は地理院の標高タイルを渡す。

使う既存部品(import するだけ・変えない):
- mountain_terrain.vincenty_direct(始点・方位・距離 → 到達点。WGS84)
- mountain_terrain.apparent_elevation_angle_deg(地球の丸みと屈折を入れた見上げ角。k=REFRACTION_K)
"""
from __future__ import annotations

import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402

from mountain_terrain import (  # noqa: E402
    REFRACTION_K,
    apparent_elevation_angle_deg,
    vincenty_direct,
)

EYE_HEIGHT_M = 1.6

# 途中の地形を取る刻み: (この距離まで, 刻み)。近くは細かく、遠くは粗く。
# 標高タイル z11 の1セルは約76m(この緯度では東西約62m)。2km までは 50m でセルを取りこぼさない。
# 20km までは 200m(見上げ角への影響は 0.1° 未満)、その先は 500m。
DEFAULT_STEPS = ((2000.0, 50.0), (20000.0, 200.0), (float("inf"), 500.0))
# 視点のごく近く(この距離未満)は取らない: タイルの粗さで足もとの斜面が「壁」に見えるのを避ける。
NEAR_SKIP_M = 100.0
# 目印の手前この距離までで止める: 山頂の周りのセルや座標の小さなずれで、山頂自身を「手前の地形」と数えないため。
TARGET_END_SKIP_M = 300.0


# ---------------------------------------------------------------------------
# 測地線(逆問題): 2点間の方位と距離
# ---------------------------------------------------------------------------
def vincenty_inverse(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple:
    """WGS84 楕円体上の2点間の (距離 m, 始点での方位 度[真北から時計回り 0〜360))。"""
    a = 6378137.0
    f = 1 / 298.257223563
    b = (1 - f) * a
    if lat1 == lat2 and lon1 == lon2:
        return 0.0, 0.0
    u1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
    u2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
    big_l = math.radians(lon2 - lon1)
    lam = big_l
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.sqrt((cos_u2 * sin_lam) ** 2 + (cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam) ** 2)
        if sin_sigma == 0:
            return 0.0, 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos_sq_alpha = 1 - sin_alpha ** 2
        cos2_sigma_m = cos_sigma - 2 * sin_u1 * sin_u2 / cos_sq_alpha if cos_sq_alpha else 0.0
        c = f / 16 * cos_sq_alpha * (4 + f * (4 - 3 * cos_sq_alpha))
        lam_prev = lam
        lam = big_l + (1 - c) * f * sin_alpha * (
            sigma + c * sin_sigma * (cos2_sigma_m + c * cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)))
        if abs(lam - lam_prev) < 1e-12:
            break
    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta_sigma = big_b * sin_sigma * (cos2_sigma_m + big_b / 4 * (
        cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)
        - big_b / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos2_sigma_m ** 2)))
    dist = b * big_a * (sigma - delta_sigma)
    az = math.degrees(math.atan2(cos_u2 * math.sin(lam), cos_u1 * sin_u2 - sin_u1 * cos_u2 * math.cos(lam)))
    return dist, az % 360.0


def sample_distances(max_m: float, steps=DEFAULT_STEPS, start_m: float = NEAR_SKIP_M) -> list:
    """start_m から max_m 以下までの標本距離(刻みは steps)。"""
    out = []
    d = start_m
    while d <= max_m:
        out.append(d)
        step = next(s for lim, s in steps if d < lim)
        d += step
    return out


def angle_diff_deg(a: float, b: float) -> float:
    """a − b を −180〜180 に正規化。"""
    return (a - b + 180.0) % 360.0 - 180.0


# ---------------------------------------------------------------------------
# 計算 A: 目印ごとの「見えるか」
# ---------------------------------------------------------------------------
def target_visibility(viewer: dict, target: dict, elev_fn, *, eye_m: float = EYE_HEIGHT_M,
                      k: float = REFRACTION_K, steps=DEFAULT_STEPS,
                      near_skip_m: float = NEAR_SKIP_M, end_skip_m: float = TARGET_END_SKIP_M) -> dict:
    """viewer/target: {"lat","lon","ele_m"}。

    返り値: 方位・距離・山頂の見上げ角・途中の地形の見上げ角の最大とその距離・余裕(度)。
    余裕 = 山頂の見上げ角 − 途中の地形の見上げ角の最大(正なら山頂が手前の地形より上に出る)。
    途中の地形の標高が取れなかった標本点の数も返す(missing)。
    """
    obs_m = viewer["ele_m"] + eye_m
    dist, az = vincenty_inverse(viewer["lat"], viewer["lon"], target["lat"], target["lon"])
    summit_angle = apparent_elevation_angle_deg(obs_m, target["ele_m"], dist, k)
    best_angle, best_d, best_h = -90.0, None, None
    missing = 0
    for d in sample_distances(dist - end_skip_m, steps, near_skip_m):
        lat, lon = vincenty_direct(viewer["lat"], viewer["lon"], az, d)
        h = elev_fn(lat, lon)
        if h is None:
            missing += 1
            continue
        ang = apparent_elevation_angle_deg(obs_m, h, d, k)
        if ang > best_angle:
            best_angle, best_d, best_h = ang, d, h
    margin = summit_angle - best_angle if best_d is not None else None
    return {
        "azimuth_deg": az,
        "distance_km": dist / 1000.0,
        "summit_angle_deg": summit_angle,
        "max_terrain_angle_deg": best_angle if best_d is not None else None,
        "max_terrain_at_km": best_d / 1000.0 if best_d is not None else None,
        "max_terrain_ele_m": best_h,
        "margin_deg": margin,
        "visible": (margin is not None and margin > 0),
        "missing_samples": missing,
    }


# ---------------------------------------------------------------------------
# 計算 B: 稜線の輪郭(スカイライン)
# ---------------------------------------------------------------------------
def skyline(viewer: dict, az_start: float, az_end: float, az_step: float, max_m: float, elev_fn, *,
            eye_m: float = EYE_HEIGHT_M, k: float = REFRACTION_K, steps=DEFAULT_STEPS,
            near_skip_m: float = NEAR_SKIP_M) -> list:
    """方位ごとに、max_m までの地形の見上げ角の最大と、その地形までの距離・標高を返す。"""
    obs_m = viewer["ele_m"] + eye_m
    dists = sample_distances(max_m, steps, near_skip_m)
    n = int(round((az_end - az_start) / az_step))
    out = []
    for i in range(n + 1):
        az = az_start + i * az_step
        best_angle, best_d, best_h, missing = -90.0, None, None, 0
        for d in dists:
            lat, lon = vincenty_direct(viewer["lat"], viewer["lon"], az, d)
            h = elev_fn(lat, lon)
            if h is None:
                missing += 1
                continue
            ang = apparent_elevation_angle_deg(obs_m, h, d, k)
            if ang > best_angle:
                best_angle, best_d, best_h = ang, d, h
        out.append({"azimuth_deg": round(az, 4),
                    "angle_deg": best_angle if best_d is not None else None,
                    "distance_km": best_d / 1000.0 if best_d is not None else None,
                    "ele_m": best_h, "missing_samples": missing})
    return out


# ---------------------------------------------------------------------------
# 横位置の当てはめ: x = cx + f × tan(方位 − 中心の方位)
# ---------------------------------------------------------------------------
def project_x(az_deg, cx: float, f: float, az0: float):
    d = np.radians(np.vectorize(angle_diff_deg)(np.asarray(az_deg, dtype=float), az0))
    return cx + f * np.tan(d)


def fit_camera(az_deg, x_px, *, cx_fixed: float | None = None, iters: int = 100) -> dict:
    """方位と横位置の組から cx・f・中心の方位を最小二乗(Gauss-Newton)で求める。

    cx_fixed を与えると cx を固定して f・中心の方位だけを求める。
    横の視野角 = 2 × atan(画像幅/2 / f) は呼ぶ側で出す(画像幅が要るため)。
    """
    az = np.asarray(az_deg, dtype=float)
    x = np.asarray(x_px, dtype=float)
    # 初期値: 直線の当てはめから
    ref = az[0]
    rel = np.array([angle_diff_deg(a, ref) for a in az])
    slope, icpt = np.polyfit(rel, x, 1)
    cx = 800.0 if cx_fixed is None else cx_fixed
    az0 = ref + (cx - icpt) / slope
    f = slope * 180.0 / math.pi
    for _ in range(iters):
        d = np.radians([angle_diff_deg(a, az0) for a in az])
        t = np.tan(d)
        pred = cx + f * t
        r = x - pred
        sec2 = 1.0 / np.cos(d) ** 2
        cols = [t, -f * sec2 * math.pi / 180.0]       # d/d f, d/d az0
        if cx_fixed is None:
            cols.append(np.ones_like(t))               # d/d cx
        jac = np.column_stack(cols)
        step, *_ = np.linalg.lstsq(jac, r, rcond=None)
        f += step[0]
        az0 += step[1]
        if cx_fixed is None:
            cx += step[2]
        if np.max(np.abs(step)) < 1e-10:
            break
    d = np.radians([angle_diff_deg(a, az0) for a in az])
    pred = cx + f * np.tan(d)
    res = x - pred
    # 残差を角度に直す: x の残差 / (f × sec²)
    res_deg = np.degrees(res / (f / np.cos(d) ** 2))
    return {"model": "tan" if cx_fixed is None else "tan_cx_fixed", "cx": float(cx), "f": float(f),
            "az0_deg": float(az0 % 360.0), "residual_px": res.tolist(), "residual_deg": res_deg.tolist(),
            "rms_px": float(np.sqrt(np.mean(res ** 2)))}


def fit_linear(az_deg, x_px) -> dict:
    """x = a + b × (方位 − 基準)。返り値の az_at(x) で横位置から方位を戻せる。"""
    az = np.asarray(az_deg, dtype=float)
    x = np.asarray(x_px, dtype=float)
    ref = float(az[0])
    rel = np.array([angle_diff_deg(a, ref) for a in az])
    b, a = np.polyfit(rel, x, 1)
    res = x - (a + b * rel)
    return {"model": "linear", "a": float(a), "b_px_per_deg": float(b), "ref_az_deg": ref,
            "residual_px": res.tolist(), "residual_deg": (res / b).tolist(),
            "rms_px": float(np.sqrt(np.mean(res ** 2)))}


def horizontal_fov_deg(f: float, width_px: float) -> float:
    return math.degrees(2 * math.atan(width_px / 2.0 / abs(f)))


def order_mismatches(items: list) -> list:
    """items: [(名前, x_px, 方位)]。x_px の順に並べ、方位が逆転している隣り合う組を返す。"""
    s = sorted(items, key=lambda t: t[1])
    out = []
    for a, b in zip(s, s[1:]):
        if angle_diff_deg(b[2], a[2]) < 0:
            out.append((a[0], b[0], a[2], b[2]))
    return out
