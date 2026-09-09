"""
Mountain Weather Dashboard - shared core
------------------------------------------
The pieces of mountain_weather_mvp.py (探索モード) and
mountain_weather_detail.py (診断モード) that were, before 2026-09, hand-copied
verbatim into both files: the MOUNTAINS pool, the raw-JSON Open-Meteo cache
layer, the climbing-score formula (mountain_climb_score + its six penalty_*
helpers + wet_chill_adjustment_c + SCORE_WEIGHTS), the pressure-level/fixed-
altitude-band plumbing, and a handful of small formatting/date helpers used
identically by both.

This module is now the single source of truth for all of that. Both mvp.py
and detail.py `from mountain_weather_core import (...)` these names instead
of redefining them, so a scoring or cache-layer change made here
automatically applies to both scripts -- the "keep both copies in sync by
hand" maintenance burden this split previously required (see SKILL.md's old
known-pitfall entries) no longer applies to anything defined here.

Deliberately NOT moved here (stayed in each file, because they differ in
real, non-cosmetic ways between the two scripts):
    - fetch_single_model / fetch_forecast: different HOURLY_VARS/extra-var
      sets and different merge shapes (mvp.py fetches a third best_match
      call for visibility only; detail.py fetches freezing level AND
      visibility separately, and also takes past_days/extra_vars).
    - window_scores_by_day (mvp.py) / compute_day_scores (detail.py):
      mvp.py aggregates over 75 mountains' fixed 1000/2000/3000m bands;
      detail.py aggregates over one mountain's own nearest pressure level.
      Different inputs, different dict shapes.
    - fmt(): mvp.py's fmt(val, ndigits=0) renders "1234.0" (round(val, 0),
      a float); detail.py's renders "1234" (round(val), an int). This is a
      genuine pre-existing inconsistency between the two scripts' output,
      not an oversight of this refactor -- unifying it would change either
      script's on-screen output, so both copies were left exactly as they
      were.
    - wind_peak_warning_cell(), precip_timing_note(): both call fmt() (see
      above) and/or take differently-shaped arguments (mvp.py's
      precip_timing_note(day_index, rows) vs detail.py's
      precip_timing_note(day_scores)), so left as separate copies too.
    - ALTITUDE_BAND_ACTUAL_M / band_altitude_label / HUMIDITY_VARS /
      WIND_DIR_BAND_VARS: detail.py-only (display/diagnosis extras mvp.py's
      75-mountain ranking table doesn't use).
    - TEMP_BAND_VARS: mvp.py-only (detail.py derives temperature from the
      selected mountain's own nearest pressure level instead of a fixed
      band -- see temp_var_for_elevation() in that file).
    - Everything GPX/route-map/ensemble-confidence/cloud-sea/JMA-map-related
      is detail.py-only and untouched by this refactor.

Not meant to be run directly.
"""

import io
import json
import math
import os
import time as time_module
import unicodedata
from datetime import date

import requests

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def display_width(s: str) -> int:
    """Return the terminal display width of s, counting full-width
    (Japanese etc.) characters as 2 columns and everything else as 1."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, width: int) -> str:
    """Left-align s within `width` display columns, accounting for
    full-width characters (unlike str.ljust, which counts by character)."""
    return s + " " * max(0, width - display_width(s))


WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def format_date_with_weekday(d: str) -> str:
    y, m, day = (int(x) for x in d.split("-"))
    weekday = WEEKDAY_JA[date(y, m, day).weekday()]
    return f"{d}({weekday})"


# ---------------------------------------------------------------------------
# Candidate mountain pool (75座、関東甲信中心 + 伊豆/北陸信越/東北南部)
# ---------------------------------------------------------------------------
MOUNTAINS = [
    # -- 関東甲信 --
    # access: how you reach the high elevation trailhead/summit area
    #   "ロープウェイ/ゴンドラ" = ropeway or gondola goes most of the way up
    #   "バス/車"          = paved road or bus reaches a high trailhead
    #   "登山"             = no mechanized assist, hiking from a lower trailhead
    {"name": "谷川岳",       "lat": 36.8025, "lon": 138.9319, "elevation_m": 1977, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "八ヶ岳(赤岳)",  "lat": 35.9722, "lon": 138.3672, "elevation_m": 2899, "access": "登山", "region": "関東甲信"},
    {"name": "横岳",         "lat": 35.9858, "lon": 138.3728, "elevation_m": 2830, "access": "登山", "region": "関東甲信"},
    {"name": "硫黄岳",       "lat": 35.9986, "lon": 138.3697, "elevation_m": 2760, "access": "登山", "region": "関東甲信"},
    {"name": "東天狗岳",      "lat": 36.0198, "lon": 138.3595, "elevation_m": 2640, "access": "登山", "region": "関東甲信"},
    {"name": "権現岳",       "lat": 35.9497, "lon": 138.3596, "elevation_m": 2715, "access": "登山", "region": "関東甲信"},
    {"name": "編笠山",       "lat": 35.9417, "lon": 138.3450, "elevation_m": 2524, "access": "登山", "region": "関東甲信"},
    {"name": "木曽駒ヶ岳",    "lat": 35.7897, "lon": 137.8083, "elevation_m": 2956, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "宝剣岳",       "lat": 35.7814, "lon": 137.8092, "elevation_m": 2931, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "三ノ沢岳",      "lat": 35.7667, "lon": 137.7939, "elevation_m": 2847, "access": "登山", "region": "関東甲信"},
    {"name": "空木岳",       "lat": 35.7189, "lon": 137.8172, "elevation_m": 2864, "access": "登山", "region": "関東甲信"},
    {"name": "富士山(剣ヶ峰)", "lat": 35.3606, "lon": 138.7274, "elevation_m": 3776, "access": "バス/車", "region": "関東甲信"},
    {"name": "天上山(カチカチ山)", "lat": 35.5033, "lon": 138.7806, "elevation_m": 1140, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "三ツ峠山(開運山)", "lat": 35.5492, "lon": 138.8092, "elevation_m": 1785, "access": "登山", "region": "関東甲信"},
    {"name": "黒岳(御坂山地)", "lat": 35.5522, "lon": 138.7494, "elevation_m": 1793, "access": "登山", "region": "関東甲信"},
    {"name": "石割山",       "lat": 35.4520, "lon": 138.9014, "elevation_m": 1413, "access": "登山", "region": "関東甲信"},
    {"name": "鉄砲木ノ頭(明神山)", "lat": 35.4111, "lon": 138.9178, "elevation_m": 1291, "access": "登山", "region": "関東甲信"},
    {"name": "杓子山",       "lat": 35.4853, "lon": 138.8661, "elevation_m": 1598, "access": "登山", "region": "関東甲信"},
    {"name": "竜ヶ岳(本栖湖)", "lat": 35.4468, "lon": 138.5834, "elevation_m": 1485, "access": "登山", "region": "関東甲信"},
    {"name": "毛無山(天子山地)", "lat": 35.4158, "lon": 138.5439, "elevation_m": 1964, "access": "登山", "region": "関東甲信"},
    {"name": "越前岳",       "lat": 35.2381, "lon": 138.7940, "elevation_m": 1504, "access": "登山", "region": "関東甲信"},
    {"name": "塔ノ岳",       "lat": 35.4508, "lon": 139.1547, "elevation_m": 1491, "access": "登山", "region": "関東甲信"},
    {"name": "丹沢山",       "lat": 35.4743, "lon": 139.1627, "elevation_m": 1567, "access": "登山", "region": "関東甲信"},
    {"name": "蛭ヶ岳",       "lat": 35.4863, "lon": 139.1389, "elevation_m": 1673, "access": "登山", "region": "関東甲信"},
    {"name": "檜洞丸",       "lat": 35.4789, "lon": 139.1028, "elevation_m": 1601, "access": "登山", "region": "関東甲信"},
    {"name": "鍋割山",       "lat": 35.4439, "lon": 139.1414, "elevation_m": 1273, "access": "登山", "region": "関東甲信"},
    {"name": "大山(伊勢原)",  "lat": 35.4408, "lon": 139.2311, "elevation_m": 1252, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "雲取山",       "lat": 35.8547, "lon": 138.9364, "elevation_m": 2017, "access": "登山", "region": "関東甲信"},
    {"name": "高尾山",       "lat": 35.6253, "lon": 139.2436, "elevation_m": 599, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "陣馬山",       "lat": 35.6522, "lon": 139.1667, "elevation_m": 855, "access": "登山", "region": "関東甲信"},
    {"name": "御岳山",       "lat": 35.7829, "lon": 139.1495, "elevation_m": 929, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "棒ノ折山(棒ノ嶺)", "lat": 35.85861, "lon": 139.15500, "elevation_m": 969, "access": "登山", "region": "関東甲信"},
    {"name": "笠取山",       "lat": 35.8653, "lon": 138.8197, "elevation_m": 1953, "access": "登山", "region": "関東甲信"},
    {"name": "燧ヶ岳",       "lat": 36.9758, "lon": 139.2864, "elevation_m": 2356, "access": "登山", "region": "関東甲信"},
    {"name": "至仏山",       "lat": 36.9269, "lon": 139.1875, "elevation_m": 2228, "access": "登山", "region": "関東甲信"},
    {"name": "燕岳",         "lat": 36.3931, "lon": 137.7325, "elevation_m": 2763, "access": "登山", "region": "関東甲信"},
    {"name": "槍ヶ岳",       "lat": 36.3419, "lon": 137.6469, "elevation_m": 3180, "access": "登山", "region": "関東甲信"},
    {"name": "常念岳",       "lat": 36.3308, "lon": 137.7267, "elevation_m": 2857, "access": "登山", "region": "関東甲信"},
    {"name": "唐松岳",       "lat": 36.7514, "lon": 137.7622, "elevation_m": 2696, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "西穂高岳",      "lat": 36.2564, "lon": 137.6494, "elevation_m": 2909, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "乗鞍岳(剣ヶ峰)", "lat": 36.1061, "lon": 137.5508, "elevation_m": 3026, "access": "バス/車", "region": "関東甲信"},
    {"name": "美ヶ原(王ヶ頭)", "lat": 36.1364, "lon": 138.1119, "elevation_m": 2034, "access": "バス/車", "region": "関東甲信"},
    {"name": "霧ヶ峰(車山)",  "lat": 36.1094, "lon": 138.1900, "elevation_m": 1925, "access": "バス/車", "region": "関東甲信"},
    {"name": "北横岳",       "lat": 36.0119, "lon": 138.3222, "elevation_m": 2480, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "甲斐駒ヶ岳",    "lat": 35.7581, "lon": 138.2364, "elevation_m": 2967, "access": "登山", "region": "関東甲信"},
    {"name": "北岳",         "lat": 35.6742, "lon": 138.2392, "elevation_m": 3193, "access": "登山", "region": "関東甲信"},
    {"name": "仙丈ヶ岳",      "lat": 35.7050, "lon": 138.1936, "elevation_m": 3033, "access": "登山", "region": "関東甲信"},
    {"name": "入笠山",       "lat": 35.8964, "lon": 138.1717, "elevation_m": 1955, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "蓼科山",       "lat": 36.1214, "lon": 138.3167, "elevation_m": 2531, "access": "登山", "region": "関東甲信"},
    {"name": "金峰山",       "lat": 35.8639, "lon": 138.6467, "elevation_m": 2599, "access": "登山", "region": "関東甲信"},
    {"name": "瑞牆山",       "lat": 35.8797, "lon": 138.5947, "elevation_m": 2230, "access": "登山", "region": "関東甲信"},
    {"name": "赤城山(黒檜山)", "lat": 36.5539, "lon": 139.1917, "elevation_m": 1828, "access": "登山", "region": "関東甲信"},
    {"name": "榛名山(掃部ヶ岳)", "lat": 36.4711, "lon": 138.8628, "elevation_m": 1449, "access": "登山", "region": "関東甲信"},
    {"name": "妙義山(相馬岳)", "lat": 36.2986, "lon": 138.7489, "elevation_m": 1104, "access": "登山", "region": "関東甲信"},
    {"name": "木曽御嶽山",    "lat": 35.8928, "lon": 137.4808, "elevation_m": 3067, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "日光白根山",    "lat": 36.7983, "lon": 139.3742, "elevation_m": 2578, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "筑波山(女体山)", "lat": 36.2253, "lon": 140.1069, "elevation_m": 877, "access": "ロープウェイ/ゴンドラ", "region": "関東甲信"},
    {"name": "大菩薩嶺",      "lat": 35.7486, "lon": 138.8453, "elevation_m": 2057, "access": "バス/車", "region": "関東甲信"},
    {"name": "浅間山(前掛山)", "lat": 36.4038, "lon": 138.5133, "elevation_m": 2524, "access": "登山", "region": "関東甲信"},
    {"name": "男体山",       "lat": 36.7619, "lon": 139.4939, "elevation_m": 2486, "access": "登山", "region": "関東甲信"},
    {"name": "両神山",       "lat": 36.0233, "lon": 138.8414, "elevation_m": 1723, "access": "登山", "region": "関東甲信"},
    {"name": "甲武信ヶ岳",    "lat": 35.9089, "lon": 138.7289, "elevation_m": 2475, "access": "登山", "region": "関東甲信"},
    {"name": "乾徳山",       "lat": 35.8227, "lon": 138.7149, "elevation_m": 2031, "access": "登山", "region": "関東甲信"},
    # -- 伊豆 --
    {"name": "天城山(万三郎岳)", "lat": 34.8628, "lon": 139.0017, "elevation_m": 1406, "access": "登山", "region": "伊豆"},
    # -- 北陸・信越 --
    {"name": "立山(雄山)",    "lat": 36.5758, "lon": 137.6197, "elevation_m": 3003, "access": "バス/車", "region": "北陸信越"},
    {"name": "白馬岳",       "lat": 36.7583, "lon": 137.7597, "elevation_m": 2932, "access": "登山", "region": "北陸信越"},
    {"name": "白山(御前峰)",  "lat": 36.1547, "lon": 136.7717, "elevation_m": 2702, "access": "登山", "region": "北陸信越"},
    {"name": "妙高山",       "lat": 36.8886, "lon": 138.1147, "elevation_m": 2454, "access": "登山", "region": "北陸信越"},
    {"name": "火打山",       "lat": 36.9106, "lon": 138.0678, "elevation_m": 2462, "access": "登山", "region": "北陸信越"},
    {"name": "苗場山",       "lat": 36.8458, "lon": 138.6903, "elevation_m": 2145, "access": "登山", "region": "北陸信越"},
    # -- 東北南部 --
    {"name": "那須岳(茶臼岳)", "lat": 37.1225, "lon": 139.9631, "elevation_m": 1915, "access": "ロープウェイ/ゴンドラ", "region": "東北南部"},
    {"name": "磐梯山",       "lat": 37.6008, "lon": 140.0722, "elevation_m": 1816, "access": "登山", "region": "東北南部"},
    {"name": "安達太良山",    "lat": 37.6256, "lon": 140.2864, "elevation_m": 1700, "access": "ロープウェイ/ゴンドラ", "region": "東北南部"},
    {"name": "西吾妻山",      "lat": 37.7256, "lon": 140.1697, "elevation_m": 2035, "access": "ロープウェイ/ゴンドラ", "region": "東北南部"},
    {"name": "蔵王山(熊野岳)", "lat": 38.1447, "lon": 140.4406, "elevation_m": 1841, "access": "ロープウェイ/ゴンドラ", "region": "東北南部"},
    {"name": "会津駒ヶ岳",    "lat": 37.0476, "lon": 139.3538, "elevation_m": 2133, "access": "登山", "region": "東北南部"},
]

# ---------------------------------------------------------------------------
# Pressure levels: Open-Meteo reports cloud cover/wind/temperature at fixed
# atmospheric pressure levels rather than fixed altitudes above ground. The
# mapping below is the standard-atmosphere approximation, used to find the
# pressure level closest to a given elevation (either one of the 3 fixed
# 1000/2000/3000m bands mvp.py compares every mountain on, or detail.py's
# selected mountain's own real elevation).
# ---------------------------------------------------------------------------
PRESSURE_LEVELS_HPA = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50]
# Levels ecmwf_ifs025 serves (900/800hPa etc. come back null there -- see
# LEVEL_STACK_HPA's comment). Today this list only feeds
# nearest_pressure_level(), which detail.py's ECMWF *ensemble* confidence
# call still needs (that API takes a real level name). Scoring and display
# no longer snap to a level at all -- see the altitude interpolation layer
# below (2026-09-09).


def pressure_level_altitude_m(hpa: int) -> float:
    """Standard-atmosphere altitude (m) for a given pressure level (hPa)."""
    return 44330 * (1 - (hpa / 1013.25) ** (1 / 5.255))


def nearest_pressure_level(elevation_m: float) -> int:
    """Return the pressure level (hPa) whose standard-atmosphere altitude is
    closest to the given elevation. Only used where a REAL level name is
    unavoidable (the ensemble API); everything else interpolates."""
    return min(PRESSURE_LEVELS_HPA, key=lambda hpa: abs(pressure_level_altitude_m(hpa) - elevation_m))


# ---------------------------------------------------------------------------
# Altitude interpolation layer (2026-09-09).
#
# Before: every per-altitude number (cloud/RH/wind/temp) came from ONE fixed
# pressure level chosen by standard atmosphere -- 925/850/700hPa for the
# nominal 1000/2000/3000m bands, nearest level for a summit. Two problems:
#   * the "2000m band" was really 850hPa ~= 1460m, and 25 of the 76
#     mountains' summits sat 500m+ from their assigned level (男体山 2486m
#     was read at ~1460m);
#   * a climber thinks in 1000/2000/3000m (and Fuji's 3776m), not hPa.
# Live-verified 2026-09-09: jma_msm serves 1000/925/900/850/800/700/600hPa
# in full, ecmwf_ifs025 serves 1000/925/850/700/600 (900/800 all-null), and
# BOTH serve geopotential_height_XXXhPa -- the actual height of each surface
# that hour (唐松岳 9/9 12時: 925=755m, 900=990m, 850=1471m, 800=1981m,
# 700=3109m, 600=4384m). So each hour we build the (height, value) profile
# from whatever levels have data and interpolate linearly in height to the
# altitude we actually want: exact 1000/2000/3000m bands, and each
# mountain's own summit. Inside MSM's range 900/800hPa make 1000/2000m
# near-exact; beyond it the 850<->700 span is interpolated -- coarser, but
# strictly better than snapping to 1460m. Levels that are null (ECMWF's
# 900/800, or any hour a variable is missing) simply drop out of the
# profile -- no None ever reaches the safe_avg pipeline as a fake 0.0,
# which is the failure the older "don't add 900/800hPa" warning was about.
# ---------------------------------------------------------------------------
LEVEL_STACK_HPA = [1000, 925, 900, 850, 800, 700, 600]
GPH_KIND = "geopotential_height"
LEVEL_KINDS = ["cloudcover", "relative_humidity", "windspeed", "winddirection", "temperature", GPH_KIND]


def level_var(kind: str, hpa: int) -> str:
    return f"{kind}_{hpa}hPa"


def level_stack_vars(kinds=None, levels=None) -> list:
    """Every Open-Meteo hourly variable the interpolation layer needs:
    kinds x levels, geopotential_height always included (it's the x-axis)."""
    kinds = list(kinds or LEVEL_KINDS)
    if GPH_KIND not in kinds:
        kinds.append(GPH_KIND)
    return [level_var(k, h) for h in (levels or LEVEL_STACK_HPA) for k in kinds]


# Nominal altitudes every mountain is displayed/compared at (detail.py's
# hourly table), now the EXACT altitudes thanks to the interpolation above.
FIXED_ALTITUDE_BANDS_M = [1000, 2000, 3000]
SUMMIT_LABEL = "summit"  # the interpolation target for a mountain's own elevation


def band_label(alt_m) -> str:
    return f"{int(round(alt_m))}m"


def altitude_col(kind: str, label: str) -> str:
    """Name of a synthesized per-altitude hourly column, e.g.
    'cloudcover_at_2000m' or 'windspeed_at_summit'."""
    return f"{kind}_at_{label}"


BAND_VARS = {alt: altitude_col("cloudcover", band_label(alt)) for alt in FIXED_ALTITUDE_BANDS_M}
WIND_SPEED_BAND_VARS = {alt: altitude_col("windspeed", band_label(alt)) for alt in FIXED_ALTITUDE_BANDS_M}
SUMMIT_VARS = {kind: altitude_col(kind, SUMMIT_LABEL) for kind in LEVEL_KINDS if kind != GPH_KIND}

# The "climb layer" the moisture rule of wet_hour_weight() checks: from the
# summit down CLIMB_LAYER_DEPTH_M -- the summit-ridge zone a climber spends
# the exposed part of the day in. Replaces the old "summit's level + the
# next standard level below it" (which for a 3000m-class summit reached
# ~1640m down, and for a 2000m one ~700m).
#
# Why 600m and not deeper (2026-09-09 night): an 800m layer was tried first
# and blew up on the 2026-09-10 forecast -- a saturated stratus deck at
# ~2000-2150m (MSM 800hPa RH 99% / cloud 80%) under bone-dry 700hPa air, i.e.
# a classic 雲海 with clear summits above it. "Any moist point in the
# layer" flagged every hour for 59 of 76 mountains and zeroed 西穂高岳/
# 木曽駒ヶ岳/白山 (100 -> 0) on what is a photogenic day up top. The
# 唐松岳 9/6 reference case (rain on the descent) looks different in the
# profile: the saturated zone climbs from 850/800hPa up to ~2260-2400m,
# within 300-450m of the 2696m summit, from 14時 to 17時. A 600m layer
# (bottom 2096m) catches those 4 hours and ignores the 2150m deck under a
# 2909m summit. So the rule reads "is saturated air within 600m of the
# summit" -- the ridge is in cloud, or about to be -- while a deck well
# below the ridge is left to the cloud-sea detector. Judgment call; retune
# with scratch_validate_refs.py (唐松岳9/6 must stay < 80) plus a 雲海-type
# day. A graded version (fraction of the layer that is saturated, as the
# hour's wet weight) was prototyped and would score 唐松岳9/6 ~83, so it's
# parked -- see SKILL.md TODO.
CLIMB_LAYER_DEPTH_M = 600
CLIMB_LAYER_MOIST_VAR = "climb_layer_moist"


def level_profile(hourly: dict, kind: str, idx: int) -> list:
    """[(height_m, value)] for one hour, ascending, over the levels where both
    the geopotential height and the value are present."""
    pts = []
    for hpa in LEVEL_STACK_HPA:
        z = hourly.get(level_var(GPH_KIND, hpa))
        v = hourly.get(level_var(kind, hpa))
        if z and v and idx < len(z) and idx < len(v) and z[idx] is not None and v[idx] is not None:
            pts.append((z[idx], v[idx]))
    pts.sort()
    return pts


def interp_at_altitude(hourly: dict, kind: str, idx: int, target_m: float):
    """Value of `kind` at target_m for hour idx, linear in height between
    the bracketing levels; clamped to the lowest/highest level outside the
    profile (a 600m summit reads the 1000hPa~=100m..925hPa~=750m bracket, a
    4400m+ target would read 600hPa). winddirection takes the nearer level
    rather than averaging angles. None when no level has data."""
    pts = level_profile(hourly, kind, idx)
    if not pts:
        return None
    if target_m <= pts[0][0]:
        return pts[0][1]
    if target_m >= pts[-1][0]:
        return pts[-1][1]
    for (z0, v0), (z1, v1) in zip(pts, pts[1:]):
        if z0 <= target_m <= z1:
            if kind == "winddirection":
                return v0 if target_m - z0 <= z1 - target_m else v1
            if z1 == z0:
                return v0
            return v0 + (v1 - v0) * (target_m - z0) / (z1 - z0)
    return None


def add_altitude_columns(hourly: dict, targets: dict, kinds=None) -> None:
    """Synthesize hourly[altitude_col(kind, label)] for every (label ->
    altitude_m) in targets and every kind (default: all LEVEL_KINDS except
    geopotential height). Both scripts' fetch_forecast() call this once
    after merging models, so the rest of the pipeline reads plain columns
    like 'cloudcover_at_2000m' / 'temperature_at_summit'."""
    kinds = [k for k in (kinds or LEVEL_KINDS) if k != GPH_KIND]
    n = len(hourly["time"])
    for label, alt_m in targets.items():
        for kind in kinds:
            hourly[altitude_col(kind, label)] = [interp_at_altitude(hourly, kind, i, alt_m) for i in range(n)]


# Capped-deck check (2026-09-09 night, same 2026-09-10 forecast as above):
# even a 600m layer still flagged 唐松岳 (2696m) for 10 hours on 9/10 because
# the interpolation between 800hPa (2040m, RH 99%) and 700hPa (3158m, RH
# 31%) smears the deck's sharp top into a gradual moistening that only
# falls below RH 90% around 2200m. What actually separates that day from
# the 唐松岳 9/6 rain-on-descent case in the profile is the air ABOVE the
# summit: 9/6 had 700hPa at RH 74-77% (deep moist layer, orographic cloud
# and drizzle), 9/10 had 27-56% (dry, subsiding air = an inversion capping
# a stratus deck = 雲海). So a moist layer BELOW the summit only counts when
# the air CAP_CHECK_ABOVE_M above the summit is not dry (RH >=
# CAP_DRY_RH_PCT); if the summit itself is in the moist layer it always
# counts. Judgment calls -- retune with scratch_validate_refs.py (唐松岳9/6
# keeps 14-17時 -> 4/14h) and a 雲海-type day (2026-09-10 is the pending
# real-world check).
CAP_CHECK_ABOVE_M = 400
CAP_DRY_RH_PCT = 70.0


def climb_layer_moist_series(hourly: dict, summit_m: float, depth_m: float = CLIMB_LAYER_DEPTH_M) -> list:
    """Per hour: True if the summit itself is a moist layer (layer_is_moist:
    RH >= WET_HOUR_RH_PCT and cloud >= WET_HOUR_MOIST_CLOUD_PCT), or if any
    point of the climb layer [summit-depth, summit] is -- checked at the
    layer's bottom and at every real model level inside it (values are
    linear between levels, so those are the extremes) -- AND the air
    CAP_CHECK_ABOVE_M above the summit is not dry (a moist deck under dry
    air is a capped 雲海, not weather the ridge is in). False if none,
    None if no level had data that hour."""
    n = len(hourly["time"])
    bottom = summit_m - depth_m
    out = []
    for i in range(n):
        rh_s = interp_at_altitude(hourly, "relative_humidity", i, summit_m)
        summit_moist = layer_is_moist(rh_s, interp_at_altitude(hourly, "cloudcover", i, summit_m))
        if summit_moist is None:
            out.append(None)
            continue
        if summit_moist:
            out.append(True)
            continue
        heights = [bottom] + [z for z, _ in level_profile(hourly, "relative_humidity", i) if bottom < z < summit_m]
        below = any_layer_moist(*[(interp_at_altitude(hourly, "relative_humidity", i, z),
                                   interp_at_altitude(hourly, "cloudcover", i, z)) for z in heights])
        if not below:
            out.append(False)
            continue
        rh_above = interp_at_altitude(hourly, "relative_humidity", i, summit_m + CAP_CHECK_ABOVE_M)
        capped = rh_above is not None and rh_above < CAP_DRY_RH_PCT
        out.append(not capped)
    return out


KMH_TO_MS = 1 / 3.6


def wind_chill_c(temp_c, wind_speed_kmh):
    """JAG/TI wind chill formula. Only valid for temp_c <= 10 and
    wind_speed_kmh > 4.8; returns temp_c unchanged outside that range."""
    if temp_c is None or wind_speed_kmh is None or temp_c > 10 or wind_speed_kmh <= 4.8:
        return temp_c
    v16 = wind_speed_kmh ** 0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v16 + 0.3965 * temp_c * v16


def safe_avg(values: list) -> float:
    """Average of the non-null values in the list; 0.0 if none are usable."""
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0.0


def safe_avg_or_none(values: list):
    """Like safe_avg, but returns None (not 0.0) when there's no usable
    data. Used for visibility specifically: 0.0 there would silently read
    as "zero visibility = whiteout" -- the worst possible penalty -- for a
    day where the value is simply missing, rather than a benign default
    like safe_avg's 0.0 is for wind/cloud (where 0 genuinely means calm/
    clear)."""
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


# ---------------------------------------------------------------------------
# Penalty functions (each returns 0-100) and mountain_climb_score. Thresholds
# are rule-of-thumb judgment calls, not a physical law -- documented so
# they're easy to argue with and retune, not because they're precise.
# ---------------------------------------------------------------------------
def thunder_penalty(cape) -> float:
    """CAPE (J/kg), peak value over the PM window. Thunderstorm risk is a
    threshold hazard, not a smooth dial, so this ramps hard once CAPE enters
    the range Japanese summer convection typically reaches on unstable
    afternoons (roughly 1000-2500+ J/kg = moderate-to-strong)."""
    if cape is None:
        return 0.0
    if cape < 300:
        return 0.0
    if cape < 1000:
        return (cape - 300) / (1000 - 300) * 40
    if cape < 2500:
        return 40 + (cape - 1000) / (2500 - 1000) * 50
    return 100.0


def wind_penalty(speed_ms) -> float:
    """Ridge-band wind speed (m/s). Thresholds cross-checked against JMA's
    public wind-strength scale plus mountaineering-specific guides (which
    run more conservative than JMA's flat-ground scale, since ridge/rock
    footing turns the same wind speed into a fall risk): ~8m/s is where
    novices are commonly told to start reconsidering; ~12m/s is where most
    guides say novices should turn back and experienced climbers should
    avoid ridges/rock; ~15m/s is widely cited as "everyone should stop" for
    ridge travel, so full penalty is reached there rather than at 20m/s."""
    if speed_ms is None:
        return 0.0
    if speed_ms < 8:
        return 0.0
    if speed_ms < 12:
        return (speed_ms - 8) / (12 - 8) * 40
    if speed_ms < 15:
        return 40 + (speed_ms - 12) / (15 - 12) * 60
    return 100.0


# Same 15m/s "everyone should stop for ridge travel" threshold wind_penalty
# above already treats as full-penalty -- reused as the bar for day_peak
# wind warnings (mvp.py's window_scores_by_day/day_peak, detail.py's
# compute_day_scores/day_peak, and both files' main()'s wind_warning). Not a
# new judgment call, just applying the existing one to hours outside the
# scored ridge window too.
WIND_PEAK_WARNING_MS = 15.0


def cloud_penalty(cloud_pct) -> float:
    """Cloud cover *at* the mountain's own altitude band (not a "look up
    from below" composite) -- i.e. are you actually standing in cloud/fog
    up there. Used directly as the 0-100 penalty.

    Caller passes the worse of the ridge-dwell window and the PM descent
    window (2026-09 widening, see mountain_climb_score's docstring) so an
    afternoon cloud-up doesn't go unscored just because the ridge window
    itself was clear."""
    return cloud_pct if cloud_pct is not None else 0.0


# PM cloud "sustained peak" (2026-09-09): how many consecutive hours a
# cloud-up has to last before it counts at full strength as the PM window's
# cloud value. The PM window used to take the single highest hour, and
# because cloud_penalty() feeds the geometric mean directly, ONE
# interpolated 100% hour (ECMWF's 3-6h values beyond MSM's range, smeared to
# hourly) zeroed the whole day -- 塔ノ岳9/12 in the 2026-09-09 cache: ridge
# cloud 30%, wet 0%, visibility 7.9km, score 0.0. Meanwhile a real
# afternoon cloud-up (唐松岳9/6-style) lasts for hours, so requiring the
# peak to be the mean over PM_CLOUD_PERSIST_HOURS consecutive hours keeps
# that case intact and only damps the one-hour blips. 2h, not 3h, because
# the PM window is ~6h long (12時〜日没+30分) and a 3h requirement would start
# averaging away genuine 2-hour squalls. A judgment call, easy to retune --
# re-run the three reference days (see wet_hour_weight's banner) plus
# 塔ノ岳9/12 after touching it. Thunder (CAPE) deliberately stays a
# single-hour peak: it's a threshold hazard, not a score input.
PM_CLOUD_PERSIST_HOURS = 2


def sustained_peak(values: list, hours: int = PM_CLOUD_PERSIST_HOURS):
    """Highest mean over any run of `hours` consecutive samples (None
    samples are dropped first, so the run is over the hours that have
    data). Fewer samples than `hours` -> mean of all of them; no samples ->
    None. With hours=1 this is a plain max()."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    if len(clean) <= hours:
        return sum(clean) / len(clean)
    return max(sum(clean[i:i + hours]) / hours for i in range(len(clean) - hours + 1))


def visibility_penalty(ridge_visibility_m) -> float:
    """Ridge-band visibility (m), Open-Meteo's own visibility variable
    averaged over the ridge window -- a direct measure of whiteout/gas risk,
    distinct from cloud_penalty even though the two correlate (you can be
    "in cloud" per cloudcover% at very different actual sighting distances).
    <1000m is a common mountaineering-guide threshold where route-finding
    and fall risk start rising; <100m is treated as near-whiteout."""
    if ridge_visibility_m is None:
        return 0.0
    if ridge_visibility_m >= 1000:
        return 0.0
    if ridge_visibility_m >= 100:
        return (1000 - ridge_visibility_m) / (1000 - 100) * 70
    return min(100.0, 70 + (100 - ridge_visibility_m) / 100 * 30)


# Activity-window precipitation total (mm) at/above which the intensity side
# of precip_penalty() is at full penalty (see its docstring). JMA calls
# 10-20mm/h "やや強い雨" and 20mm+/h "強い雨"; 20mm over a climbing day means
# either one such hour or a genuinely wet afternoon, both of which a climber
# would call a washout. Round number, not a fitted value -- easy to retune.
PRECIP_FULL_PENALTY_TOTAL_MM = 20.0


def precip_penalty(wet_pct, mm) -> float:
    """Takes the *worse* of wet_pct and intensity, not a blend.

    wet_pct (2026-09 widened, second pass): no longer a single hour's
    probability -- see mountain_climb_score's docstring -- but the
    percentage of the day's activity-window HOURS that are "wet"
    (window_scores_by_day()'s/compute_day_scores()'s wet_fraction_pct). A
    day that's rainy for most of the window (終日雨) scores high here even
    if no single hour hit 100%; a single passing-shower hour among many dry
    ones (通り雨) scores low even if that one hour's probability spiked,
    matching how a climber actually experiences "was today wet" (confirmed
    against real hiking reports for 槍ヶ岳9/5 and 立山9/5 -- both had one
    isolated high-probability hour and were reported as good days; the
    older peak-based version scored them down as if the rain had been
    sustained). Originally (first pass, 唐松岳9/6) this was itself a single
    hourly probability, worst-of-AM/PM; a persistent 90% chance of light
    rain was already bad enough to matter even with low accumulation (that
    day: ~1mm/day total, but the old 50/50-blended formula gave only
    ~45/100, letting a genuinely wet, dangerous descent score as "fine") --
    the max(wet_pct, intensity) combination below still captures that same
    case now that wet_pct is duration-based instead of a single probability.

    mm (2026-09-09, third pass) is the activity window's TOTAL precipitation
    (mm summed over the window's hours), no longer its PEAK hourly rate.
    The peak version hit full penalty at 5mm/h, so a single hour at 5mm/h
    (a passing shower on an otherwise dry day) zeroed the whole day's
    score -- 119 of the 1140 mountain-days in the 2026-09-09 cache were
    at that cap, most of them one-hour ECMWF-interpolated spikes -- which
    is the same single-hour-peak failure the wet_pct side was already
    redesigned away from. The window total is what a climber actually
    carries home ("how much rain fell on me today"): a 1h/5mm shower is
    5mm -> 25% penalty (the day is dented, not destroyed), a sustained
    afternoon of 2h x 8mm/h is 16mm -> 80%, and anything at or above
    PRECIP_FULL_PENALTY_TOTAL_MM is a washout. Sustained light rain
    (0.5mm/h x 14h = 7mm) is caught by the wet_pct side anyway, which is
    why the intensity side only needs to answer "short but heavy"."""
    wet_pct = wet_pct or 0.0
    mm = mm or 0.0
    return max(wet_pct, min(mm / PRECIP_FULL_PENALTY_TOTAL_MM, 1.0) * 100.0)


def wet_chill_adjustment_c(precip_wet_pct, precip_mm) -> float:
    """Extra degrees to subtract from wind-chill before temp_penalty(),
    when the activity window's precipitation is both widespread and
    non-trivial (precip_wet_pct/precip_mm, see precip_penalty's docstring).
    Hypothermia progresses fastest under a low-temp + wind + WET
    combination, but wind_chill_c() only models the dry (still-air-
    adjusted) component -- this is a deliberately simple single-step
    correction (not a continuous ramp) rather than a second full scoring
    axis; threshold and magnitude are a judgment call, easy to retune.
    precip_mm is the activity window's TOTAL mm since 2026-09-09 (it was
    the peak mm/h before); the 0.5mm gate was kept as-is -- it only exists
    to rule out a "wet" half-day that is all ridge fog and no rain."""
    wet_pct = precip_wet_pct or 0.0
    mm = precip_mm or 0.0
    if wet_pct >= 50.0 and mm >= 0.5:
        return -3.0
    return 0.0


def temp_penalty(chill_c) -> float:
    """Wind-chill at the ridge band (already wet-adjusted by the caller via
    wet_chill_adjustment_c). >=5C is treated as fine with normal layering;
    below -15C is treated as frostbite-risk territory."""
    if chill_c is None:
        return 0.0
    if chill_c >= 5:
        return 0.0
    if chill_c >= -5:
        return (5 - chill_c) / 10 * 30
    if chill_c >= -15:
        return 30 + (-5 - chill_c) / 10 * 40
    return 100.0


# Weights (2026-09 redesign, third pass -- now exponents of a weighted
# geometric mean, not a weighted sum): thunderstorm, wind, and
# wind-chill/hypothermia used to carry weight in an additive blend here, but
# a user incident (唐松岳 9/6: dry/calm morning and ridge window, score 100,
# while rain and strong wind hit the actual descent) and the resulting
# design discussion converged on a different split of responsibilities
# instead of just re-tuning numbers:
#   - This score now covers only "is the view/climb itself pleasant and dry"
#     -- cloud, visibility, precip. It answers "should I go" for what the
#     user explicitly wants the ranking based on: "seeing blue sky up there
#     is the best case" (登山者目線では青空が見えるのが最高), which is an AND
#     of all three, not a diluted average of them -- see
#     mountain_climb_score's docstring for why that pushed the aggregation
#     itself from a weighted sum to a weighted geometric mean.
#   - Thunderstorm risk, wind, and cold/hypothermia are all threshold
#     hazards, not everyday-comfort ones -- they're fine right up until
#     they're suddenly not. Blending any of them into one number let a day
#     with real danger hide behind otherwise-good conditions (exactly what
#     happened 9/6), and the opposite failure mode too (a merely-cloudy day
#     scoring lower than its actual danger level warranted). All three are
#     now surfaced as their own explicit warning (see hazard_level()/
#     mountain_hazards() below) that can't be diluted into a passing score.
#   - Equal thirds (1/3 each) was the deliberate starting point, not a fitted
#     value -- cloud/visibility/precip correlate as weather but represent
#     distinct climber experiences (in-cloud / can't-see-far / getting-wet),
#     and there wasn't a strong enough reason to weight one over another
#     without real-day validation first.
#   - **Retuned to precip 50% / cloud 25% / visibility 25% (2026-09, after
#     real-day validation)**: re-testing the equal-thirds version against
#     唐松岳9/6 (the incident that motivated this whole redesign, now scored
#     with the duration-based precip_wet_pct -- see precip_penalty's
#     docstring) still gave 81.1 -- technically a "go" above
#     mvp.py's MIN_SCORE_THRESHOLD=80, despite the user's judgment that a day
#     with 5 of 14 activity-window hours crossing 50% ground precip
#     probability (ramping to 90% by evening) shouldn't score as a green
#     light. Cloud (11%) and visibility (913m, barely over the whiteout
#     threshold) happened to look good enough that equal weighting couldn't
#     sink the geometric mean far enough on precip alone. Raising precip to
#     50% is a direct response to that verified gap, not a theoretical
#     preference -- see the "既知の精度傾向" / TODO sections of SKILL.md for
#     the before/after scores this produced on 唐松岳9/6, 立山9/5, 槍ヶ岳9/5.
# thunder_penalty/wind_penalty/temp_penalty (below) are unchanged and still
# used -- just to build that separate warning now, not to weight this score.
SCORE_WEIGHTS = {"cloud": 0.25, "visibility": 0.25, "precip": 0.50}


def mountain_climb_score(*, cloud_pct, ridge_visibility_m, precip_wet_pct, precip_mm) -> float:
    """Climbing-oriented score: a weighted GEOMETRIC mean (not a weighted
    sum) of three "satisfaction" factors -- 1 minus each of cloud cover,
    ridge visibility, and precipitation's 0-100 penalty, rescaled to 0-1 --
    raised to SCORE_WEIGHTS' exponents and multiplied together, then scaled
    back to 0-100. See SCORE_WEIGHTS and the penalty_* functions above.
    Thunderstorm (CAPE), wind, and wind-chill/hypothermia risk are
    deliberately NOT inputs here -- see SCORE_WEIGHTS' comment for why --
    call mountain_hazards() alongside this for those three.

    Geometric, not arithmetic (2026-09, third pass): the user's framing --
    "登山者目線では、その場に行って青空が見えるのが最高" (seeing blue sky up
    there is the peak experience) -- describes an AND of clear/visible/dry,
    not conditions that trade off against each other. A weighted SUM lets a
    great score on one axis buy back a bad score on another (exactly the
    dilution problem this whole redesign has been chasing: thunder/wind
    hiding behind good cloud/precip on 唐松岳 9/6, then precip's own
    probability/intensity averaging hiding a bad half). A plain PRODUCT of
    the three factors was tried first and rejected as too harsh (three
    80%-satisfaction axes multiply to ~51%, punishing a merely-good day as
    if it were bad); a weighted geometric mean fixes that -- since the
    weights sum to 1, three equal 80%-satisfaction axes still average out to
    80%, while one genuinely bad axis (near 0) still pulls the whole score
    toward 0 the way a raw product does. Satisfaction is clamped to >=0
    before exponentiation so a Python float's `0.0 ** (positive exponent)`
    (which is well-defined, unlike `0 ** 0` or negative bases) is the only
    edge case reached.

    cloud_pct (2026-09 widened): the worse of the ridge-dwell window and the
    PM descent window, not the ridge window alone. Confirmed against a real
    incident (2026-09, 唐松岳 9/6): a morning that's completely dry and clear
    can still score ~100 while rain moves in through the early afternoon,
    because the PM window previously fed only CAPE into the score. Callers
    (mvp.py's main(), detail.py's compute_day_scores()) take max(ridge_value,
    pm_value) for cloud before calling this, so a bad PM cloud-up can no
    longer be invisible to the score. pm_value is the PM window's
    sustained peak (sustained_peak() over PM_CLOUD_PERSIST_HOURS, 2026-09-09
    -- see that constant's comment), not its single worst hour.

    precip_wet_pct/precip_mm (2026-09, second pass -- see precip_penalty's
    docstring): precip_wet_pct is no longer a single window's probability
    either; it's the percentage of the whole activity window's HOURS that
    are wet, and precip_mm is that window's total mm (2026-09-09; was the
    peak hourly rate -- see precip_penalty's docstring). Both already
    span climb-through-typical-descent in one window (see
    window_scores_by_day()'s "activity" / compute_day_scores()'s
    activity_by_day), so there's no separate AM/PM max to take for precip
    the way cloud still needs above.

    Takes plain scalars (not a windows/elevation structure) so the exact
    same function serves both mvp.py (fixed 1000/2000/3000m bands shared
    across all 75 mountains) and detail.py (the selected mountain's own
    exact pressure level) -- only how each script *derives* these inputs
    differs; the scoring itself is shared here."""
    satisfaction = {
        "cloud": max(0.0, 1 - cloud_penalty(cloud_pct) / 100.0),
        "visibility": max(0.0, 1 - visibility_penalty(ridge_visibility_m) / 100.0),
        "precip": max(0.0, 1 - precip_penalty(precip_wet_pct, precip_mm) / 100.0),
    }
    score = 100.0
    for k, weight in SCORE_WEIGHTS.items():
        score *= satisfaction[k] ** weight
    return max(0.0, round(score, 1))


# Hazard levels for thunder/wind/cold, all kept out of mountain_climb_score
# entirely (see SCORE_WEIGHTS' comment) and surfaced as their own explicit
# warning instead. Built on top of thunder_penalty/wind_penalty/temp_penalty's
# existing 0-100 severity curves rather than re-deriving new thresholds, so
# the hazard bands stay anchored to the same judgment calls documented on
# those functions.
HAZARD_LEVELS = (
    (0, "-"),
    (40, "注意"),
    (80, "警戒"),
    (100.0001, "危険"),  # upper bound exclusive-below, so 100 itself lands here
)


def hazard_level(penalty_0_100: float) -> str:
    """Maps a 0-100 penalty (from thunder_penalty/wind_penalty/temp_penalty)
    to a 4-level warning label: "-" (none) / "注意" (caution) / "警戒"
    (warning) / "危険" (danger)."""
    for threshold, label in HAZARD_LEVELS:
        if penalty_0_100 <= threshold:
            return label
    return "危険"


def mountain_hazards(*, ridge_wind_ms, pm_cape, chill_c, precip_wet_pct, precip_mm) -> dict:
    """Thunder/wind/cold(hypothermia) hazard levels, independent of
    mountain_climb_score (see that function's docstring and SCORE_WEIGHTS'
    comment for why these three were pulled out of the weighted score).
    chill_c/precip_wet_pct/precip_mm feed the same wet-chill adjustment
    (wet_chill_adjustment_c) temp_penalty always used, just computed here
    instead of inside the score. Returns {"thunder": "-"/"注意"/"警戒"/"危険",
    "wind": same, "cold": same, "any": bool}."""
    wet_chill_c = None if chill_c is None else chill_c + wet_chill_adjustment_c(precip_wet_pct, precip_mm)
    thunder = hazard_level(thunder_penalty(pm_cape))
    wind = hazard_level(wind_penalty(ridge_wind_ms))
    cold = hazard_level(temp_penalty(wet_chill_c))
    return {"thunder": thunder, "wind": wind, "cold": cold,
            "any": thunder != "-" or wind != "-" or cold != "-"}


# ---------------------------------------------------------------------------
# Per-hour "wet" judgment for the activity window's wet_fraction_pct
# (2026-09-09, MSM-first redesign; moisture rule added the same day; the
# beyond-MSM expected-value rule added the same evening).
# Shared by mvp.py's window_scores_by_day() and detail.py's
# compute_day_scores().
#
# Background: Open-Meteo's jma_msm serves NO precipitation_probability, cape,
# lifted_index or convective_inhibition (all-null, confirmed live 2026-09-09
# against the API). Before this change every "wet hour" -- 50% of the score
# -- came from ecmwf_ifs025's ensemble-derived probability (~25km grid) even
# inside MSM's own ~3-day range, while MSM's 5km precipitation (which IS
# served) was only used for the peak-mm/h side.
#
# Rule, per hour -- wet_hour_weight() returns a 0..1 "wetness" weight, and
# wet_fraction_pct is the mean of those weights over the window:
#   - MSM available (inside MSM's range, roughly today+2): weight 1.0 if
#       MSM mm/h >= WET_HOUR_MSM_PRECIP_MM
#           or  a "moist layer" is on the mountain: relative humidity >=
#               WET_HOUR_RH_PCT AND cloud cover >= WET_HOUR_MOIST_CLOUD_PCT
#               anywhere in the climb layer -- the summit down to
#               CLIMB_LAYER_DEPTH_M below it, the slope the climber walks
#               up/down through (climb_layer_moist_series(); until
#               2026-09-09 evening this was "the summit's pressure level or
#               the next standard level below it"). This is the
#               mountain-meteorology reading: "is saturated air sitting on
#               the ridge/slope," which a 5km deterministic model resolves
#               even when it converts none of it into mm (drizzle, ridge
#               fog, orographic stratus).
#       else 0.0. (ECMWF probability is NOT consulted here unless
#       WET_HOUR_KEEP_ECMWF_PROB_IN_MSM_RANGE is flipped on -- kept as an
#       A/B switch.)
#   - No MSM value (beyond its range, or model=None past-date lookups):
#       weight = ECMWF probability / 100 -- the hour's EXPECTED wetness,
#       so the window's fraction is the expected fraction of wet hours
#       (E[wet hours] = sum of per-hour probabilities). Until 2026-09-09
#       this was a hard >= WET_HOUR_PRECIP_THRESHOLD_PCT (50%) cut, which
#       scored a day of 30-49% every hour (丹沢9/12 in the 2026-09-09 cache:
#       12 of 14 hours) as bone dry, and a day of 50% every hour as fully
#       wet -- a cliff with nothing between. Why the probability rather
#       than extending the moisture rule to ECMWF's own pressure-level
#       RH/cloud: at 4+ days out a single deterministic run's hourly mm
#       or RH is noise, and the ensemble probability is precisely the
#       tool built for that lead time (the false alarms that motivated the
#       MSM rule were all inside MSM's range, where MSM now overrides). The
#       moisture rule on ECMWF levels remains a future candidate once it
#       can be validated (SKILL.md TODO). The old 50% cut is kept as an A/B
#       switch (WET_HOUR_BEYOND_MSM_USE_THRESHOLD).
#   - Neither available: None (the hour is excluded from the fraction).
#
# Why (validated 2026-09-09 on past-date MSM data, see CHANGELOG):
#   - 唐松岳9/6 (the incident this scoring redesign exists for; rain on the
#     descent, ~1mm/day): MSM precipitation was 0.0mm at the point AND on all
#     25 grid cells within +-10km for every activity hour -- a neighborhood
#     probability would have scored the day ~97 (a miss). But MSM's 850hPa
#     layer (the descent) moistened 80->96% RH with cloud 37->58% from 13時
#     (and 800hPa ~2000m, seen once the level stack was widened: RH 94-97%,
#     cloud 52-66% 14-18時), wind veering SW/W: the moisture rule flags
#     14-18時.
#   - 立山9/5 and 槍ヶ岳9/5 (reported fine days): ECMWF probability put
#     50-80% on hours where MSM's 700hPa RH was 14-29% (dry aloft, likely
#     false alarms); the moisture rule leaves 立山 with ~1h and 槍 with the
#     one 15時 hour where RH700 hit 94% / cloud 56% (a real late-afternoon
#     summit cloud-up, after most people were down).
#   Thresholds: RH 90 / cloud 40 reproduce the user's own call that
#   唐松岳9/6 must stay below 80 (5/14h wet -> 77.9, same as before);
#   cloud 50 would drop 14時 and lift it to 82.1. Judgment calls, easy to
#   retune -- re-run scratch validation on the three reference days after
#   touching any of them.
# ---------------------------------------------------------------------------
WET_HOUR_PRECIP_THRESHOLD_PCT = 50.0  # beyond MSM, A/B only: hard cut at/above this probability
WET_HOUR_MSM_PRECIP_MM = 0.1          # MSM: an hour is wet at/above this MSM hourly precipitation (mm/h)
WET_HOUR_RH_PCT = 90.0                # MSM: ... or a moist layer: RH at/above this ...
WET_HOUR_MOIST_CLOUD_PCT = 40.0       #      ... AND cloud cover at/above this, at summit or slope level
WET_HOUR_KEEP_ECMWF_PROB_IN_MSM_RANGE = False  # A/B switch: also count ECMWF prob>=50% inside MSM range
WET_HOUR_BEYOND_MSM_USE_THRESHOLD = False      # A/B switch: beyond MSM, the pre-2026-09-09 hard 50% cut instead of prob/100
MSM_PRECIP_VAR = "precipitation_msm"  # raw jma_msm precipitation, None beyond MSM's range


def layer_is_moist(rh_pct, cloud_pct):
    """True/False for one layer at one hour; None if either input is missing."""
    if rh_pct is None or cloud_pct is None:
        return None
    return rh_pct >= WET_HOUR_RH_PCT and cloud_pct >= WET_HOUR_MOIST_CLOUD_PCT


def any_layer_moist(*rh_cloud_pairs):
    """any() over layer_is_moist() for (rh, cloud) pairs, ignoring layers with
    missing data; None if no layer had data."""
    flags = [layer_is_moist(rh, c) for rh, c in rh_cloud_pairs]
    flags = [f for f in flags if f is not None]
    return any(flags) if flags else None


def wet_hour_weight(prob_pct, msm_mm, moist=None):
    """0..1 wetness weight for one hour per the rule above (1.0/0.0 inside
    MSM's range, probability/100 beyond it); None when nothing is usable.
    `moist` is any_layer_moist()'s result for this hour (only consulted
    inside MSM's range, i.e. when msm_mm is not None)."""
    if msm_mm is not None:
        wet = msm_mm >= WET_HOUR_MSM_PRECIP_MM
        if moist:
            wet = True
        if WET_HOUR_KEEP_ECMWF_PROB_IN_MSM_RANGE and prob_pct is not None:
            wet = wet or prob_pct >= WET_HOUR_PRECIP_THRESHOLD_PCT
        return 1.0 if wet else 0.0
    if prob_pct is not None:
        if WET_HOUR_BEYOND_MSM_USE_THRESHOLD:
            return 1.0 if prob_pct >= WET_HOUR_PRECIP_THRESHOLD_PCT else 0.0
        return max(0.0, min(1.0, prob_pct / 100.0))
    return None


def is_wet_hour(prob_pct, msm_mm, moist=None):
    """Boolean view of wet_hour_weight() (weight >= 0.5); None when nothing
    is usable. Kept for callers that want a yes/no per hour."""
    w = wet_hour_weight(prob_pct, msm_mm, moist)
    return None if w is None else w >= 0.5


def wet_fraction(prob_series: list, msm_mm_series: list, moist_series: list = None) -> dict:
    """Aggregate wet_hour_weight() over one activity window. Returns
    {"wet_hours", "total_hours", "msm_hours", "moist_hours", "prob_hours",
    "wet_fraction_pct"}; wet_hours is the SUM of weights (a whole number
    inside MSM's range, fractional -- expected wet hours -- beyond it);
    msm_hours is how many of total_hours had an MSM value, prob_hours how
    many were judged on the ECMWF probability instead, moist_hours how many
    MSM-judged wet hours came from the moisture rule alone (no MSM mm)."""
    moist_series = moist_series or [None] * len(prob_series)
    wet = 0.0
    total = msm = moist_only = prob_hours = 0
    for prob, mm, moist in zip(prob_series, msm_mm_series, moist_series):
        w = wet_hour_weight(prob, mm, moist)
        if w is None:
            continue
        total += 1
        wet += w
        if mm is not None:
            msm += 1
            if w and mm < WET_HOUR_MSM_PRECIP_MM and moist:
                moist_only += 1
        else:
            prob_hours += 1
    return {"wet_hours": round(wet, 1), "total_hours": total, "msm_hours": msm, "moist_hours": moist_only,
            "prob_hours": prob_hours, "wet_fraction_pct": (100.0 * wet / total) if total else 0.0}


# Precip *timing* (which specific day/hour the peak rain lands) is
# genuinely uncertain at this lead time and in this probability band -- not
# something this tool's methodology can resolve further. Confirmed 2026-09
# by comparing 立山 against Windy/Mountain-Forecast/WeatherNews for the same
# 2-3-day-out rain event: services split roughly evenly on which day (9/3
# vs 9/4) would see the worse rain (see SKILL.md's 既知の精度傾向).
# Thresholds are round numbers, not a fitted model -- easy to retune.
# Only the constants are shared here -- mvp.py's precip_timing_note(day_index,
# rows) and detail.py's precip_timing_note(day_scores) take differently
# shaped arguments (per-day-table vs whole-forecast-dict), so the function
# itself stays defined separately in each file.
PRECIP_TIMING_UNCERTAIN_DAYS_OUT = 3
PRECIP_TIMING_BOUNDARY_LOW = 40.0
PRECIP_TIMING_BOUNDARY_HIGH = 60.0
PRECIP_TIMING_NOTE = "※この先の降水タイミングは日によって変わりやすいため、直前に再確認することをおすすめします。"


# ---------------------------------------------------------------------------
# HTTP + raw-JSON response cache
# ---------------------------------------------------------------------------
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3


def get_with_retry(url: str, params: dict):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = 2 * attempt  # 2s, 4s, ...
                print(f"  (通信エラー、{wait}秒後にリトライ {attempt}/{MAX_RETRIES}: {e})")
                time_module.sleep(wait)
    raise last_error


# Open-Meteo responses are cached to disk as the exact raw JSON the API
# returned, one file per (lat, lon, model, forecast_days[, past_days]). A
# query that only needs variables already in a fresh cache file is answered
# from disk with no network call; a query that needs variables not yet
# cached fetches only those and merges them into the cached JSON. Shared by
# both scripts (same cache/ directory, same file-naming scheme) -- they
# never collide because mvp.py always requests days=15 and detail.py
# days=14, giving each script its own cache files per (lat, lon, model).
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_TTL_SECONDS = 3 * 60 * 60  # upstream forecasts refresh every few hours


def _cache_path(lat: float, lon: float, model: str, days: int, past_days: int = 0) -> str:
    # past_days suffix only when non-default, so the normal (forward-only)
    # cache entries both scripts' main pipelines rely on are untouched --
    # only detail.py's one-off past-date lookups (scratch_past_date.py) ever
    # pass a non-zero past_days.
    suffix = f"_past{past_days}d" if past_days else ""
    return os.path.join(CACHE_DIR, f"{model or 'best_match'}_{lat:.4f}_{lon:.4f}_{days}d{suffix}.json")


def _load_cache(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(path: str, data: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def fetch_open_meteo(lat: float, lon: float, hourly: list = None, daily: list = None,
                      days: int = 14, model: str = None, force_refresh: bool = False,
                      past_days: int = 0) -> dict:
    """General-purpose, cached Open-Meteo forecast query -- the single entry
    point both mvp.py and detail.py go through (via each file's own
    fetch_single_model/fetch_forecast wrappers) for every Open-Meteo call.

    Pass any hourly/daily variable name(s) the Open-Meteo forecast API
    supports (https://open-meteo.com/en/docs) -- not limited to the
    variables either script currently uses. This is the entry point for
    pulling in a new variable for one-off analysis without touching the
    scoring pipeline, e.g.:

        import mountain_weather_core as core
        data = core.fetch_open_meteo(36.7514, 137.7622, hourly=["temperature_2m", "snow_depth"], days=3)

    past_days: Open-Meteo's own `past_days` param (0-92) -- prepends that
    many days of actual/short-range-hindcast data (same models, not a true
    observational reanalysis) before "today" onto the returned hourly/daily
    arrays, so e.g. past_days=3 lets you look at a date 3 days ago. Both
    scripts' normal scoring pipelines are forward-looking only and never set
    this; it exists for one-off past-date lookups (see
    scratch_past_date.py).

    Caching: responses are stored as raw JSON keyed by (lat, lon, model,
    days, past_days). If the cache is fresh (< CACHE_TTL_SECONDS old) and
    already has every requested variable, this reads the cache with no
    network call. If it's fresh but missing some requested variables, only
    those are fetched and merged in. If there's no cache or it's stale,
    everything requested is fetched fresh. Pass force_refresh=True to
    bypass the cache entirely.

    Returns the (possibly merged) raw Open-Meteo JSON dict.
    """
    hourly = hourly or []
    daily = daily or []
    path = _cache_path(lat, lon, model, days, past_days)
    cached = None if force_refresh else _load_cache(path)

    is_fresh = cached is not None and (time_module.time() - cached.get("_fetched_at", 0)) < CACHE_TTL_SECONDS
    have_hourly = set(cached.get("hourly", {}).keys()) if is_fresh else set()
    have_daily = set(cached.get("daily", {}).keys()) if is_fresh else set()
    missing_hourly = [v for v in hourly if v not in have_hourly]
    missing_daily = [v for v in daily if v not in have_daily]

    if is_fresh and not missing_hourly and not missing_daily:
        return cached

    fetch_hourly = missing_hourly if is_fresh else hourly
    fetch_daily = missing_daily if is_fresh else daily

    params = {"latitude": lat, "longitude": lon, "forecast_days": days, "timezone": "Asia/Tokyo"}
    if past_days:
        params["past_days"] = past_days
    if fetch_hourly:
        params["hourly"] = fetch_hourly
    if fetch_daily:
        params["daily"] = fetch_daily
    if model:
        params["models"] = model
    resp = get_with_retry(OPEN_METEO_URL, params)
    fresh = resp.json()

    if is_fresh:
        # Partial merge: the cache is within TTL but lacks some variables.
        # The API always returns a forecast starting from *today*, so if
        # the cache was written before midnight and this top-up runs after
        # it, fresh["hourly"]["time"] starts a day later than the cached
        # axis. Blindly .update()-ing would replace "time" and leave every
        # older column misaligned by 24h (found 2026-09-10 when the terrain
        # layer added winddirection to mvp.py's variable set). So re-index
        # the new columns onto the CACHED time axis (hours the fresh call
        # doesn't cover -> None) and never touch "time" itself.
        merged = cached
        cached_hourly = merged.setdefault("hourly", {})
        fresh_hourly = fresh.get("hourly", {})
        cached_times = cached_hourly.get("time")
        fresh_times = fresh_hourly.get("time")
        if cached_times and fresh_times and cached_times != fresh_times:
            fresh_index = {t: i for i, t in enumerate(fresh_times)}
            for var, values in fresh_hourly.items():
                if var == "time":
                    continue
                cached_hourly[var] = [values[fresh_index[t]] if t in fresh_index and fresh_index[t] < len(values) else None
                                      for t in cached_times]
        else:
            cached_hourly.update(fresh_hourly)
        if fresh.get("daily"):
            cached_daily = merged.setdefault("daily", {})
            fresh_daily = fresh["daily"]
            if cached_daily.get("time") and fresh_daily.get("time") and cached_daily["time"] != fresh_daily["time"]:
                fidx = {t: i for i, t in enumerate(fresh_daily["time"])}
                for var, values in fresh_daily.items():
                    if var == "time":
                        continue
                    cached_daily[var] = [values[fidx[t]] if t in fidx and fidx[t] < len(values) else None
                                         for t in cached_daily["time"]]
            else:
                cached_daily.update(fresh_daily)
    else:
        merged = fresh
        merged.setdefault("hourly", {})
        merged.setdefault("daily", {})

    merged["_fetched_at"] = time_module.time()
    _save_cache(path, merged)
    return merged


# ---------------------------------------------------------------------------
# JMA himawari satellite imagery (Japan-area tiles) -- optional, 2026-09
# addition. Same role as fetch_jma_weather_map() in mountain_weather_detail.py
# (an "eyeball the real thing" corroboration source, not fed into
# mountain_climb_score), but for cloud imagery specifically: the infrared
# band works at night, unlike a forecast cloudcover% -- useful for confirming
# there's actually cloud on the ridge right now before a pre-dawn start.
# 雲頂強調画像(cloud-top enhanced) doubles as a rough visual cross-check on a
# high CAPE reading (mountain_hazards()' thunder threshold).
#
# Placed here rather than mountain_weather_detail.py because it's
# lat/lon-only with no dependency on MOUNTAINS or the Open-Meteo pipeline
# (2026-09 request: keep it callable standalone from other projects).
#
# Unlike fetch_jma_weather_map(), this needs no Playwright: the page
# (https://www.jma.go.jp/bosai/map.html, 気象衛星ひまわり) renders tiles via
# plain <img> tags at fixed URLs, not a canvas/JS-only draw -- confirmed
# live (2026-09) by opening that page in a browser, switching the image-type
# dropdown, and reading performance.getEntriesByType('resource') for the
# actual tile requests, rather than guessing the URL shape. Two things
# that request inspection surfaced and aren't obvious from the page's own
# UI labels:
#   - The dropdown's element code (e.g. "ir") is NOT the path segment the
#     tile URL uses -- HIMAWARI_IMG_TYPES below maps the public code to the
#     real one (e.g. "ir" -> "B13/TBB"). Only the two this project needed
#     were confirmed this way; the dropdown also offers vis/vap/color/
#     nightmicro/naturalcolor/snowfog/daymicro, whose real path codes are
#     NOT in the dict below (not verified against real traffic -- add them
#     the same way, don't guess from the pattern of the two known ones).
#   - Japan-area ("jp") tiles are served at a single fixed zoom (z=6 --
#     confirmed both by the page's own tile-layer config, which pins
#     minZoom=maxZoom=6 for "jp", and by every request captured regardless
#     of the map's on-screen zoom level). This is coarser than
#     fetch_jma_weather_map()'s screenshot (that one crops JMA's own
#     already-rendered <img>, at whatever resolution JMA draws it) -- each
#     256x256 z=6 tile spans roughly 5.6 degrees, so this is regional cloud
#     context (tens of km/pixel), not summit-precise imagery.
# This module covers "jp" (Japan-area) tiles only, per this feature's scope
# -- the full-disk/global ("fd") tile set uses a different zoom range and
# was not exercised through this same verification, so it's intentionally
# not supported here.
#
# 利用規約: 気象庁の公共データ利用規約に基づく扱いは
# fetch_jma_weather_map()と同じ(mountain_weather_detail.pyのそちらの
# docstring、SKILL.mdの「気象庁天気図スクリーンショット機能」節を参照) --
# 出典表記必須(HIMAWARI_ATTRIBUTION)、気象業務法17条・23条によりこれを元に
# した独自予報・警報の公表は禁止、節度あるアクセス(ループ処理や短時間の
# 連続呼び出しはしない)。持続的なディスクキャッシュは持たせていない
# (fetch_jma_weather_map()と同じ判断)。
# ---------------------------------------------------------------------------
HIMAWARI_BASE_URL = "https://www.jma.go.jp/bosai/himawari/data/satimg"
HIMAWARI_ZOOM = 6  # fixed for the "jp" area -- see banner comment
HIMAWARI_TILE_PX = 256
HIMAWARI_ATTRIBUTION = "出典:気象庁ホームページ (https://www.jma.go.jp/bosai/map.html)"
HIMAWARI_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")

# Public dropdown code -> real tile-path code (see banner comment; only
# these two are confirmed against live traffic).
HIMAWARI_IMG_TYPES = {
    "ir": "B13/TBB",          # 赤外画像 (infrared) -- usable day and night
    "strengthen": "SND/ETC",  # 雲頂強調画像 (cloud-top enhanced)
}


def himawari_latest_basetime(area: str = "jp") -> str:
    """Most recent basetime (JST, "YYYYMMDDHHMMSS") with imagery available
    for `area` ("jp" updates every ~2.5-10 minutes). No persistent cache
    (mirrors fetch_jma_weather_map()'s own no-cache stance) -- call
    sparingly, not in a loop; a caller doing several fetches for the same
    moment should call this once and pass the result as basetime= to
    fetch_himawari_tile()/fetch_himawari_image() rather than calling this
    again for each."""
    resp = get_with_retry(f"{HIMAWARI_BASE_URL}/targetTimes_{area}.json", params=None)
    return max(t["basetime"] for t in resp.json())


def latlon_to_tile_xy(lat: float, lon: float, zoom: int = HIMAWARI_ZOOM) -> tuple:
    """Standard Web Mercator slippy-map tile (x, y) containing (lat, lon) at
    `zoom` -- the same scheme Leaflet/OSM/JMA's own map use, and what the
    tile URL's {x}/{y} are."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def fetch_himawari_tile(x: int, y: int, imgtype: str = "ir", basetime: str = None) -> bytes:
    """Raw JPEG bytes of one z=6 "jp"-area himawari tile at slippy-map
    coordinate (x, y) -- see latlon_to_tile_xy() to get (x, y) from a
    lat/lon. basetime defaults to the latest available
    (himawari_latest_basetime()); pass an explicit one (itself from
    himawari_latest_basetime(), or a "basetime" value out of
    targetTimes_jp.json) to look at a specific past moment within JMA's own
    retention window."""
    if imgtype not in HIMAWARI_IMG_TYPES:
        raise ValueError(f"imgtype must be one of {list(HIMAWARI_IMG_TYPES)}, got {imgtype!r}")
    basetime = basetime or himawari_latest_basetime("jp")
    path_code = HIMAWARI_IMG_TYPES[imgtype]
    url = f"{HIMAWARI_BASE_URL}/{basetime}/jp/{basetime}/{path_code}/{HIMAWARI_ZOOM}/{x}/{y}.jpg"
    resp = get_with_retry(url, params=None)
    return resp.content


def fetch_himawari_image(lat: float, lon: float, imgtype: str = "ir", basetime: str = None,
                          tile_span: int = 3, out_path: str = None) -> dict:
    """Small mosaic of tile_span x tile_span himawari tiles (default 3x3,
    768x768px) centered on (lat, lon), with a red crosshair marking that
    exact point, saved as a JPEG. tile_span must be odd, so the target
    point's own tile lands in the mosaic's center.

    Each z=6 tile spans roughly 5.6 degrees (see banner comment) -- this
    gives regional cloud context around the point ("is there cloud near
    this mountain right now"), not summit-precise imagery. Widen tile_span
    for more surrounding context (e.g. tracking an approaching system), or
    leave at the default for a tighter crop.

    Requires Pillow (lazy import, same convention as
    render_route_weather_map()'s own PIL dependency in
    mountain_weather_detail.py -- not part of this project's core
    `pip install requests` dependency; see SKILL.md).

    Returns {"path": saved JPEG path, "basetime": the JST
    "YYYYMMDDHHMMSS" actually used, "attribution": HIMAWARI_ATTRIBUTION}
    -- keep that attribution string alongside the image wherever it's
    shown/shared (see banner comment's 利用規約 note)."""
    from PIL import Image, ImageDraw

    if tile_span % 2 == 0:
        raise ValueError(f"tile_span must be odd, got {tile_span}")

    basetime = basetime or himawari_latest_basetime("jp")
    cx, cy = latlon_to_tile_xy(lat, lon)
    half = tile_span // 2

    mosaic = Image.new("RGB", (tile_span * HIMAWARI_TILE_PX, tile_span * HIMAWARI_TILE_PX))
    for row, ty in enumerate(range(cy - half, cy + half + 1)):
        for col, tx in enumerate(range(cx - half, cx + half + 1)):
            try:
                tile_bytes = fetch_himawari_tile(tx, ty, imgtype=imgtype, basetime=basetime)
                tile_img = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
            except requests.exceptions.RequestException:
                # Off the edge of JMA's jp tile grid, or a transient error --
                # fall back to a flat gray tile rather than failing the
                # whole mosaic over one missing corner.
                tile_img = Image.new("RGB", (HIMAWARI_TILE_PX, HIMAWARI_TILE_PX), (32, 32, 32))
            mosaic.paste(tile_img, (col * HIMAWARI_TILE_PX, row * HIMAWARI_TILE_PX))

    # Sub-tile pixel position of (lat, lon) within the mosaic -- same
    # projection as latlon_to_tile_xy(), kept in float instead of floored to
    # the tile index, so the crosshair lands on the exact point rather than
    # just its tile's corner.
    n = 2 ** HIMAWARI_ZOOM
    lat_rad = math.radians(lat)
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    px = (fx - (cx - half)) * HIMAWARI_TILE_PX
    py = (fy - (cy - half)) * HIMAWARI_TILE_PX

    draw = ImageDraw.Draw(mosaic)
    r = 10
    draw.ellipse([px - r, py - r, px + r, py + r], outline=(255, 0, 0), width=3)

    out_path = os.path.abspath(out_path or os.path.join(HIMAWARI_OUT_DIR, f"himawari_{imgtype}_{basetime}.jpg"))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    mosaic.save(out_path, "JPEG", quality=90)

    return {"path": out_path, "basetime": basetime, "attribution": HIMAWARI_ATTRIBUTION}
