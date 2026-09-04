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

import json
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
# REVERTED a widened list (975/950/900/800hPa added) that looked like a pure
# accuracy win for the fixed-band altitude mapping -- it was NOT. Confirmed
# live: ecmwf_ifs025 (the fallback model both scripts merge in wherever
# jma_msm has no data -- i.e. essentially all of the 4+ day range) returns
# null for windspeed/temperature/cloudcover at 975/950/900/800hPa; only
# jma_msm's short (~2-3 day) range supports them. The merge -> safe_avg
# pipeline silently treats that null as 0.0, which silently corrupted
# wind/temp/cloud (0.0 wind = no penalty, 0.0 cloud = perfect) for most of
# the forecast horizon. Only 1000/925/850/700/600/500/... are confirmed
# valid on ecmwf_ifs025 -- do not add intermediate levels back without
# re-verifying ecmwf_ifs025 (not just best_match) actually returns non-null
# data for them.


def pressure_level_altitude_m(hpa: int) -> float:
    """Standard-atmosphere altitude (m) for a given pressure level (hPa)."""
    return 44330 * (1 - (hpa / 1013.25) ** (1 / 5.255))


def nearest_pressure_level(elevation_m: float) -> int:
    """Return the pressure level (hPa) whose standard-atmosphere altitude is
    closest to the given elevation."""
    return min(PRESSURE_LEVELS_HPA, key=lambda hpa: abs(pressure_level_altitude_m(hpa) - elevation_m))


# Fixed altitude bands mvp.py compares every mountain on (so results are
# directly comparable across peaks regardless of each one's own elevation);
# detail.py also uses these same 3 bands for its hourly cloud/wind display
# table (see that file's BAND_VARS/WIND_SPEED_BAND_VARS usage), separately
# from its own per-mountain nearest-pressure-level logic used for scoring.
FIXED_ALTITUDE_BANDS_M = [1000, 2000, 3000]
ALTITUDE_BAND_HPA = {alt: nearest_pressure_level(alt) for alt in FIXED_ALTITUDE_BANDS_M}
# e.g. {1000: 925, 2000: 850, 3000: 700}
BAND_VARS = {alt: f"cloudcover_{hpa}hPa" for alt, hpa in ALTITUDE_BAND_HPA.items()}
WIND_SPEED_BAND_VARS = {alt: f"windspeed_{hpa}hPa" for alt, hpa in ALTITUDE_BAND_HPA.items()}


def nearest_band(elevation_m: float) -> int:
    """Which of the fixed altitude bands (1000/2000/3000m) is closest to a
    given summit elevation. Peaks above 3000m still map to the 3000m band,
    since that's the highest band tracked."""
    return min(FIXED_ALTITUDE_BANDS_M, key=lambda b: abs(b - elevation_m))


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


def cloud_penalty(ridge_cloud_pct) -> float:
    """Cloud cover *at* the ridge band itself (not a "look up from below"
    composite) -- i.e. are you actually standing in cloud/fog up there.
    Used directly as the 0-100 penalty."""
    return ridge_cloud_pct if ridge_cloud_pct is not None else 0.0


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


def precip_penalty(prob, mm) -> float:
    """Blends probability with intensity so a high-probability drizzle and a
    lower-probability downpour aren't scored the same. mm is the AM-window
    average precip rate; 5mm/h+ is treated as already at max penalty for the
    intensity half."""
    prob = prob or 0.0
    mm = mm or 0.0
    return min(100.0, prob * 0.5 + min(mm / 5.0, 1.0) * 50)


def wet_chill_adjustment_c(precip_prob, precip_mm) -> float:
    """Extra degrees to subtract from wind-chill before temp_penalty(),
    when AM-window precipitation is both likely and non-trivial.
    Hypothermia progresses fastest under a low-temp + wind + WET
    combination, but wind_chill_c() only models the dry (still-air-
    adjusted) component -- this is a deliberately simple single-step
    correction (not a continuous ramp) rather than a second full scoring
    axis; threshold and magnitude are a judgment call, easy to retune."""
    prob = precip_prob or 0.0
    mm = precip_mm or 0.0
    if prob >= 50.0 and mm >= 0.5:
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


# Weights: thunderstorm and wind are the two hazards that actually turn a
# hike into an emergency, so they carry the most weight. Ridge cloud cover
# and ridge visibility both cover view/whiteout risk and correlate
# strongly, so their combined budget is unchanged from the old cloud-only
# 20% -- just split 12/8 between them, since visibility is the more direct
# whiteout signal but cloud% still catches band-level cloud cover
# visibility itself doesn't measure. Precip and wind-chill unchanged.
SCORE_WEIGHTS = {"thunder": 0.30, "wind": 0.25, "cloud": 0.12, "visibility": 0.08,
                  "precip": 0.15, "temp": 0.10}


def mountain_climb_score(*, ridge_wind_ms, ridge_cloud_pct, ridge_visibility_m, chill_c,
                          am_precip_prob, am_precip_mm, pm_cape) -> float:
    """Climbing-oriented score: 100 minus a weighted blend of thunderstorm
    risk (PM CAPE), ridge wind, ridge cloud cover, ridge visibility, AM
    precipitation, and ridge wind-chill (wet-adjusted -- see
    wet_chill_adjustment_c). See SCORE_WEIGHTS and the penalty_* functions
    above.

    Takes plain scalars (not a windows/elevation structure) so the exact
    same function serves both mvp.py (fixed 1000/2000/3000m bands shared
    across all 75 mountains) and detail.py (the selected mountain's own
    exact pressure level) -- only how each script *derives* these inputs
    differs; the scoring itself is shared here."""
    wet_chill_c = None if chill_c is None else chill_c + wet_chill_adjustment_c(am_precip_prob, am_precip_mm)
    penalties = {
        "thunder": thunder_penalty(pm_cape),
        "wind": wind_penalty(ridge_wind_ms),
        "cloud": cloud_penalty(ridge_cloud_pct),
        "visibility": visibility_penalty(ridge_visibility_m),
        "precip": precip_penalty(am_precip_prob, am_precip_mm),
        "temp": temp_penalty(wet_chill_c),
    }
    score = 100 - sum(SCORE_WEIGHTS[k] * penalties[k] for k in SCORE_WEIGHTS)
    return max(0.0, round(score, 1))


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
        merged = cached
        merged.setdefault("hourly", {}).update(fresh.get("hourly", {}))
        if fresh.get("daily"):
            merged.setdefault("daily", {}).update(fresh["daily"])
    else:
        merged = fresh
        merged.setdefault("hourly", {})
        merged.setdefault("daily", {})

    merged["_fetched_at"] = time_module.time()
    _save_cache(path, merged)
    return merged
