"""展望図の部品: 測地(距離・方位・地点)。元: experiments/panorama/geometry.py。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

import math


# ---- 元: experiments/panorama/geometry.py ----

EYE_HEIGHT_M = 1.6

DEFAULT_STEPS = ((2000.0, 50.0), (20000.0, 200.0), (float("inf"), 500.0))

NEAR_SKIP_M = 100.0

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
