"""
Mountain Weather Dashboard - Single-Mountain Detail script
------------------------------------------------------------
A different view on the same data as mountain_weather_mvp.py: instead of
ranking many mountains for a given day, this shows the full 14-day outlook
for ONE mountain you pick from a list at startup.

Same underlying data/scoring as mountain_weather_mvp.py -- both scripts now
share their mountain pool, cache layer, and scoring formula via
mountain_weather_core.py (see that module's docstring for exactly what's
shared vs. kept separate). If you tune the scoring, remember to check
whether the change belongs in core.py (shared) or here (detail.py-only).

Requirements:
    pip install requests

Usage:
    python mountain_weather_detail.py
"""

import math
import re
import statistics
import time as time_module
import os
import requests
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from mountain_weather_core import (
    MOUNTAINS, pad,
    pressure_level_altitude_m, nearest_pressure_level,
    FIXED_ALTITUDE_BANDS_M, ALTITUDE_BAND_HPA, BAND_VARS, WIND_SPEED_BAND_VARS,
    KMH_TO_MS, wind_chill_c, safe_avg, safe_avg_or_none,
    SCORE_WEIGHTS, mountain_climb_score, mountain_hazards,
    format_date_with_weekday,
    PRECIP_TIMING_UNCERTAIN_DAYS_OUT, PRECIP_TIMING_BOUNDARY_LOW,
    PRECIP_TIMING_BOUNDARY_HIGH, PRECIP_TIMING_NOTE,
    get_with_retry, CACHE_DIR, CACHE_TTL_SECONDS, _load_cache, _save_cache,
    fetch_open_meteo,
    WET_HOUR_PRECIP_THRESHOLD_PCT, WET_HOUR_MSM_PRECIP_MM, WET_HOUR_RH_PCT, WET_HOUR_MOIST_CLOUD_PCT,
    MSM_PRECIP_VAR, wet_fraction, any_layer_moist, slope_pressure_level,
    sustained_peak, PM_CLOUD_PERSIST_HOURS, PRECIP_FULL_PENALTY_TOTAL_MM,
)

# ---------------------------------------------------------------------------
# How many days ahead to show for the selected mountain
# ---------------------------------------------------------------------------
FORECAST_DAYS = 14

# ---------------------------------------------------------------------------
# Pressure levels / fixed altitude bands: shared with mountain_weather_mvp.py,
# see mountain_weather_core.py (imported above). detail.py-only extras below
# (ALTITUDE_BAND_ACTUAL_M/band_altitude_label/HUMIDITY_VARS/WIND_DIR_BAND_VARS)
# build on the shared FIXED_ALTITUDE_BANDS_M/ALTITUDE_BAND_HPA.
# ---------------------------------------------------------------------------
# The pressure levels nearest 1000/2000/3000m in standard atmosphere are
# 925/850/700hPa (see core.PRESSURE_LEVELS_HPA -- 900/800hPa aren't in that
# list because ecmwf_ifs025 returns null for them). Their *actual*
# standard-atmosphere altitudes deviate from the nominal 1000/2000/3000m
# labels non-trivially -- 925hPa~=762m (-238m), 850hPa~=1458m (-542m),
# 700hPa~=3013m (+13m) -- so "2000m帯" in particular is really closer to
# 1460m. Kept here so the hourly table's subtitle can show the real figure
# instead of silently using the nominal one.
ALTITUDE_BAND_ACTUAL_M = {alt: round(pressure_level_altitude_m(hpa)) for alt, hpa in ALTITUDE_BAND_HPA.items()}


def band_altitude_label(alt: int) -> str:
    """Display label for one fixed band, e.g. '約1458m' -- uses the band's
    real standard-atmosphere altitude (ALTITUDE_BAND_ACTUAL_M), not the
    nominal 1000/2000/3000m name used internally as its dict key, since the
    two diverge non-trivially (see ALTITUDE_BAND_ACTUAL_M's comment)."""
    return f"約{ALTITUDE_BAND_ACTUAL_M[alt]}m"
# Relative humidity at each band's pressure level -- a proxy for whether
# that layer is actually saturated (near 100%) vs. just partly cloudy.
# Combined with surface precipitation_probability, high humidity at a band
# + high surface precip suggests that band is likely getting wet too.
HUMIDITY_VARS = {alt: f"relative_humidity_{hpa}hPa" for alt, hpa in ALTITUDE_BAND_HPA.items()}
# Wind speed (WIND_SPEED_BAND_VARS, shared -- imported from core) and
# direction at each of the same fixed altitude bands, so the hourly memo
# can report which band has the strongest wind that hour. WIND_DIR_BAND_VARS
# is detail.py-only (mvp.py's ranking table doesn't show wind direction).
WIND_DIR_BAND_VARS = {alt: f"winddirection_{hpa}hPa" for alt, hpa in ALTITUDE_BAND_HPA.items()}

# Freezing level (0°C altitude) and, at the pressure level nearest the
# *selected* mountain's actual elevation (not the fixed 1000/2000/3000m
# bands above), wind speed/direction -- used to estimate a rough
# "feels like" condition for the ridge/summit period.
FREEZING_LEVEL_VAR = "freezing_level_height"

# Visibility (m) hits the same models= restriction as freezing_level_height
# above -- jma_msm/ecmwf_ifs025 return null for it, only best_match
# (model=None) has real data -- so it's fetched the same way, via
# fetch_visibility() below.
VISIBILITY_VAR = "visibility"


def wind_vars_for_elevation(elevation_m: float):
    """Pressure level (hPa) nearest the given elevation, plus the
    corresponding wind speed/direction hourly variable names."""
    hpa = nearest_pressure_level(elevation_m)
    return hpa, f"windspeed_{hpa}hPa", f"winddirection_{hpa}hPa"


def moisture_vars_for_summit(summit_hpa: int) -> list:
    """Hourly variable names the moisture rule of core.is_wet_hour() needs
    for a mountain whose own level is summit_hpa: RH + cloud at that level
    and at the slope level below it (core.slope_pressure_level). Pass these
    as extra_vars to fetch_forecast() -- the fixed 925/850/700hPa cloud/RH
    are always fetched anyway, so this only adds anything for summits
    outside those (e.g. 富士山's 600hPa)."""
    levels = [summit_hpa] + ([slope_pressure_level(summit_hpa)] if slope_pressure_level(summit_hpa) else [])
    return [f"{kind}_{h}hPa" for h in levels for kind in ("relative_humidity", "cloudcover")]


def temp_var_for_elevation(elevation_m: float) -> str:
    """temperature_XXXhPa variable name for the pressure level nearest the
    given elevation -- same source mountain_weather_mvp.py uses for its
    fixed 1000/2000/3000m bands. Confirmed (2026-09, cross-checked against
    てんきとくらす's 700hPa figures for 日光白根山) to return real,
    non-null values on the named jma_msm/ecmwf_ifs025 models -- unlike
    freezing_level_height/visibility, it does NOT need the model=None
    (best_match) workaround. Previously this file instead derived a rough
    estimate from freezing_level_height + a standard lapse rate
    (estimate_temp_c(), now removed) because temperature_XXXhPa was assumed
    unavailable; that assumption was never actually tested and the estimate
    ran 3-5°C warmer than both てんきとくらす and this direct fetch during
    a same-day comparison. Switched to match mvp.py's approach."""
    return f"temperature_{nearest_pressure_level(elevation_m)}hPa"


COMPASS_JA = ["北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
              "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西"]


def compass_label(deg) -> str:
    if deg is None:
        return "-"
    idx = int((deg + 11.25) // 22.5) % 16
    return COMPASS_JA[idx]


def feels_like_label(chill_c) -> str:
    if chill_c is None:
        return "-"
    if chill_c <= -15:
        return "厳冬・凍傷リスク大"
    elif chill_c <= -5:
        return "厳しい寒さ"
    elif chill_c <= 0:
        return "肌寒い"
    elif chill_c <= 10:
        return "やや涼しい"
    else:
        return "穏やか"


# ---------------------------------------------------------------------------
# Climbing-oriented score: shared with mountain_weather_mvp.py via
# mountain_weather_core.py (penalty_* functions, SCORE_WEIGHTS,
# mountain_climb_score, imported above) -- only the *inputs* fed into
# mountain_climb_score differ (this file derives them from the selected
# mountain's own exact elevation/pressure level, mvp.py from fixed
# 1000/2000/3000m bands shared across all 75 mountains).
# ---------------------------------------------------------------------------


# Displayed window per day: sunrise + EARLY_START_OFFSET_HOURS -> sunset,
# broken out hour-by-hour (rather than averaged into AM/PM/ridge buckets).
# EARLY_START_OFFSET_HOURS covers early starts like a 4:00 departure on a
# ~5:16 sunrise day (-1 = start 1 hour before sunrise).
EARLY_START_OFFSET_HOURS = -1

HOURLY_VARS = ["cloudcover", "cloudcover_low", "cloudcover_mid",
               "cloudcover_high", "precipitation_probability",
               # Raw surface values -- kept alongside the derived score/estimate
               # fields specifically so a forecast hour can be checked directly
               # against a Windy/SCW screenshot for the same time (Windy shows
               # actual precip mm and 2m temp; SCW's cloud map is the same
               # total-column cloudcover metric as our "cloud" field above).
               "precipitation", "temperature_2m", "windspeed_10m", "winddirection_10m",
               # cape: convective available potential energy, used for the
               # PM-window thunderstorm-risk penalty (see mountain_climb_score).
               "cape"]

# Three evaluation windows per day, shared with mountain_weather_mvp.py's
# design (see that file's comment for the rationale). EARLY_START_OFFSET_HOURS
# above controls the *display* window for the hourly table (sunrise-1h to
# sunset); these three separately control the *scoring* windows fed into
# mountain_climb_score. TRIP_START_OFFSET_HOURS happens to equal
# EARLY_START_OFFSET_HOURS today but the two are conceptually independent --
# don't assume changing one changes the other.
TRIP_START_OFFSET_HOURS = -1
PM_START_HOUR = 12

# Ridge-dwell window (2026-09 redesign, sunrise-relative -- mirrors mvp.py's
# constants of the same name, see that file for the full rationale). Replaces
# the old fixed RIDGE_START_HOUR=8/RIDGE_END_HOUR=11 clock hours, which gave
# only 3 hourly forecast points and, being unrelated to sunrise, drifted out
# of sync with when a climber is actually near the ridge/summit as day length
# changes across the season.
#
# Built from the user's own "main climbing time" framing:
# 「日の出から14時ころまでが登山のメインタイム」, trimmed by
# RIDGE_DWELL_TRIM_HOURS on each end (excludes the still-climbing-up start
# and the already-descending end):
#   main_start = sunrise + TRIP_START_OFFSET_HOURS
#   main_end   = MAIN_TIME_END_HOUR (fixed clock hour -- deliberately NOT
#                sunset-relative like the activity/PM windows, which were
#                widened for a different job: keep catching afternoon/evening
#                hazards, not "when is the view worth having")
#   ridge_start = main_start + RIDGE_DWELL_TRIM_HOURS
#   ridge_end   = main_end - RIDGE_DWELL_TRIM_HOURS
# ~sunrise+1h through a fixed 12:00 with RIDGE_DWELL_TRIM_HOURS=2 -- about
# 5-6 hours/hourly points in September, versus the old window's fixed 3.
MAIN_TIME_END_HOUR = 14
RIDGE_DWELL_TRIM_HOURS = 2

# Activity/PM window end line (2026-09 redesign, sunset-based -- mirrors
# mvp.py's constant of the same name, see that file for the full rationale).
# Replaces the old fixed ACTIVITY_END_HOUR=19 (precip window)/PM_END_HOUR=17
# (CAPE/cloud window) clock hours, which were wrong by 2+ hours at the
# solstices (9月の日没は18時前後・12月は16時半ごろ・7月は19時近く) -- a 19時
# cutoff in December kept scoring well after dark as "activity time," and a
# 17時 cutoff in July would clip real danger hours the same way the original
# 唐松岳9/6 incident did (15-18時 rain, sunset ~18時 in September). The two
# former end lines are now ONE (PM_END_HOUR and ACTIVITY_END_HOUR never had a
# real reason to differ -- both were "roughly when the climbing day ends,"
# just approximated with different round numbers), measured from this file's
# own `_daily_sunset` (already fetched below, unlike mvp.py which needed a
# new sunset fetch added for this same redesign). +30min accounts for a
# descent already underway continuing into civil twilight rather than
# snapping to "activity" at the instant of sunset -- a starting judgment
# call, not a fitted value, easy to retune.
ACTIVITY_END_GRACE_MINUTES = 30
# WET_HOUR_PRECIP_THRESHOLD_PCT / WET_HOUR_MSM_PRECIP_MM / is_wet_hour(): the
# per-hour wet judgment now lives in mountain_weather_core.py (2026-09-09,
# MSM-first redesign -- see the banner comment there).

# get_with_retry/REQUEST_TIMEOUT/MAX_RETRIES and the raw-JSON response cache
# (CACHE_DIR/CACHE_TTL_SECONDS/_load_cache/_save_cache/fetch_open_meteo) are
# shared with mountain_weather_mvp.py via mountain_weather_core.py (imported
# above). _cache_path() itself is not imported/used directly by this file --
# fetch_open_meteo() calls it internally -- but fetch_ensemble() below reuses
# _load_cache/_save_cache/get_with_retry/CACHE_DIR/CACHE_TTL_SECONDS for its
# own, deliberately separate cache file naming scheme (see that section).


def fetch_single_model(lat: float, lon: float, model: str, days: int, with_sunrise: bool = False,
                        extra_vars: list = None) -> dict:
    hourly_vars = HOURLY_VARS + list(BAND_VARS.values()) + list(HUMIDITY_VARS.values())
    if extra_vars:
        hourly_vars = hourly_vars + extra_vars
    daily_vars = ["sunrise", "sunset"] if with_sunrise else None
    return fetch_open_meteo(lat, lon, hourly=hourly_vars, daily=daily_vars, days=days, model=model)


def fetch_freezing_level(lat: float, lon: float, days: int) -> dict:
    """Freezing level height isn't served for the named jma_msm/ecmwf_ifs025
    models (comes back all-null) -- only for Open-Meteo's blended
    'best_match', so it's fetched separately (model=None) and merged in by
    time."""
    return fetch_open_meteo(lat, lon, hourly=[FREEZING_LEVEL_VAR], days=days, model=None)


def fetch_visibility(lat: float, lon: float, days: int) -> dict:
    """Visibility hits the same models= restriction as freezing_level_height
    above (all-null on jma_msm/ecmwf_ifs025, only served on best_match), so
    it's fetched the same way -- separately, with model=None, merged in by
    time."""
    return fetch_open_meteo(lat, lon, hourly=[VISIBILITY_VAR], days=days, model=None)


def fetch_forecast(lat: float, lon: float, days: int = FORECAST_DAYS, extra_vars: list = None) -> dict:
    """Merge JMA MSM (preferred, where available) with ECMWF IFS 0.25°
    (fallback for hours MSM doesn't cover). Also fetches daily sunrise
    (astronomical, not model-dependent) from the ECMWF call, and freezing
    level height + visibility from separate best_match calls."""
    all_vars = HOURLY_VARS + list(BAND_VARS.values()) + list(HUMIDITY_VARS.values())
    if extra_vars:
        all_vars = all_vars + extra_vars

    msm = fetch_single_model(lat, lon, "jma_msm", days, extra_vars=extra_vars)
    fallback = fetch_single_model(lat, lon, "ecmwf_ifs025", days, with_sunrise=True, extra_vars=extra_vars)
    freezing = fetch_freezing_level(lat, lon, days)
    visibility = fetch_visibility(lat, lon, days)

    fallback_index = {t: i for i, t in enumerate(fallback["hourly"]["time"])}
    freezing_index = {t: i for i, t in enumerate(freezing["hourly"]["time"])}
    visibility_index = {t: i for i, t in enumerate(visibility["hourly"]["time"])}
    merged_hourly = {"time": msm["hourly"]["time"]}
    for var in all_vars:
        merged_values = []
        msm_values = msm["hourly"].get(var, [])
        fb_values = fallback["hourly"].get(var, [])
        for i, t in enumerate(msm["hourly"]["time"]):
            v = msm_values[i] if i < len(msm_values) else None
            if v is None:
                j = fallback_index.get(t)
                v = fb_values[j] if j is not None and j < len(fb_values) else None
            merged_values.append(v)
        merged_hourly[var] = merged_values

    freezing_series = freezing["hourly"].get(FREEZING_LEVEL_VAR, [])
    merged_hourly[FREEZING_LEVEL_VAR] = [
        freezing_series[j] if (j := freezing_index.get(t)) is not None and j < len(freezing_series) else None
        for t in msm["hourly"]["time"]
    ]

    visibility_series = visibility["hourly"].get(VISIBILITY_VAR, [])
    merged_hourly[VISIBILITY_VAR] = [
        visibility_series[j] if (j := visibility_index.get(t)) is not None and j < len(visibility_series) else None
        for t in msm["hourly"]["time"]
    ]

    # Raw MSM precipitation, un-merged (None beyond MSM's ~3-day range) --
    # the MSM-first wet-hour judgment (core.is_wet_hour) needs to know
    # which hours MSM actually covered, which the merged "precipitation"
    # column above can no longer tell it.
    merged_hourly[MSM_PRECIP_VAR] = list(msm["hourly"].get("precipitation", []))

    daily_sunrise = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunrise"]))
    daily_sunset = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunset"]))
    return {"hourly": merged_hourly, "_daily_sunrise": daily_sunrise, "_daily_sunset": daily_sunset}


def compute_day_scores(forecast: dict, wind_speed_var: str, summit_hpa: int, temp_var: str) -> dict:
    """Per-day mountain_climb_score for the selected mountain, using the same
    three windows as mountain_weather_mvp.py (AM/ridge/PM -- see that file's
    comment for the rationale) but this file's own per-mountain-exact inputs:
    wind_speed_var and temp_var (nearest pressure level to the mountain's
    real elevation, from wind_vars_for_elevation/temp_var_for_elevation) and
    summit_hpa's own cloud cover (not mvp.py's fixed-band lookup composite).

    Returns {date_str: {"score": ..., "ridge_wind_ms": ..., "cape": ...,
    "cloud_pct": ..., "precip_wet_pct": ..., "chill": ..., "day_peak_wind_ms":
    ..., "day_peak_wind_hour": ..., "hazards": ..., "day_total_precip_mm":
    ...}}. day_peak_wind_ms is the highest wind speed (at wind_speed_var's
    pressure level) anywhere in that day's sunrise+EARLY_START_OFFSET_HOURS..
    sunset span -- the same span print_hourly_table() displays -- not just
    the scored 8-11 ridge window. hazards (mountain_hazards()'s return value,
    2026-09) judges wind on this day-wide peak rather than the 8-11 ridge
    average, since a gust is a threshold hazard an average can smooth away --
    this replaced an earlier ridge-window-only "wind_warning" flag that only
    caught gusts outside the window (a peak inside it used to already be
    reflected in ridge_wind_ms/the score; now that wind isn't scored at all,
    it needs to show up here regardless of which window it falls in).

    day_total_precip_mm (2026-09 addition) is the full calendar day's
    (00:00-23:59 local) summed precipitation -- deliberately NOT windowed
    to AM/PM/ridge like everything else here, kept as an unweighted display
    figure alongside the score.

    cloud_pct (2026-09 widened) feeds the score itself via
    mountain_climb_score, and is the *worse* of the ridge-dwell window and
    the PM descent window rather than the ridge window alone -- confirmed
    against a real incident: 唐松岳 9/6 scored 100 because the AM/ridge
    windows were dry and clear while rain moved in from midday through the
    descent, since PM previously fed only CAPE into the score.

    precip_wet_pct/precip_mm (2026-09, second pass) replace what used to be
    a similar AM/PM-max probability with a duration-based measure instead:
    the percentage of the activity_by_day window's (climb start through
    sunset+ACTIVITY_END_GRACE_MINUTES -- see that constant's comment) HOURS
    that are "wet" (per core.wet_hour_weight(): MSM data inside its range,
    ECMWF probability/100 as an expected value beyond it -- 2026-09-09),
    and that window's TOTAL mm (2026-09-09; was the peak mm/h -- see
    precip_penalty's docstring in core.py). A day that's rainy for most of the window
    scores high even without a single 100% hour; a single passing-shower
    hour among many dry ones scores low even if that hour spiked -- matching
    how a climber actually experiences "was today wet" (confirmed against
    real hiking reports for 槍ヶ岳9/5 and 立山9/5: both had one isolated
    high-probability hour and were reported as good days, which the earlier
    peak-based version scored down as if the rain had been sustained). This
    still feeds directly into the score (not just a side-column like
    day_total_precip_mm above), because a reader relying on the score to
    decide whether to go needs the activity-window risk reflected there."""
    times = forecast["hourly"]["time"]
    precip_prob_series = forecast["hourly"]["precipitation_probability"]
    precip_mm_series = forecast["hourly"]["precipitation"]
    # .get(): scratch_past_date.py-style model=None forecasts have no MSM
    # column, in which case every hour falls back to the probability rule.
    msm_mm_series = forecast["hourly"].get(MSM_PRECIP_VAR) or [None] * len(times)
    # Moisture rule layers (core.is_wet_hour()): summit level + slope level.
    # .get(): missing columns just mean that layer can't vote.
    slope_hpa = slope_pressure_level(summit_hpa) if summit_hpa else None
    moist_layer_series = []
    for hpa in [h for h in (summit_hpa, slope_hpa) if h]:
        moist_layer_series.append((forecast["hourly"].get(f"relative_humidity_{hpa}hPa") or [None] * len(times),
                                   forecast["hourly"].get(f"cloudcover_{hpa}hPa") or [None] * len(times)))
    cape_series = forecast["hourly"]["cape"]
    wind_speed_series = forecast["hourly"].get(wind_speed_var)
    summit_cloud_series = forecast["hourly"].get(f"cloudcover_{summit_hpa}hPa") if summit_hpa else None
    temp_series = forecast["hourly"].get(temp_var)
    visibility_series = forecast["hourly"].get(VISIBILITY_VAR)
    daily_sunrise = forecast["_daily_sunrise"]
    daily_sunset = forecast["_daily_sunset"]

    activity_by_day, ridge_by_day, pm_by_day, day_by_day = {}, {}, {}, {}
    day_precip_by_day = {}
    for idx, t in enumerate(times):
        day_str = t.split("T")[0]
        t_dt = datetime.fromisoformat(t)

        # Unconditional (no window gate) -- every hour of the calendar day,
        # unlike activity_by_day/ridge_by_day/pm_by_day below.
        if precip_mm_series[idx] is not None:
            day_precip_by_day[day_str] = day_precip_by_day.get(day_str, 0.0) + precip_mm_series[idx]

        # activity_end (2026-09 redesign): sunset + ACTIVITY_END_GRACE_MINUTES,
        # the single end line that replaced the old fixed ACTIVITY_END_HOUR
        # (precip)/PM_END_HOUR (CAPE/cloud) clock hours -- see that
        # constant's comment. Independent of day_by_day's own wind-peak
        # window just below, which already used actual sunset (no grace)
        # with EARLY_START_OFFSET_HOURS and is unchanged here.
        sunrise_iso = daily_sunrise.get(day_str)
        sunset_iso = daily_sunset.get(day_str)
        activity_end = (
            datetime.fromisoformat(sunset_iso) + timedelta(minutes=ACTIVITY_END_GRACE_MINUTES)
            if sunset_iso is not None else None
        )

        # main_start (2026-09 redesign): also the ridge-dwell window's own
        # anchor -- see MAIN_TIME_END_HOUR/RIDGE_DWELL_TRIM_HOURS's comment.
        # Computed here (needs only sunrise, not sunset) so the ridge window
        # below doesn't depend on sunset data being present.
        main_start = (
            datetime.fromisoformat(sunrise_iso) + timedelta(hours=TRIP_START_OFFSET_HOURS)
            if sunrise_iso is not None else None
        )

        if main_start is not None and activity_end is not None:
            if main_start <= t_dt < activity_end:
                e = activity_by_day.setdefault(day_str, {"precip_prob": [], "precip_mm": [], "msm_mm": [], "moist": []})
                e["precip_prob"].append(precip_prob_series[idx])
                e["precip_mm"].append(precip_mm_series[idx])
                e["msm_mm"].append(msm_mm_series[idx])
                e["moist"].append(any_layer_moist(*[(rh[idx], c[idx]) for rh, c in moist_layer_series]))

        if sunrise_iso is not None and sunset_iso is not None:
            day_start = datetime.fromisoformat(sunrise_iso) + timedelta(hours=EARLY_START_OFFSET_HOURS)
            day_end = datetime.fromisoformat(sunset_iso)
            if day_start <= t_dt < day_end and wind_speed_series and wind_speed_series[idx] is not None:
                day_by_day.setdefault(day_str, []).append((wind_speed_series[idx], t_dt.hour))

        if main_start is not None:
            main_end = t_dt.replace(hour=MAIN_TIME_END_HOUR, minute=0, second=0, microsecond=0)
            ridge_start = main_start + timedelta(hours=RIDGE_DWELL_TRIM_HOURS)
            ridge_end = main_end - timedelta(hours=RIDGE_DWELL_TRIM_HOURS)
            if ridge_start <= t_dt < ridge_end:
                e = ridge_by_day.setdefault(day_str, {"wind_kmh": [], "cloud": [], "temp": [], "visibility": []})
                e["wind_kmh"].append(wind_speed_series[idx] if wind_speed_series else None)
                e["cloud"].append(summit_cloud_series[idx] if summit_cloud_series else None)
                e["temp"].append(temp_series[idx] if temp_series else None)
                e["visibility"].append(visibility_series[idx] if visibility_series else None)

        if activity_end is not None and PM_START_HOUR <= t_dt.hour and t_dt < activity_end:
            e = pm_by_day.setdefault(day_str, {"cape": [], "cloud": []})
            e["cape"].append(cape_series[idx])
            e["cloud"].append(summit_cloud_series[idx] if summit_cloud_series else None)

    scores = {}
    for day_str in sorted(set(activity_by_day) | set(ridge_by_day) | set(pm_by_day)):
        activity = activity_by_day.get(day_str, {"precip_prob": [], "precip_mm": [], "msm_mm": [], "moist": []})
        ridge = ridge_by_day.get(day_str, {"wind_kmh": [], "cloud": [], "temp": [], "visibility": []})
        pm = pm_by_day.get(day_str, {"cape": [], "cloud": []})

        # Duration-based precip (2026-09, second pass -- see this function's
        # docstring): fraction of the activity window's hours that are
        # "wet" (>=WET_HOUR_PRECIP_THRESHOLD_PCT), not a single averaged/
        # peaked probability. A single passing-shower hour (通り雨) among
        # many dry ones scores a low fraction; sustained rain through most
        # of the window (終日雨) scores a high one, even with the same peak
        # hourly probability -- confirmed against 槍ヶ岳9/5 and 立山9/5, both
        # reported as good days despite one isolated high-probability hour.
        # MSM-first (2026-09-09): inside MSM's range an hour is wet on MSM's
        # own 5km precipitation (>=WET_HOUR_MSM_PRECIP_MM) or the moisture
        # rule; beyond it, the ECMWF probability/100 is the hour's expected
        # wetness (so the fraction is an expected value, not a 50% cut). See
        # core.wet_hour_weight()'s banner comment for the rule and why.
        # precip_mm (2026-09-09) is the window's summed precipitation (was
        # the single-hour peak -- see precip_penalty's docstring in core.py).
        activity_mm = [v for v in activity["precip_mm"] if v is not None]
        wf = wet_fraction(activity["precip_prob"], activity["msm_mm"], activity["moist"])
        wet_hours = wf["wet_hours"]
        precip_wet_pct = wf["wet_fraction_pct"]
        precip_mm = round(sum(activity_mm), 1) if activity_mm else 0.0

        # CAPE keeps the window's single-hour PEAK (a threshold hazard can
        # hide behind a mild average). Cloud (2026-09-09) uses the SUSTAINED
        # peak -- the highest PM_CLOUD_PERSIST_HOURS-hour mean -- so one
        # interpolated 100% hour no longer zeroes the day while a real
        # multi-hour cloud-up still counts in full (see PM_CLOUD_PERSIST_HOURS'
        # comment in core.py). Precip used to be peaked here too, until the
        # 2026-09 duration-based redesign above moved it to the activity
        # window (climb start through sunset+ACTIVITY_END_GRACE_MINUTES).
        ridge_wind_kmh = safe_avg(ridge["wind_kmh"])
        ridge_cloud = safe_avg(ridge["cloud"])
        pm_cloud_sustained = sustained_peak(pm["cloud"])
        pm_cloud = pm_cloud_sustained if pm_cloud_sustained is not None else 0.0
        ridge_temp = safe_avg(ridge["temp"])
        ridge_visibility = safe_avg_or_none(ridge["visibility"])
        cape_clean = [v for v in pm["cape"] if v is not None]
        pm_cape = max(cape_clean) if cape_clean else None

        # Worse of ridge-dwell/PM for cloud -- see this function's docstring
        # (2026-09 widening, 唐松岳 9/6 incident). Precip no longer needs an
        # AM/PM max: the activity window above already spans climb through
        # descent in one pass.
        cloud_pct = max(ridge_cloud, pm_cloud)

        ridge_wind_ms = ridge_wind_kmh * KMH_TO_MS
        chill = wind_chill_c(ridge_temp, ridge_wind_kmh)
        score = mountain_climb_score(
            cloud_pct=cloud_pct,
            ridge_visibility_m=ridge_visibility,
            precip_wet_pct=precip_wet_pct,
            precip_mm=precip_mm,
        )

        day_wind_samples = day_by_day.get(day_str, [])
        if day_wind_samples:
            peak_kmh, peak_hour = max(day_wind_samples, key=lambda pair: pair[0])
            day_peak_wind_ms = peak_kmh * KMH_TO_MS
        else:
            day_peak_wind_ms, peak_hour = None, None
        # Thunder/wind no longer feed the score (see SCORE_WEIGHTS' comment
        # in core.py) -- surfaced instead as an explicit hazard level. Wind
        # uses the day's PEAK gust (day_peak_wind_ms, whole active span), not
        # the 8-11 ridge average, since a gust is a threshold hazard an
        # average can miss -- this supersedes the old ridge-window-only
        # wind_warning flag (a peak inside the ridge window now shows up
        # here too, not just ones outside it).
        hazards = mountain_hazards(ridge_wind_ms=day_peak_wind_ms, pm_cape=pm_cape,
                                    chill_c=chill, precip_wet_pct=precip_wet_pct, precip_mm=precip_mm)

        scores[day_str] = {
            "score": score,
            "ridge_wind_ms": round(ridge_wind_ms, 1),
            "cape": pm_cape,
            "cloud_pct": round(cloud_pct, 1),
            "ridge_visibility": round(ridge_visibility) if ridge_visibility is not None else None,
            "precip_wet_pct": round(precip_wet_pct, 1),
            "precip_wet_hours": wet_hours,
            "precip_total_hours": wf["total_hours"],
            "precip_msm_hours": wf["msm_hours"],
            "precip_moist_hours": wf["moist_hours"],
            "precip_prob_hours": wf["prob_hours"],
            "precip_total_mm": precip_mm,
            "chill": round(chill, 1) if chill is not None else None,
            "day_peak_wind_ms": round(day_peak_wind_ms, 1) if day_peak_wind_ms is not None else None,
            "day_peak_wind_hour": peak_hour,
            "hazards": hazards,
            "day_total_precip_mm": round(day_precip_by_day[day_str], 1) if day_str in day_precip_by_day else None,
        }
    return scores


def build_hourly_rows(forecast: dict, wind_speed_var: str = None, wind_dir_var: str = None,
                       summit_hpa: int = None, temp_var: str = None) -> list:
    """One row per hour, for every hour from sunrise+EARLY_START_OFFSET_HOURS
    to sunset on each forecast day -- combining the cloud/precip score with
    the wind/temperature "feels like" estimate, since both are now on the
    same per-hour granularity.

    summit_hpa: pressure level nearest the mountain's *actual* elevation
    (from wind_vars_for_elevation / nearest_pressure_level), used for the
    score's cloud reference instead of the fixed 1000/2000/3000m bands.
    Those fixed bands top out at ~3000m (700hPa) for every mountain, so for
    anything taller -- Fuji at 3776m is ~700m above that -- the capped
    lookup was checking cloud cover well below the real summit. Passing
    summit_hpa fixes that; omitting it falls back to the old capped
    behavior (kept only for scripts written against the old signature).

    temp_var: temperature_XXXhPa variable name for that same pressure level
    (from temp_var_for_elevation), read directly rather than estimated."""
    summit_cloud_var = f"cloudcover_{summit_hpa}hPa" if summit_hpa else None
    summit_alt_m = round(pressure_level_altitude_m(summit_hpa)) if summit_hpa else None

    times = forecast["hourly"]["time"]
    cloud = forecast["hourly"]["cloudcover"]
    cloud_low = forecast["hourly"]["cloudcover_low"]
    cloud_mid = forecast["hourly"]["cloudcover_mid"]
    cloud_high = forecast["hourly"]["cloudcover_high"]
    precip_prob = forecast["hourly"]["precipitation_probability"]
    precip_mm = forecast["hourly"]["precipitation"]
    temp_2m = forecast["hourly"]["temperature_2m"]
    wind10_speed = forecast["hourly"]["windspeed_10m"]
    wind10_dir = forecast["hourly"]["winddirection_10m"]
    band_series = {alt: forecast["hourly"][var] for alt, var in BAND_VARS.items()}
    humidity_series = {alt: forecast["hourly"][var] for alt, var in HUMIDITY_VARS.items()}
    wind_speed_band_series = {alt: forecast["hourly"][var] for alt, var in WIND_SPEED_BAND_VARS.items()}
    wind_dir_band_series = {alt: forecast["hourly"][var] for alt, var in WIND_DIR_BAND_VARS.items()}
    wind_speed_series = forecast["hourly"].get(wind_speed_var) if wind_speed_var else None
    wind_dir_series = forecast["hourly"].get(wind_dir_var) if wind_dir_var else None
    summit_cloud_series = forecast["hourly"].get(summit_cloud_var) if summit_cloud_var else None
    temp_series = forecast["hourly"].get(temp_var) if temp_var else None
    daily_sunrise = forecast["_daily_sunrise"]
    daily_sunset = forecast["_daily_sunset"]

    rows = []
    for idx, t in enumerate(times):
        day_str = t.split("T")[0]
        sunrise_iso = daily_sunrise.get(day_str)
        sunset_iso = daily_sunset.get(day_str)
        if sunrise_iso is None or sunset_iso is None:
            continue
        window_start = datetime.fromisoformat(sunrise_iso) + timedelta(hours=EARLY_START_OFFSET_HOURS)
        window_end = datetime.fromisoformat(sunset_iso)
        t_dt = datetime.fromisoformat(t)
        if not (window_start <= t_dt < window_end):
            continue

        cloud_summit = summit_cloud_series[idx] if summit_cloud_series else None
        wind_speed = wind_speed_series[idx] if wind_speed_series else None
        temp_est = temp_series[idx] if temp_series else None
        chill = wind_chill_c(temp_est, wind_speed)

        rows.append({
            "date": day_str,
            "time": t_dt.strftime("%H:%M"),
            "precip": precip_prob[idx],
            "precip_mm": precip_mm[idx],
            "cloud": cloud[idx],
            "cloud_low": cloud_low[idx],
            "cloud_mid": cloud_mid[idx],
            "cloud_high": cloud_high[idx],
            "cloud_1000m": band_series[1000][idx],
            "cloud_2000m": band_series[2000][idx],
            "rh_2000m": humidity_series[2000][idx],
            "cloud_3000m": band_series[3000][idx],
            "cloud_summit": cloud_summit,
            "summit_alt_m": summit_alt_m,
            "temp_2m_c": temp_2m[idx],
            "wind10_speed_kmh": wind10_speed[idx],
            "wind10_dir": wind10_dir[idx],
            "wind_speed_1000m_kmh": wind_speed_band_series[1000][idx],
            "wind_dir_1000m": wind_dir_band_series[1000][idx],
            "wind_speed_2000m_kmh": wind_speed_band_series[2000][idx],
            "wind_dir_2000m": wind_dir_band_series[2000][idx],
            "wind_speed_3000m_kmh": wind_speed_band_series[3000][idx],
            "wind_dir_3000m": wind_dir_band_series[3000][idx],
            "wind_speed": wind_speed,
            "wind_dir": wind_dir_series[idx] if wind_dir_series else None,
            "temp_est": temp_est,
            "chill": chill,
        })
    return rows


# ---------------------------------------------------------------------------
# GPX route waypoint extraction (ヤマレコ登山計画書)
# ---------------------------------------------------------------------------
GPX_NAMESPACE = {"gpx": "http://www.topografix.com/GPX/1/1"}


def parse_gpx_waypoints(filepath: str, dedupe: bool = True) -> list:
    """Extract named waypoints (trailhead/hut/summit/pass -- any <trkpt> that
    carries a <name>) from a Yamareco 登山計画書 GPX file, skipping the much
    larger set of unnamed route-interpolation points that only exist to
    trace the path between them.

    A round-trip route often revisits the same place twice (e.g. a hut
    passed on both the outbound and return leg) with the same <name>, and a
    multi-day trip can spread those visits across different calendar dates
    (e.g. a hut passed outbound on day 1 and again on the return descent on
    day 3). dedupe=True (default) keeps only the first, i.e.
    earliest-in-file / outbound-leg, occurrence of each name and drops
    later repeats -- right for diagnose_route()'s tables, which are
    deliberately one row per place, not per visit (unchanged behavior).
    Pass dedupe=False to keep EVERY occurrence instead, each with its own
    <time> -- for render_route_weather_map() (2026-09, multi-day trips),
    which is built to handle multiple visits to the same coordinate (see
    its docstring and _spread_duplicate_points()); main_gpx_route() parses
    the file a second time with dedupe=False just for the map, after
    diagnose_route() has already run against the deduped list.

    Returns a list of dicts in file order:
        {"name": str, "lat": float, "lon": float, "elevation_m": float,
         "time": str or None}
    "time" is the plan's scheduled timestamp, raw <time> text exactly as
    Yamareco wrote it (e.g. "2026-05-06T21:00:00Z", UTC) -- this is the
    plan's actual designated climbing date+time, not a placeholder
    (confirmed 2026-09 against two real 登山計画書; see
    _gpx_time_to_local()'s docstring). print_waypoint_list() shows it
    verbatim (still UTC there, unconverted) for reference; the GPX route
    diagnosis flow's actual forecast date is chosen separately by the
    caller regardless (prompt_target_date() -- see its docstring), letting
    you compare any date within the forecast window even when it differs
    from whatever's in the plan. render_route_weather_map(), in contrast,
    uses this field directly by default (see compute_arrival_times()).
    """
    tree = ET.parse(filepath)
    root = tree.getroot()

    waypoints = []
    seen_names = set()
    for trkpt in root.iter(f"{{{GPX_NAMESPACE['gpx']}}}trkpt"):
        name_el = trkpt.find("gpx:name", GPX_NAMESPACE)
        if name_el is None or not (name_el.text and name_el.text.strip()):
            continue
        name = name_el.text.strip()
        if dedupe:
            if name in seen_names:
                continue
            seen_names.add(name)

        ele_el = trkpt.find("gpx:ele", GPX_NAMESPACE)
        time_el = trkpt.find("gpx:time", GPX_NAMESPACE)
        waypoints.append({
            "name": name,
            "lat": float(trkpt.get("lat")),
            "lon": float(trkpt.get("lon")),
            "elevation_m": float(ele_el.text) if ele_el is not None and ele_el.text else None,
            "time": time_el.text if time_el is not None else None,
        })
    return waypoints


def parse_gpx_track_points(filepath: str) -> list:
    """Every <trkpt>'s (lat, lon), in file order, named or not -- unlike
    parse_gpx_waypoints() (which keeps only named points, for the
    diagnosis/label list), this is for drawing the actual trail shape on
    render_route_weather_map()'s map, where the unnamed route-interpolation
    points are exactly what's needed (straight lines between only the named
    waypoints would cut corners across switchbacks/contours)."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    return [
        (float(trkpt.get("lat")), float(trkpt.get("lon")))
        for trkpt in root.iter(f"{{{GPX_NAMESPACE['gpx']}}}trkpt")
    ]


_FURIGANA_SUFFIX_RE = re.compile(r"\s*\[[^\[\]]*\]\s*$")


def strip_furigana(name: str) -> str:
    """Drop a trailing ' [かな読み]' bracket -- ヤマレコ's <trkpt><name> often
    carries the reading this way (e.g. '上日川峠 [かみひかわとうげ]'), which
    is redundant for a display table. Names with no bracket pass through
    unchanged."""
    return _FURIGANA_SUFFIX_RE.sub("", name)


def prompt_gpx_path() -> str:
    while True:
        raw = input("\nGPXファイルのパスを入力: ").strip().strip('"')
        if os.path.isfile(raw):
            return raw
        print(f"  ファイルが見つかりません: {raw}")


def print_waypoint_list(waypoints: list):
    print("\n=== GPXから抽出されたwaypoints ===\n")
    header = pad("No", 4) + pad("地点名", 22) + pad("標高", 8) + pad("時刻(計画)", 12)
    print(header)
    for i, wp in enumerate(waypoints, start=1):
        time_label = wp["time"].split("T")[1][:5] if wp["time"] else "-"
        row = (
            pad(str(i), 4) + pad(strip_furigana(wp["name"]), 22)
            + pad(f'{wp["elevation_m"]:.0f}m', 8) + pad(time_label, 12)
        )
        print(row)


def prompt_waypoint_selection(waypoints: list) -> list:
    """Comma-separated indices into waypoints (e.g. '1,3,5'), or blank for
    all of them. Falls back to all waypoints on empty/unparseable input --
    a name-or-type-code heuristic for "which points matter" isn't reliable
    across arbitrary GPX files (e.g. a hut kept on one route may be a minor
    side stop on another), so the user picks explicitly instead."""
    raw = input(
        f"\n診断する地点の番号をカンマ区切りで入力 (例: 1,3,5 / 全地点は Enter): "
    ).strip()
    if not raw:
        return waypoints
    indices = [int(p) - 1 for p in raw.split(",") if p.strip().isdigit() and 1 <= int(p.strip()) <= len(waypoints)]
    return [waypoints[i] for i in indices] if indices else waypoints


def prompt_target_date() -> str:
    """Target date for the GPX route diagnosis's text tables -- deliberately
    independent of the GPX plan's own <time> values (see
    parse_gpx_waypoints), so you can compare the score/hourly tables for any
    date in the forecast window, whether or not it's the plan's own
    designated date. render_route_weather_map()'s map, in contrast, defaults
    to the plan's own date (resolve_route_target_date()) rather than asking
    here -- the two can end up showing different dates for the same run if
    you pick one here and don't override the map's start time to match."""
    today = date.today()
    max_date = today + timedelta(days=FORECAST_DAYS - 1)
    while True:
        raw = input(f"\n対象日を入力 (YYYY-MM-DD, {today.isoformat()}〜{max_date.isoformat()}): ").strip()
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            print("  日付の形式が正しくありません。例: 2026-09-05")
            continue
        if not (today <= d <= max_date):
            print(f"  予報範囲外です。{today.isoformat()}〜{max_date.isoformat()}の範囲で入力してください。")
            continue
        return d.isoformat()


# ---------------------------------------------------------------------------
# Interactive selection + single-mountain forecast table
# ---------------------------------------------------------------------------
def print_mountain_list():
    print("\n=== 山を選んでください ===\n")
    header = pad("No", 4) + pad("山", 26) + pad("アクセス", 24) + pad("地域", 10)
    print(header)
    for i, m in enumerate(MOUNTAINS, start=1):
        row = (
            pad(str(i), 4) + pad(f'{m["name"]}({m["elevation_m"]}m)', 26)
            + pad(m["access"], 24) + pad(m["region"], 10)
        )
        print(row)


def prompt_selection() -> dict:
    while True:
        choice = input(f"\n番号を入力 (1-{len(MOUNTAINS)}): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(MOUNTAINS):
            return MOUNTAINS[int(choice) - 1]
        print("  無効な入力です。番号を入力してください。")


def fmt(val, ndigits: int = 1) -> str:
    """None-safe rounded string for table cells. ndigits=0 rounds to an int
    (no trailing '.0'), matching how whole-meter/percent values read best."""
    if val is None:
        return "-"
    return str(round(val)) if ndigits == 0 else str(round(val, ndigits))


def wind_band_cell(speed_kmh, dir_deg) -> str:
    """'{speed}m/s({方角})' for a single altitude band's wind, or '-' if
    that band has no data this hour."""
    if speed_kmh is None:
        return "-"
    return f"{fmt(speed_kmh * KMH_TO_MS)}m/s({compass_label(dir_deg)})"


def hazard_warning_cell(r) -> str:
    """Thunder/wind/cold(hypothermia) hazard level (mountain_hazards()/
    hazard_level() in core.py) plus the CAPE/peak-gust/wet-chill numbers
    behind each, or '-' when none are active. None of the three feed
    mountain_climb_score (see SCORE_WEIGHTS' comment in core.py), so this is
    now the only place any of them shows up -- supersedes the old wind-only
    out-of-window flag, since wind here is judged on the day's peak gust
    already (see compute_day_scores)."""
    h = r["hazards"]
    if not h["any"]:
        return "-"
    parts = []
    if h["thunder"] != "-":
        parts.append(f"雷{h['thunder']}(CAPE{fmt(r['cape'], 0)})")
    if h["wind"] != "-":
        hour = r["day_peak_wind_hour"]
        hour_label = f"{hour:02d}時" if hour is not None else ""
        parts.append(f"強風{h['wind']}({hour_label}{fmt(r['day_peak_wind_ms'])}m/s)")
    if h["cold"] != "-":
        parts.append(f"低体温症{h['cold']}(体感{fmt(r['chill'])}℃)")
    return f"⚠{'・'.join(parts)}"


# PRECIP_TIMING_* constants: shared with mountain_weather_mvp.py via
# mountain_weather_core.py (imported above; see that module for the
# rationale). precip_timing_note() itself stays defined separately in each
# file -- this file's version takes a day_scores dict, mvp.py's takes
# (day_index, rows) -- differently shaped arguments for differently shaped
# per-day tables.
def precip_timing_note(day_scores: dict):
    """PRECIP_TIMING_NOTE if any day in day_scores is PRECIP_TIMING_UNCERTAIN_DAYS_OUT+
    out, or has a precip_wet_pct (fraction of the activity window's hours
    that are wet, see compute_day_scores) inside the ambiguous 40-60% band,
    else None. Checked once per table rather than per-row -- with a 14-day
    forecast, days 3+ out are the norm, not the exception, so this reads as
    a single standing caveat rather than a per-row flag."""
    today = date.today()
    for day_str, r in day_scores.items():
        days_out = (date.fromisoformat(day_str) - today).days
        wet_pct = r["precip_wet_pct"]
        boundary = wet_pct is not None and PRECIP_TIMING_BOUNDARY_LOW <= wet_pct <= PRECIP_TIMING_BOUNDARY_HIGH
        if days_out >= PRECIP_TIMING_UNCERTAIN_DAYS_OUT or boundary:
            return PRECIP_TIMING_NOTE
    return None


def wet_fraction_cell(r) -> str:
    """'35.7(5/14h M 湿5 0.6mm)' -- M = every hour judged on MSM (precipitation
    + moisture rule), 'M9' = 9 of the hours were, '確n' = n hours beyond
    MSM's range judged on the ECMWF probability as an expected value
    (wet_hours is then fractional: expected wet hours); '湿n' = n of the
    MSM-judged wet hours came from the moisture rule alone (no MSM rain);
    the trailing mm is the activity window's total precipitation (the
    intensity side of precip_penalty, shown only when non-zero). See
    core.wet_hour_weight()."""
    msm, total = r.get("precip_msm_hours", 0), r["precip_total_hours"]
    tag = "" if not msm else (" M" if msm == total else f" M{msm}")
    prob = r.get("precip_prob_hours", 0)
    if prob:
        tag += f" 確{prob}"
    moist = r.get("precip_moist_hours", 0)
    if moist:
        tag += f" 湿{moist}"
    wet_hours = r["precip_wet_hours"]
    wet_hours_s = str(int(wet_hours)) if float(wet_hours).is_integer() else f"{wet_hours:.1f}"
    total_mm = r.get("precip_total_mm") or 0.0
    mm_tag = f" {total_mm:.1f}mm" if total_mm >= 0.05 else ""
    return f"{fmt(r['precip_wet_pct'])}({wet_hours_s}/{total}h{tag}){mm_tag}"


def print_day_score_table(mtn: dict, day_scores: dict):
    """Day-by-day mountain_climb_score for the selected mountain -- the same
    scoring formula/weights mountain_weather_mvp.py uses to rank all 75
    mountains for a given day, here shown across all forecast days for just
    this one mountain."""
    print(f"\n=== {mtn['name']}({mtn['elevation_m']}m) 登山向け総合スコア(日別) ===")
    print(f"(稜線帯:日の出{TRIP_START_OFFSET_HOURS + RIDGE_DWELL_TRIM_HOURS:+.0f}h〜{MAIN_TIME_END_HOUR - RIDGE_DWELL_TRIM_HOURS}時 "
          f"PM(下山・雷/雲)開始:{PM_START_HOUR}時 "
          f"活動時間(降水判定・PM共通の終了):日の出{TRIP_START_OFFSET_HOURS:+.0f}h〜日没+{ACTIVITY_END_GRACE_MINUTES}分 / "
          f"雲量(稜線/PM)・視程・降水(活動時間中の雨天割合)の重み付き幾何平均(各{SCORE_WEIGHTS['cloud']:.2f}) / "
          f"雷・強風・低体温症はスコアに含めず「危険信号」列で別枠警告 / "
          f"雨天判定:MSM範囲内(表のM表記)はMSM降水量{WET_HOUR_MSM_PRECIP_MM}mm/h以上または湿潤層(山頂面/その下の面でRH{WET_HOUR_RH_PCT:.0f}%以上かつ雲量{WET_HOUR_MOIST_CLOUD_PCT:.0f}%以上、湿n表記)、MSM範囲外(確n表記)はECMWF降水確率の期待値(各時間の確率/100の合計、4日目以降の時間別詳細は追わない) / 降水量は活動時間内の合計mm({PRECIP_FULL_PENALTY_TOTAL_MM:.0f}mmで満点ペナルティ) / PM雲量は連続{PM_CLOUD_PERSIST_HOURS}時間平均の最大値)\n")
    header = (
        pad("日付", 18) + pad("危険信号(雷/強風/低体温症)", 34) + pad("スコア", 8) + pad("稜線風速m/s", 12)
        + pad("雷リスクCAPE", 14) + pad("雲量%(稜線/PM)", 14) + pad("視程m(稜線)", 10)
        + pad("雨天割合%(活動時間)", 30) + pad("体感温度℃(稜線)", 16)
        + pad("降水量mm(全日)", 14)
    )
    print(header)
    for day_str in sorted(day_scores):
        r = day_scores[day_str]
        wet_cell = wet_fraction_cell(r)
        row = (
            pad(format_date_with_weekday(day_str), 18) + pad(hazard_warning_cell(r), 34)
            + pad(str(r["score"]), 8)
            + pad(fmt(r["ridge_wind_ms"]), 12) + pad(fmt(r["cape"], 0), 14)
            + pad(fmt(r["cloud_pct"]), 14) + pad(fmt(r["ridge_visibility"], 0), 10)
            + pad(wet_cell, 30)
            + pad(fmt(r["chill"]), 16)
            + pad(fmt(r.get("day_total_precip_mm"), 0), 14)
        )
        print(row)
    print("(「降水量mm(全日)」は登り区間(AM)に限らない0-23時の合計。AM降水確率が低くても"
          "全日ではまとまった雨になる日があるため参考に併記)")

    note = precip_timing_note(day_scores)
    if note:
        print(f"\n{note}")


def print_hourly_table(mtn: dict, rows: list):
    print(f"\n=== {mtn['name']}({mtn['elevation_m']}m) / {mtn['access']} / {mtn['region']} ===")
    hpa_note = "/".join(f"{ALTITUDE_BAND_HPA[alt]}hPa≈{band_altitude_label(alt)}" for alt in FIXED_ALTITUDE_BANDS_M)
    print(f"(日の出{EARLY_START_OFFSET_HOURS:+.0f}h〜日の入りまで1時間ごと。"
          f"雲量・風とも地上+気圧面3バンド(標高は標準大気換算: {hpa_note})。今日から{FORECAST_DAYS}日先まで)\n")

    header = (
        pad("日付", 18) + pad("時刻", 7) + pad("降水確率%(地上)", 16)
        + pad("雲量地上%", 10) + pad(f"雲量{band_altitude_label(1000)}%", 13)
        + pad(f"雲量{band_altitude_label(2000)}%", 13) + pad(f"雲量{band_altitude_label(3000)}%", 13)
        + pad("風地上", 17) + pad(f"風{band_altitude_label(1000)}", 17)
        + pad(f"風{band_altitude_label(2000)}", 17) + pad(f"風{band_altitude_label(3000)}", 17)
    )
    print(header)
    last_date = None
    for r in rows:
        date_label = format_date_with_weekday(r["date"]) if r["date"] != last_date else ""
        last_date = r["date"]
        row = (
            pad(date_label, 18) + pad(r["time"], 7)
            + pad(fmt(r["precip"]), 16) + pad(fmt(r["cloud"]), 10)
            + pad(fmt(r["cloud_1000m"]), 13) + pad(fmt(r["cloud_2000m"]), 13)
            + pad(fmt(r["cloud_3000m"]), 13)
            + pad(wind_band_cell(r["wind10_speed_kmh"], r["wind10_dir"]), 17)
            + pad(wind_band_cell(r["wind_speed_1000m_kmh"], r["wind_dir_1000m"]), 17)
            + pad(wind_band_cell(r["wind_speed_2000m_kmh"], r["wind_dir_2000m"]), 17)
            + pad(wind_band_cell(r["wind_speed_3000m_kmh"], r["wind_dir_3000m"]), 17)
        )
        print(row)


# ---------------------------------------------------------------------------
# Cloud sea / inversion photo-opportunity detector (2026-09 addition).
# Revives ヤマビヨリ's original concept -- find the photogenic clear-air
# moments a plain "is the score good" read misses: 麓は雨でも登山口・山頂は
# 晴れ / 1000m・3000m帯は曇りでも2000m帯だけ晴れ(逆転層) / 雲海のチャンス.
# All three collapse to the same underlying pattern -- clear at your own
# altitude, a solid cloud deck somewhere below you -- so one detector covers
# all three framings instead of three separate ones.
#
# Reuses data the normal fetch_forecast() call already pulls in for every
# mountain (HOURLY_VARS' surface "cloudcover", BAND_VARS' three fixed-band
# clouds, HUMIDITY_VARS' three fixed-band humidities, plus summit_hpa's own
# cloudcover_XXXhPa, which main_single_mountain() already requests via
# extra_vars) -- no new Open-Meteo variables, no new API calls. HUMIDITY_VARS
# in particular was fetched every run but, other than a single rh_2000m
# column build_hourly_rows() surfaces nowhere else uses, went completely
# unused (see SKILL.md's "死んでいるデータ" note) -- here it finally does
# the corroborating job its own comment always said it was for ("a proxy
# for whether that layer is actually saturated").
# ---------------------------------------------------------------------------
CLOUD_SEA_CLEAR_MAX_PCT = 30.0  # summit/own-band cloud% at/below this counts as "clear up top"
CLOUD_SEA_DECK_MIN_PCT = 80.0  # a lower band at/above this counts as a "solid deck" below
CLOUD_SEA_DAWN_START_OFFSET_HOURS = -0.5  # dawn window: sunrise-0.5h .. sunrise+CLOUD_SEA_DAWN_WINDOW_HOURS
CLOUD_SEA_DAWN_WINDOW_HOURS = 2.0
# 雲海 is specifically a dawn phenomenon (nocturnal radiative cooling pools
# cold, moist air -- and the cloud deck it forms -- in the valleys
# overnight; daytime mixing usually breaks it up by mid-morning), so this
# is restricted to that window rather than firing on any clear-above/
# cloudy-below hour all day -- a clear noon with valley haze is real but
# isn't what "雲海のチャンス" means culturally, and reporting it as one
# would dilute the signal for the actually-photogenic dawn moments.


def detect_cloud_sea_opportunity(forecast: dict, summit_hpa: int) -> list:
    """Scan for hours where the mountain's own band (summit_hpa) is clear
    while some band BELOW its real altitude -- surface, or a fixed
    1000/2000/3000m band, whichever are genuinely lower -- is under a solid
    cloud deck, restricted to the dawn window (see banner comment). When
    multiple lower bands qualify at once, the one CLOSEST TO THE SUMMIT is
    reported (see the candidates-sort comment below for why), falling back
    to surface only when no fixed band qualifies.

    Returns a list of {"date", "time", "summit_cloud_pct", "deck_band",
    "deck_cloud_pct", "deck_humidity_pct"} dicts in chronological order.
    deck_band is a display label ("surface", "1000m帯", etc.);
    deck_humidity_pct is that band's relative humidity at the same hour
    when available (None for the surface candidate, which has no
    HUMIDITY_VARS entry), included as corroboration -- not a hard filter,
    since cloud% and humidity don't always cross their thresholds in
    perfect lockstep and requiring both would under-detect real events."""
    times = forecast["hourly"]["time"]
    daily_sunrise = forecast["_daily_sunrise"]
    summit_cloud_series = forecast["hourly"].get(f"cloudcover_{summit_hpa}hPa")
    surface_cloud_series = forecast["hourly"].get("cloudcover")
    band_cloud_series = {alt: forecast["hourly"].get(var) for alt, var in BAND_VARS.items()}
    band_humidity_series = {alt: forecast["hourly"].get(var) for alt, var in HUMIDITY_VARS.items()}
    summit_actual_m = pressure_level_altitude_m(summit_hpa)

    # Candidate deck layers below the summit's own altitude, CLOSEST TO THE
    # SUMMIT first, so the reported deck is the one immediately below the
    # clear air rather than always "surface" (surface cloud, being lowest
    # by definition, would otherwise win the "first qualifying" check on
    # almost every real cloud-sea hour, since if the summit's clear and
    # anything below is overcast, the true valley floor usually is too --
    # that made the fixed bands' humidity corroboration nearly unreachable
    # in testing, precisely the "activate the dead humidity data" gap this
    # feature was meant to close). A band immediately under the clear layer
    # is also the more diagnostic reading for a genuine inversion cap
    # anyway, vs. deep/turbulent boundary-layer stratus at the surface. A
    # fixed band only counts if it's meaningfully below the summit's real
    # altitude (skip a "1000m帯" candidate for a mountain whose own band
    # already IS ~1000m); surface is always a candidate and sorts last, as
    # the fallback when no fixed band qualifies.
    candidates = [("surface", 0.0, surface_cloud_series, None)]
    for alt in FIXED_ALTITUDE_BANDS_M:
        band_actual_m = ALTITUDE_BAND_ACTUAL_M[alt]
        if band_actual_m < summit_actual_m - 100:
            candidates.append((f"{alt}m帯", band_actual_m, band_cloud_series.get(alt), band_humidity_series.get(alt)))
    candidates.sort(key=lambda c: c[1], reverse=True)

    opportunities = []
    if summit_cloud_series is None or len(candidates) <= 0:
        return opportunities

    for idx, t in enumerate(times):
        day_str = t.split("T")[0]
        sunrise_iso = daily_sunrise.get(day_str)
        if sunrise_iso is None:
            continue
        t_dt = datetime.fromisoformat(t)
        window_start = datetime.fromisoformat(sunrise_iso) + timedelta(hours=CLOUD_SEA_DAWN_START_OFFSET_HOURS)
        window_end = window_start + timedelta(hours=CLOUD_SEA_DAWN_WINDOW_HOURS)
        if not (window_start <= t_dt < window_end):
            continue

        summit_cloud = summit_cloud_series[idx]
        if summit_cloud is None or summit_cloud > CLOUD_SEA_CLEAR_MAX_PCT:
            continue

        for label, _, cloud_series, humidity_series in candidates:
            if cloud_series is None:
                continue
            deck_cloud = cloud_series[idx]
            if deck_cloud is None or deck_cloud < CLOUD_SEA_DECK_MIN_PCT:
                continue
            opportunities.append({
                "date": day_str,
                "time": t_dt.strftime("%H:%M"),
                "summit_cloud_pct": summit_cloud,
                "deck_band": label,
                "deck_cloud_pct": deck_cloud,
                "deck_humidity_pct": humidity_series[idx] if humidity_series else None,
            })
            break  # closest-to-summit qualifying deck only

    return opportunities


def print_cloud_sea_opportunities(mtn: dict, opportunities: list):
    """予報期間中の雲海・快晴チャンス一覧 -- see detect_cloud_sea_opportunity()
    for what qualifies. Prints nothing when the list is empty (no
    "見つかりませんでした" noise -- most mountains on most days won't have
    one, and that's an unremarkable, not noteworthy, result)."""
    if not opportunities:
        return
    print(f"\n=== {mtn['name']}({mtn['elevation_m']}m) 雲海・快晴チャンス "
          f"(日の出前後、稜線晴れ+下層に雲) ===")
    print(f"(稜線雲量{CLOUD_SEA_CLEAR_MAX_PCT:.0f}%以下 かつ 下層のいずれかの帯で雲量"
          f"{CLOUD_SEA_DECK_MIN_PCT:.0f}%以上。参考値であり、実際に見えるかは大気の状態や視界に左右されます)\n")
    header = (
        pad("日付", 18) + pad("時刻", 7) + pad("稜線雲量%", 10)
        + pad("雲海の高さ", 12) + pad("その雲量%", 10) + pad("湿度%(参考)", 12)
    )
    print(header)
    for o in opportunities:
        row = (
            pad(format_date_with_weekday(o["date"]), 18) + pad(o["time"], 7)
            + pad(fmt(o["summit_cloud_pct"], 0), 10) + pad(o["deck_band"], 12)
            + pad(fmt(o["deck_cloud_pct"], 0), 10)
            + pad(fmt(o["deck_humidity_pct"], 0) if o["deck_humidity_pct"] is not None else "-", 12)
        )
        print(row)


# Within-day weather transition detection (2026-09 addition, diagnosis-mode
# only -- mvp.py has no per-hour data to run this against). Confirmed
# against a real incident: 唐松岳 9/6 hiking reports showed early starters
# climbing in clear weather while the ridge/descent turned to rain from
# midday, and late starters getting rained on from around 2000m elevation
# onward the whole way -- a single per-day score/hazard can't convey a day
# that's genuinely two different experiences depending on start time/pace,
# but the hourly data (build_hourly_rows) already has it.
TRANSITION_LOW_PCT = 30.0   # "calm" side of a transition (segment average at/below this)
TRANSITION_HIGH_PCT = 60.0  # "risky" side of a transition (segment average at/above this)
TRANSITION_MIN_SEGMENT_HOURS = 2  # each side of the split must span at least this many hours


def detect_weather_transition(day_rows: list):
    """Finds the EARLIEST hour that splits one day's ground precip
    probability (build_hourly_rows() rows already filtered to one date) into
    a calm stretch (segment average <= TRANSITION_LOW_PCT) followed by a
    risky one (segment average >= TRANSITION_HIGH_PCT), or the reverse
    (risky-then-calm -- e.g. morning fog burning off into a clear
    afternoon). Returns {"time": "HH:MM", "before_avg": ..., "after_avg":
    ..., "direction": "worsening"/"improving"} or None if no such split
    exists.

    Deliberately reports the ONSET (earliest qualifying split), not the
    point of widest before/after contrast -- an earlier version scanned for
    the single biggest gap and correctly found a transition, but at the
    point furthest into the ramp (e.g. 15:00 on a day that visibly started
    turning at 13:00) rather than the moment a climber would actually want
    a heads-up about. Confirmed against 唐松岳 9/6's real hourly data
    (0-1% through 11:00, ramping 4%->24%->51%->73%->83%->87%->90% from
    12:00): this method reports 13:00 (first hour where the remaining
    average clears 60%), matching hiking reports of rain setting in around
    midday, not 15:00.

    Deliberately simple (plain threshold crossing, not a fitted model) since
    the input (14-day hourly forecast) is itself uncertain beyond a couple
    of days out (see PRECIP_TIMING_* / 既知の精度傾向 in SKILL.md)."""
    probs = [r["precip"] for r in day_rows if r["precip"] is not None]
    times = [r["time"] for r in day_rows if r["precip"] is not None]
    n = len(probs)
    if n < TRANSITION_MIN_SEGMENT_HOURS * 2:
        return None

    for i in range(TRANSITION_MIN_SEGMENT_HOURS, n - TRANSITION_MIN_SEGMENT_HOURS + 1):
        before_avg = sum(probs[:i]) / i
        after_avg = sum(probs[i:]) / (n - i)
        if before_avg <= TRANSITION_LOW_PCT and after_avg >= TRANSITION_HIGH_PCT:
            return {"time": times[i], "before_avg": before_avg, "after_avg": after_avg, "direction": "worsening"}

    for i in range(TRANSITION_MIN_SEGMENT_HOURS, n - TRANSITION_MIN_SEGMENT_HOURS + 1):
        before_avg = sum(probs[:i]) / i
        after_avg = sum(probs[i:]) / (n - i)
        if before_avg >= TRANSITION_HIGH_PCT and after_avg <= TRANSITION_LOW_PCT:
            return {"time": times[i], "before_avg": before_avg, "after_avg": after_avg, "direction": "improving"}

    return None


def transition_note(transition: dict) -> str:
    """One advisory line for detect_weather_transition()'s result, or ''
    when there's no transition to report."""
    if transition is None:
        return ""
    t = transition
    if t["direction"] == "worsening":
        return (f"※{t['time']}頃を境に降水確率が悪化する見込み"
                f"(それまで平均{t['before_avg']:.0f}% → それ以降平均{t['after_avg']:.0f}%)。"
                f"早めの行動開始・早め下山で回避できる可能性があります。")
    return (f"※{t['time']}頃を境に降水確率が改善する見込み"
            f"(それまで平均{t['before_avg']:.0f}% → それ以降平均{t['after_avg']:.0f}%)。"
            f"天候の回復を待てる余裕があれば選択肢になりますが、無理な待機は避けてください。")


def print_weather_transitions(mtn: dict, rows: list):
    """Per-day transition advisories across the forecast period -- see
    detect_weather_transition(). Prints nothing when no day has one (most
    days are consistently good or consistently bad all day, and that's an
    unremarkable, not noteworthy, result -- same convention as
    print_cloud_sea_opportunities())."""
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    lines = []
    for day_str in sorted(by_date):
        transition = detect_weather_transition(by_date[day_str])
        if transition is not None:
            lines.append(f"{format_date_with_weekday(day_str)}: {transition_note(transition)}")

    if not lines:
        return
    print(f"\n=== {mtn['name']}({mtn['elevation_m']}m) 天候の変わり目 ===")
    print("(1日の中で降水確率が大きく変化する日のみ表示。早出/待機の判断材料に)\n")
    for line in lines:
        print(line)


def print_comparison_table(mtn: dict, rows: list, target_date: str):
    """Raw values for one day, matched to what Windy/SCW screenshots show
    directly (actual mm, actual 2m temp, actual 10m wind) -- for eyeballing
    our forecast against a screenshot of either site at the same hour.
    Not wired into main(); call this from a one-off snippet, e.g.:

        import mountain_weather_detail as m
        rows = m.build_hourly_rows(forecast, wind_speed_var, wind_dir_var, summit_hpa=wind_hpa, temp_var=temp_var)
        m.print_comparison_table(mtn, rows, "2026-09-01")
    """
    day_rows = [r for r in rows if r["date"] == target_date]
    summit_alt_m = day_rows[0]["summit_alt_m"] if day_rows else None
    summit_label = f"山頂帯雲量%(約{summit_alt_m}m)" if summit_alt_m else "山頂帯雲量%"
    print(f"\n=== {mtn['name']}({mtn['elevation_m']}m) {format_date_with_weekday(target_date)} "
          f"/ Windy・SCW比較用の生値 ===\n")
    header = (
        pad("時刻", 7) + pad("降水量mm", 9) + pad("総雲量%", 9)
        + pad("低層雲%", 9) + pad("中層雲%", 9) + pad("高層雲%", 9) + pad(summit_label, 20)
        + pad("気温℃(2m)", 10) + pad("風速m/s(10m)", 13) + pad("風向(10m)", 8)
    )
    print(header)
    for r in day_rows:
        row = (
            pad(r["time"], 7) + pad(fmt(r["precip_mm"]), 9) + pad(fmt(r["cloud"]), 9)
            + pad(fmt(r["cloud_low"]), 9) + pad(fmt(r["cloud_mid"]), 9) + pad(fmt(r["cloud_high"]), 9)
            + pad(fmt(r["cloud_summit"]), 20)
            + pad(fmt(r["temp_2m_c"]), 10)
            + pad(fmt(r["wind10_speed_kmh"] * KMH_TO_MS if r["wind10_speed_kmh"] is not None else None), 13)
            + pad(compass_label(r["wind10_dir"]), 8)
        )
        print(row)


# ---------------------------------------------------------------------------
# GPX route diagnosis -- per-waypoint elevation-band weather, for a single
# caller-chosen date. Distinct from print_hourly_table (fixed 1000/2000/3000m
# bands, one lat/lon = the selected mountain's own): here each waypoint has
# its own lat/lon *and* its own elevation, so each gets its own Open-Meteo
# fetch and its own nearest_pressure_level().
# ---------------------------------------------------------------------------
def fetch_waypoint_hourly(wp: dict, target_date: str):
    """Fetch + score one GPX waypoint for target_date: the per-point
    nearest_pressure_level -> fetch_forecast -> compute_day_scores ->
    build_hourly_rows pipeline, factored out so diagnose_route() (text
    tables) and render_route_weather_map() (PNG icons) consume identical
    per-point data instead of two copies of the same fetch logic.

    Returns (hpa, day_score, day_rows). day_score is compute_day_scores()'s
    entry for target_date (None if that date has no data). day_rows is
    build_hourly_rows()'s rows filtered to target_date. May raise
    requests.exceptions.RequestException -- caller decides how to handle
    a failed fetch for one point without aborting the rest of the route."""
    hpa = nearest_pressure_level(wp["elevation_m"])
    wind_speed_var = f"windspeed_{hpa}hPa"
    wind_dir_var = f"winddirection_{hpa}hPa"
    temp_var = f"temperature_{hpa}hPa"
    cloud_var = f"cloudcover_{hpa}hPa"
    # WIND_SPEED_BAND_VARS/WIND_DIR_BAND_VARS aren't used by
    # print_waypoint_table, but build_hourly_rows() unconditionally reads
    # them (it also builds the fixed 1000/2000/3000m wind columns
    # print_hourly_table wants) -- same extra_vars main_single_mountain()
    # passes for the single-mountain flow.
    extra_vars = ([wind_speed_var, wind_dir_var, temp_var, cloud_var]
                  + moisture_vars_for_summit(hpa)
                  + list(WIND_SPEED_BAND_VARS.values()) + list(WIND_DIR_BAND_VARS.values()))

    forecast = fetch_forecast(wp["lat"], wp["lon"], extra_vars=extra_vars)
    day_scores = compute_day_scores(forecast, wind_speed_var, hpa, temp_var)
    day_score = day_scores.get(target_date)
    rows = build_hourly_rows(forecast, wind_speed_var, wind_dir_var, summit_hpa=hpa, temp_var=temp_var)
    day_rows = [r for r in rows if r["date"] == target_date]
    return hpa, day_score, day_rows


def print_waypoint_table(wp: dict, hpa: int, day_rows: list, day_score: dict = None):
    label = strip_furigana(wp["name"])
    alt_m = round(pressure_level_altitude_m(hpa))
    print(f"\n--- {label}({wp['elevation_m']:.0f}m) / 使用気圧面:{hpa}hPa≈約{alt_m}m ---")
    if day_score is not None:
        print(f"    登山向け総合スコア: {day_score['score']} "
              f"(稜線風速{fmt(day_score['ridge_wind_ms'])}m/s 雲量{fmt(day_score['cloud_pct'])}%(稜線/PM) "
              f"雷リスクCAPE{fmt(day_score['cape'], 0)} 体感温度{fmt(day_score['chill'])}℃) "
              f"危険信号: {hazard_warning_cell(day_score)}")
    if not day_rows:
        print("  (対象日のデータがありません -- 予報範囲外の可能性があります)")
        return
    header = pad("時刻", 7) + pad("雲量%", 8) + pad("風", 17) + pad("気温℃", 8)
    print(header)
    for r in day_rows:
        row = (
            pad(r["time"], 7) + pad(fmt(r["cloud_summit"]), 8)
            + pad(wind_band_cell(r["wind_speed"], r["wind_dir"]), 17)
            + pad(fmt(r["temp_est"]), 8)
        )
        print(row)


def diagnose_route(waypoints: list, target_date: str):
    """Fetch and print a per-hour cloud/wind/temperature table for each
    waypoint, one print_waypoint_table() block per point, all for the same
    caller-chosen target_date. Each waypoint gets its own Open-Meteo fetch
    (its own lat/lon) at the pressure level nearest its own elevation_m --
    the same per-mountain-exact-elevation approach main() uses for the
    single selected mountain (wind_vars_for_elevation/temp_var_for_elevation),
    just repeated per route point instead of once.

    Also runs the unmodified compute_day_scores()/mountain_climb_score()
    pipeline per waypoint (same inputs it already takes for a single
    mountain -- wind_speed_var/summit_hpa/temp_var -- just derived per
    point here) and prints target_date's score/breakdown alongside the
    hourly table, then a route-wide ranking by that score at the end, so
    "which point on this route looks best/worst that day" falls out of the
    existing scoring logic for free rather than needing a separate metric."""
    ranking = []
    hpa_used = set()
    for wp in waypoints:
        try:
            hpa, day_score, day_rows = fetch_waypoint_hourly(wp, target_date)
        except requests.exceptions.RequestException as e:
            print(f"\n{strip_furigana(wp['name'])}: 取得に失敗しました: {e}")
            continue

        hpa_used.add(hpa)
        print_waypoint_table(wp, hpa, day_rows, day_score=day_score)

        if day_score is not None:
            ranking.append((strip_furigana(wp["name"]), day_score["score"]))

    if len(ranking) > 1:
        print(f"\n=== ルート内スコア比較({format_date_with_weekday(target_date)}) ===")
        for name, score in sorted(ranking, key=lambda pair: pair[1], reverse=True):
            print(f"  {pad(name, 20)} {score}")

    # Same-band note: if every selected waypoint rounded to one
    # nearest_pressure_level(), their displayed values are identical by
    # construction (same pressure-level variable, and at this route scale
    # usually the same model grid cell too) -- not a bug. See SKILL.md's
    # "GPXルート診断機能" section (大菩薩嶺 vs 唐松岳 comparison) for the
    # confirmed cases this covers.
    if len(waypoints) > 1 and len(hpa_used) == 1:
        only_hpa = next(iter(hpa_used))
        alt_m = round(pressure_level_altitude_m(only_hpa))
        print(f"\n※選択した{len(waypoints)}地点はすべて同じ気圧面({only_hpa}hPa≈約{alt_m}m)"
              f"に丸められたため、表示値が同一になっています(標高差が小さいルートでは"
              f"想定通りの挙動で、バグではありません。標高差の大きいルートでは気圧面の"
              f"境界をまたぎ、地点ごとに値が分かれます)。")


def diagnose_route_by_visit_date(waypoints_to_plot: list, all_waypoints: list = None):
    """Multi-day-aware wrapper around diagnose_route(): groups
    waypoints_to_plot by each visit's own GPX <time> (converted to a local
    calendar date via compute_arrival_times() in default/no-override mode
    -- see its docstring for why that date is trusted as-is, not
    recomputed), then calls the unmodified diagnose_route() once per date
    with that date's subset. diagnose_route() itself still only handles one
    shared target_date -- this is what lets a multi-day out-and-back's
    outbound (day 1) and return (day N) visits to the same named place each
    get their own table, on their own correct date, instead of all being
    forced onto one date or silently dropped.

    waypoints_to_plot: pass parse_gpx_waypoints(gpx_path, dedupe=False)
    (optionally filtered to a selection, e.g. by name) so both
    outbound/return visits are present as separate entries -- the same
    convention render_route_weather_map() uses for the map, so this
    produces the text-table equivalent of that same multi-day view.

    all_waypoints: the full route, passed through to compute_arrival_times()
    as its departure-point anchor (matters if waypoints_to_plot is a
    subset that excludes the actual start point -- see that function's
    docstring). Defaults to waypoints_to_plot when omitted.

    Waypoints with no <time> tag are skipped (there's no date to group them
    under)."""
    arrivals = compute_arrival_times(waypoints_to_plot, all_waypoints=all_waypoints, start_time_str=None)
    by_date = {}
    for wp in waypoints_to_plot:
        arrival_dt = arrivals.get(id(wp))
        if arrival_dt is None:
            print(f"{strip_furigana(wp['name'])}: GPXに時刻情報がないため除外します。")
            continue
        by_date.setdefault(arrival_dt.date(), []).append(wp)

    for d in sorted(by_date):
        print(f"\n{'=' * 70}")
        print(f"=== {format_date_with_weekday(d.isoformat())} ===")
        diagnose_route(by_date[d], d.isoformat())


# ---------------------------------------------------------------------------
# Route weather map PNG (optional; not wired into main() by default -- see
# main_gpx_route()'s y/N prompt after the text diagnosis). Plots a weather
# icon (+ name/arrival time/temperature) at each selected GPX waypoint on a
# GSI (国土地理院) tile basemap, for SNS-style sharing of "what will the
# weather look like at each point on this route, at the time I'll actually
# be there". Separate concern from diagnose_route()'s text tables, but reuses
# the exact same per-point fetch (fetch_waypoint_hourly()), so the icons and
# temperatures shown here are consistent with the text diagnosis for the
# same waypoints/date. Icon classification (pick_weather_icon()) is built on
# the same cloud/precip signals the score functions already consume, not a
# new data source.
#
# Needs Pillow (`pip install Pillow`), imported lazily inside the functions
# that need it -- NOT part of this project's normal `pip install requests`
# dependency, same pattern as fetch_jma_weather_map()'s lazy
# `from playwright...` import.
#
# GSI tile terms of use (https://maps.gsi.go.jp/development/ichiran.html,
# checked 2026-09): rendering tiles live in a web page/app needs no
# application ("リアルタイムに読み込んで利用する場合" is application-free),
# but that page also says downloading tiles and saving/redistributing a
# composited image -- exactly what this function does -- can fall outside
# that exemption ("基本測量成果となっているタイルを利用する際には、測量法に
# 基づく申請が必要な場合があります"). This is a judgment call the user made
# explicitly for this project's small-scale personal/SNS use (accepting that
# risk rather than switching to e.g. OpenStreetMap tiles); if usage ever
# grows beyond that (bulk generation, commercial/public distribution),
# revisit with GSI directly rather than assuming this decision still covers
# it. Attribution (GSI_TILE_ATTRIBUTION below) is drawn onto every generated
# image regardless, per the "「国土地理院」または「地理院タイル」+ link"
# requirement that applies either way.
#
# Target date/arrival-time policy (2026-09, revised after confirming with
# the user that Yamareco 登山計画書 <time> tags ARE the plan's real
# designated date -- see _gpx_time_to_local()'s docstring, which corrects
# this file's earlier wrong assumption that they were a placeholder): by
# default the map trusts the GPX plan's own date+time entirely (no
# start_time override); resolve_route_target_date() refuses to render
# (clear message, no exception) rather than guess when that date is already
# in the past or beyond the forecast horizon.
# ---------------------------------------------------------------------------
MAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")
GSI_TILE_URL_TEMPLATE = "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"
GSI_TILE_ATTRIBUTION = "地図:地理院タイル(国土地理院) maps.gsi.go.jp/development/ichiran.html"
GSI_TILE_CACHE_DIR = os.path.join(CACHE_DIR, "gsi_tiles")
TILE_SIZE_PX = 256
MAP_ZOOM_MIN, MAP_ZOOM_MAX = 12, 17
MAP_MAX_CANVAS_PX = 2000  # keep the stitched basemap (before final crop) under this on its longer side
MAP_PADDING_FRAC = 0.20  # margin kept around the waypoint bounding box when choosing zoom/stitching tiles
MAP_CROP_MARGIN_PX = 190  # final crop margin (px) around the point cluster, for icon/label room
ICON_RADIUS_PX = 13
ICON_VISUAL_RADIUS_PX = ICON_RADIUS_PX + 8  # sunny icon's rays reach r+8 (see _draw_weather_icon) -- the
# true keep-out radius for label placement is this, not ICON_RADIUS_PX, or labels clip the rays.
ROUTE_LINE_COLOR = (255, 0, 170, 235)  # pink, per user request -- drawn under icons/labels
ROUTE_LINE_HALO_COLOR = (255, 255, 255, 200)  # thin white outline so pink stays legible over the tan basemap
ROUTE_LINE_WIDTH_PX = 4
# Common Windows Japanese-capable fonts, tried in order; matplotlib/Pillow's
# own default (DejaVu Sans) has no Japanese glyphs, so waypoint names would
# render as tofu boxes without one of these.
JP_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
]


def pick_weather_icon(cloud_pct, precip_prob, precip_mm) -> str:
    """sunny/cloudy/rain classification for the map icon, reusing the same
    raw signals the existing score functions already consume -- not a new
    data source:
      - rain: precip_prob>=50% AND precip_mm>=0.5mm/h -- the exact same "is
        it actually wet" cutoff wet_chill_adjustment_c() already uses (see
        its docstring) for its low-temp+wind+wet compound risk. Reused
        verbatim here for consistency rather than inventing a new pair of
        numbers.
      - cloudy vs sunny: cloud_pct>=85% -- JMA's own 曇り definition (cloud
        cover 9-10 on the 10-scale JMA reports observations in). This one
        IS new to this codebase: cloud_penalty() uses the raw % linearly
        with no sunny/cloudy breakpoint, so there was nothing existing to
        reuse for this half of the classification."""
    precip_prob = precip_prob or 0.0
    precip_mm = precip_mm or 0.0
    cloud_pct = cloud_pct if cloud_pct is not None else 0.0
    if precip_prob >= 50.0 and precip_mm >= 0.5:
        return "rain"
    if cloud_pct >= 85.0:
        return "cloudy"
    return "sunny"


def _gpx_time_to_local(time_str: str) -> datetime:
    """Parse a GPX <time> tag (UTC, e.g. "2026-09-02T00:00:00Z") into a naive
    Asia/Tokyo local datetime -- matching the wall-clock convention
    fetch_open_meteo() requests (timezone="Asia/Tokyo") and the "HH:MM"
    strings in build_hourly_rows()'s rows already use, so the two are
    directly comparable.

    Yamareco's 登山計画書 GPX <time> tags encode the plan's actual
    designated climbing date+time, confirmed 2026-09 against two real plans
    (唐松岳: exported the day before a next-day 09:00 JST start; 大菩薩嶺:
    an already-past climbing date carried over from an earlier plan) --
    NOT a placeholder unrelated to the real date, which this file assumed
    before that was checked. See compute_arrival_times()'s docstring for
    what that means for how <time> is used here."""
    JST = timezone(timedelta(hours=9))
    return datetime.fromisoformat(time_str.replace("Z", "+00:00")).astimezone(JST).replace(tzinfo=None)


def compute_arrival_times(waypoints_to_plot: list, all_waypoints: list = None,
                           start_time_str: str = None) -> dict:
    """Map each waypoint -> an estimated real-world arrival datetime, keyed
    by id(wp) (each dict's own object identity, NOT wp["name"] -- a
    multi-day out-and-back trip revisits the same named place on a later
    calendar date, so waypoints_to_plot can contain several dicts sharing
    one name with different <time> values; keying by name would collide and
    silently drop all but the last visit). Values come from the GPX plan's
    own <time> tags (which -- see _gpx_time_to_local()'s docstring --
    encode the plan's actual designated date+time, not a placeholder). Two
    modes:

    - start_time_str is None (default): each waypoint's arrival is its own
      GPX <time> AS-IS (just converted from UTC to local wall-clock time) --
      there's nothing to recompute, the plan already says when you'll be
      there. This works unchanged for a multi-day trip: each visit keeps
      its own plan date, whichever day that is.
    - start_time_str given ("HH:MM"): the calendar date still comes from the
      route's departure point (earliest <time> among all_waypoints, local
      date) -- only its time-of-day is overridden to start_time_str -- and
      every point's arrival becomes that new start instant plus the same
      elapsed-since-departure duration the GPX plan encodes (this duration
      can span multiple days -- datetime subtraction handles that the same
      as a same-day gap). Use this when you intend to leave at a different
      clock time than the plan says; it shifts every visit (including
      later days) by that same amount, it does not let you retarget just
      one day.

    all_waypoints: the full route in file order, used to find the
    departure-point anchor (its earliest <time> -- normally the trailhead,
    since Yamareco's course-time tags increase monotonically from the
    start of each leg). Defaults to waypoints_to_plot when omitted. Pass
    the full route explicitly whenever waypoints_to_plot is a subset that
    might exclude the actual start point -- otherwise the anchor silently
    becomes "earliest among what's being plotted" instead of the true
    departure time.

    Waypoints with no <time> tag map to no entry (caller falls back to some
    default, e.g. via _closest_hour_row())."""
    reference = all_waypoints if all_waypoints is not None else waypoints_to_plot
    timed_reference = [wp for wp in reference if wp.get("time")]
    if not timed_reference:
        return {}
    origin_local = min(_gpx_time_to_local(wp["time"]) for wp in timed_reference)

    if start_time_str is not None:
        hh, mm = (int(x) for x in start_time_str.split(":"))
        base_dt = origin_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    else:
        base_dt = origin_local

    arrivals = {}
    for wp in waypoints_to_plot:
        if not wp.get("time"):
            continue
        wp_local = _gpx_time_to_local(wp["time"])
        elapsed = wp_local - origin_local
        arrivals[id(wp)] = base_dt + elapsed
    return arrivals


def _forecast_date_status(d):
    """(ok, kind) for whether date `d` falls within Open-Meteo's actual
    forecast coverage as fetched here (today..today+FORECAST_DAYS-1,
    Asia/Tokyo; fetch_open_meteo() as called here has no past_days, so
    anything outside that range doesn't error -- it silently comes back
    with zero matching hourly rows). kind is "past"/"future" when not ok,
    else None. Shared by resolve_route_target_date() (one date for the
    whole route) and render_route_weather_map()'s per-visit check (a
    multi-day trip's different visits can fall on different calendar
    dates, each needing its own check)."""
    today = date.today()
    max_date = today + timedelta(days=FORECAST_DAYS - 1)
    if d < today:
        return False, "past"
    if d > max_date:
        return False, "future"
    return True, None


def resolve_route_target_date(all_waypoints: list):
    """The calendar date (YYYY-MM-DD, Asia/Tokyo) a route weather map should
    fetch -- the GPX plan's own departure-point date (see
    compute_arrival_times()) -- plus a human-readable warning when that date
    can't actually be forecast (see _forecast_date_status()). Common cause
    of "past": the plan's climbing date has passed (running the diagnosis
    later than planned) without the GPX being re-exported.

    This assumes ONE date for the whole route -- right for a single-day
    trip, but a multi-day out-and-back's return leg lands on a later date
    than its outbound leg, which this function does not represent (it only
    looks at the earliest <time> in all_waypoints). render_route_weather_map()
    does NOT use this for that reason -- it resolves and range-checks each
    visit's own date individually instead (same _forecast_date_status()
    logic, applied per point). This function remains useful on its own
    (e.g. diagnose_route()'s single shared date, or a quick "what date
    would a single-day map use" check).

    Returns (target_date_str, warning_message) -- exactly one is None.
    Callers should print the warning and skip map generation rather than
    proceeding with an empty/partial result."""
    timed = [wp for wp in all_waypoints if wp.get("time")]
    if not timed:
        return None, "GPXに<time>タグを持つ地点がないため、対象日を判定できません。"

    target = min(_gpx_time_to_local(wp["time"]) for wp in timed).date()
    ok, kind = _forecast_date_status(target)
    if not ok:
        today = date.today()
        max_date = today + timedelta(days=FORECAST_DAYS - 1)
        if kind == "past":
            return None, (
                f"GPXの計画日({target.isoformat()})は既に過去の日付です。"
                f"Open-Meteoは過去の実況ではなく予報のみを扱うため、この日の地図は生成できません。"
                f"実際に登る日の計画書をヤマレコで作り直す(または既存の計画の登山日を更新する)か、"
                f"手元のGPXの<time>を実際の登山日に合わせて編集してから再度お試しください。"
            )
        return None, (
            f"GPXの計画日({target.isoformat()})は予報範囲({today.isoformat()}"
            f"〜{max_date.isoformat()})より先です。Open-Meteoは{FORECAST_DAYS}日先までしか"
            f"予報できないため、この日の地図はまだ生成できません。"
        )
    return target.isoformat(), None


def _closest_hour_row(day_rows: list, arrival_dt, fallback_time_str: str):
    """Row in day_rows (each has an "HH:MM" time string on the target date)
    whose time-of-day is closest to arrival_dt. Falls back to
    fallback_time_str (the route's start time) when arrival_dt is None (the
    waypoint had no GPX <time> tag to derive one from) or day_rows is empty
    -> None."""
    if not day_rows:
        return None
    target_hm = arrival_dt.strftime("%H:%M") if arrival_dt is not None else fallback_time_str
    th, tm = (int(x) for x in target_hm.split(":"))
    target_minutes = th * 60 + tm

    def minutes_of(r):
        rh, rm = (int(x) for x in r["time"].split(":"))
        return rh * 60 + rm

    return min(day_rows, key=lambda r: abs(minutes_of(r) - target_minutes))


def _lonlat_to_tile_xy(lon: float, lat: float, zoom: int):
    """Fractional (x, y) tile coordinates for a lon/lat at the given zoom --
    standard slippy-map (Web Mercator / EPSG:3857) tile math, the same
    scheme GSI's XYZ tile URLs use."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _auto_zoom(points: list) -> int:
    """Highest zoom (most detail) at which the waypoints' bounding box, plus
    MAP_PADDING_FRAC margin, still fits within MAP_MAX_CANVAS_PX -- so a
    tight cluster (e.g. 大菩薩嶺's 11 points within ~2km) renders zoomed in
    further than a longer, more spread-out route (唐松岳's ~2.5km ridge),
    without either blowing up the image size or crowding points into a
    handful of pixels. points: list of dicts with "lat"/"lon" keys."""
    lons = [p["lon"] for p in points]
    lats = [p["lat"] for p in points]
    for zoom in range(MAP_ZOOM_MAX, MAP_ZOOM_MIN - 1, -1):
        xs = [_lonlat_to_tile_xy(lon, lat, zoom)[0] for lon, lat in zip(lons, lats)]
        ys = [_lonlat_to_tile_xy(lon, lat, zoom)[1] for lon, lat in zip(lons, lats)]
        span_tiles = max(max(xs) - min(xs), max(ys) - min(ys), 0.001)
        span_px = span_tiles * TILE_SIZE_PX * (1 + 2 * MAP_PADDING_FRAC)
        if span_px <= MAP_MAX_CANVAS_PX or zoom == MAP_ZOOM_MIN:
            return zoom
    return MAP_ZOOM_MIN


def _fetch_tile(zoom: int, tx: int, ty: int):
    """One GSI tile PNG as a Pillow Image, disk-cached indefinitely (tiles
    are static basemap imagery -- unlike the forecast cache's 3h TTL, there's
    no freshness concern) under GSI_TILE_CACHE_DIR. Returns None (leaving
    that cell blank on the stitched canvas) rather than raising, so one
    failed/out-of-range tile doesn't abort the whole map."""
    from PIL import Image
    os.makedirs(GSI_TILE_CACHE_DIR, exist_ok=True)
    path = os.path.join(GSI_TILE_CACHE_DIR, f"pale_{zoom}_{tx}_{ty}.png")
    if os.path.exists(path):
        return Image.open(path).convert("RGBA")
    try:
        resp = requests.get(
            GSI_TILE_URL_TEMPLATE.format(z=zoom, x=tx, y=ty),
            timeout=10, headers={"User-Agent": "mountain-weather-personal-use/1.0"},
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    with open(path, "wb") as f:
        f.write(resp.content)
    return Image.open(path).convert("RGBA")


def _stitch_basemap(points: list, zoom: int):
    """Stitch the GSI tiles covering the waypoints' bounding box (+padding)
    into one Pillow Image. Returns (image, tile_x0, tile_y0) -- the latter
    two so callers can convert lon/lat -> pixel position on this image via
    _lonlat_to_pixel()."""
    from PIL import Image
    lons = [p["lon"] for p in points]
    lats = [p["lat"] for p in points]
    xs = [_lonlat_to_tile_xy(lon, lat, zoom)[0] for lon, lat in zip(lons, lats)]
    ys = [_lonlat_to_tile_xy(lon, lat, zoom)[1] for lon, lat in zip(lons, lats)]
    pad = max(max(xs) - min(xs), max(ys) - min(ys), 0.05) * MAP_PADDING_FRAC
    tile_x0, tile_x1 = math.floor(min(xs) - pad), math.ceil(max(xs) + pad)
    tile_y0, tile_y1 = math.floor(min(ys) - pad), math.ceil(max(ys) + pad)

    canvas = Image.new(
        "RGBA",
        ((tile_x1 - tile_x0 + 1) * TILE_SIZE_PX, (tile_y1 - tile_y0 + 1) * TILE_SIZE_PX),
        (255, 255, 255, 255),
    )
    for tx in range(tile_x0, tile_x1 + 1):
        for ty in range(tile_y0, tile_y1 + 1):
            tile = _fetch_tile(zoom, tx, ty)
            if tile is not None:
                canvas.paste(tile, ((tx - tile_x0) * TILE_SIZE_PX, (ty - tile_y0) * TILE_SIZE_PX))
    return canvas, tile_x0, tile_y0


def _lonlat_to_pixel(lon: float, lat: float, zoom: int, tile_x0: int, tile_y0: int):
    x, y = _lonlat_to_tile_xy(lon, lat, zoom)
    return (x - tile_x0) * TILE_SIZE_PX, (y - tile_y0) * TILE_SIZE_PX


def _load_jp_font(size: int):
    from PIL import ImageFont
    for path in JP_FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_weather_icon(draw, cx: float, cy: float, kind: str):
    """Simple filled-shape icon (not an emoji glyph -- Pillow has no reliable
    cross-platform support for color-emoji fonts, so a glyph-based sun/cloud
    emoji risks rendering as a tofu box or a flat outline depending on the
    font) at (cx, cy): a yellow sun with rays, a gray cloud blob, or a
    smaller cloud + blue raindrops, matching pick_weather_icon()'s 3-way
    classification."""
    r = ICON_RADIUS_PX
    if kind == "sunny":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 179, 0, 255), outline=(120, 80, 0, 255), width=2)
        for i in range(8):
            angle = math.radians(i * 45)
            x0, y0 = cx + math.cos(angle) * (r + 3), cy + math.sin(angle) * (r + 3)
            x1, y1 = cx + math.cos(angle) * (r + 8), cy + math.sin(angle) * (r + 8)
            draw.line((x0, y0, x1, y1), fill=(255, 179, 0, 255), width=2)
    elif kind == "cloudy":
        draw.ellipse((cx - r, cy - r * 0.7, cx + r * 0.4, cy + r * 0.6),
                      fill=(200, 200, 205, 255), outline=(110, 110, 115, 255), width=2)
        draw.ellipse((cx - r * 0.4, cy - r, cx + r, cy + r * 0.5),
                      fill=(220, 220, 225, 255), outline=(110, 110, 115, 255), width=2)
    else:  # rain
        draw.ellipse((cx - r, cy - r * 0.9, cx + r * 0.5, cy + r * 0.3),
                      fill=(180, 185, 190, 255), outline=(90, 90, 95, 255), width=2)
        draw.ellipse((cx - r * 0.5, cy - r * 1.1, cx + r, cy + r * 0.1),
                      fill=(195, 200, 205, 255), outline=(90, 90, 95, 255), width=2)
        for dx in (-r * 0.5, r * 0.1, r * 0.65):
            draw.line((cx + dx, cy + r * 0.5, cx + dx - 3, cy + r * 1.3), fill=(50, 110, 220, 255), width=3)


def _place_label_box(cx: float, cy: float, box_w: float, box_h: float, obstacles: list):
    """Greedy declutter: try a handful of offset slots around the icon
    (closest/most-natural first) and use the first one whose label box
    doesn't overlap any obstacle -- including THIS point's own icon (see
    render_route_weather_map()'s caller, which passes every point's icon
    footprint, itself included, plus every label box placed so far); falls
    back to the first slot if all are taken (a dense cluster just overlaps
    slightly rather than a point silently losing its label -- this is a
    lightweight placer, not a general layout solver, which is more
    machinery than an 8-11 point route needs). Returns
    ((bx0, by0, bx1, by1), (dx, dy)) -- the box and which offset slot was
    used (so the caller knows which edge to anchor the leader line to).
    Does NOT mutate obstacles -- the caller appends the returned box
    itself.

    Candidate geometry note: a rectangle overlap test only needs ONE axis
    (x or y) to be fully disjoint to guarantee no collision, regardless of
    the other axis -- so every candidate below is placed far enough along
    a single axis to clear this point's own icon (radius
    ICON_VISUAL_RADIUS_PX, i.e. including the sunny icon's rays) by
    construction, without having to know the label box's exact width/height
    up front: |dy|=44 clears vertically (icon radius ~21 + a label box's
    typical half-height ~17 + buffer); |dx|=85/130 clears horizontally
    (same icon radius + a label box's typical half-width ~55 + buffer).
    Earlier revisions used much smaller offsets and only checked against
    OTHER points' icons, never the point's own -- which let a label
    overlap/clip its own icon's rays (reported 2026-09); this fixes that by
    construction rather than by hoping the greedy search finds a clear slot
    among candidates that were never guaranteed to clear it."""
    candidates = [
        (0, -44), (34, -44), (-34, -44), (0, 44), (34, 44), (-34, 44), (85, 0), (-85, 0),
        # Second, farther ring -- only reached when the first ring is all taken
        # (a dense cluster, e.g. several 大菩薩嶺-scale waypoints within a few
        # hundred meters of each other at the auto-picked zoom).
        (0, -74), (50, -74), (-50, -74), (0, 74), (50, 74), (-50, 74), (130, 0), (-130, 0),
    ]

    def overlaps(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    for dx, dy in candidates:
        box = (cx + dx - box_w / 2, cy + dy - box_h / 2, cx + dx + box_w / 2, cy + dy + box_h / 2)
        if not any(overlaps(box, b) for b in obstacles):
            return box, (dx, dy)
    dx, dy = candidates[0]
    return (cx + dx - box_w / 2, cy + dy - box_h / 2, cx + dx + box_w / 2, cy + dy + box_h / 2), (dx, dy)


def _draw_label(draw, font, cx: float, cy: float, lines: list, obstacles: list):
    """Draw a small white-background label (name / arrival time + temp) near
    (cx, cy), offset via _place_label_box() to dodge obstacles (neighboring
    icons/labels), with a thin leader line back to the icon. Returns the
    placed box (bx0, by0, bx1, by1) -- the caller appends it to `obstacles`
    for subsequent points, since this function doesn't mutate the list
    itself."""
    # LABEL_PAD_PX is used both above the first line and below the last --
    # symmetric top/bottom whitespace, per user feedback (2026-09) that the
    # box originally looked bottom-heavy (text sitting right against the
    # bottom border). That asymmetry was because draw.text()'s y positions
    # the font's ascender line, not the glyphs' actual ink top -- so each
    # line's real ink started a few px below `ty`, eating into what should
    # have been bottom padding. Drawing at `ty - bbox_top` (bbox_top =
    # line_bboxes[i][1], the ink's offset from the nominal origin) aligns
    # the ink's actual top edge to `ty`, so the box_h math below (built from
    # the same ink-height bboxes) comes out symmetric in practice, not just
    # on paper.
    LABEL_PAD_PX = 5
    LABEL_LINE_GAP_PX = 2
    line_bboxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [b[3] - b[1] for b in line_bboxes]
    box_w = max(b[2] - b[0] for b in line_bboxes) + 8
    box_h = sum(line_heights) + LABEL_LINE_GAP_PX * (len(lines) - 1) + 2 * LABEL_PAD_PX

    (bx0, by0, bx1, by1), (dx, dy) = _place_label_box(cx, cy, box_w, box_h, obstacles)

    anchor_x = bx0 if dx > 0 else (bx1 if dx < 0 else (bx0 + bx1) / 2)
    anchor_y = by0 if dy < 0 else (by1 if dy > 0 else (by0 + by1) / 2)
    draw.line((cx, cy, anchor_x, anchor_y), fill=(90, 90, 90, 180), width=1)

    draw.rectangle((bx0, by0, bx1, by1), fill=(255, 255, 255, 225), outline=(120, 120, 120, 200), width=1)
    ty = by0 + LABEL_PAD_PX
    for line, bbox, h in zip(lines, line_bboxes, line_heights):
        draw.text((bx0 + 4, ty - bbox[1]), line, font=font, fill=(20, 20, 20, 255))
        ty += h + LABEL_LINE_GAP_PX
    return (bx0, by0, bx1, by1)


def _draw_route_line(draw, track_pixel_points: list):
    """Draw the trail itself as a pink polyline (ROUTE_LINE_COLOR) with a
    thin white halo underneath for contrast against the basemap's tan
    contour lines, connecting consecutive points in track_pixel_points
    (already offset to the cropped canvas's coordinate space). Call this
    before drawing icons/labels so the line sits underneath them, not on
    top."""
    if len(track_pixel_points) < 2:
        return
    draw.line(track_pixel_points, fill=ROUTE_LINE_HALO_COLOR, width=ROUTE_LINE_WIDTH_PX + 3, joint="curve")
    draw.line(track_pixel_points, fill=ROUTE_LINE_COLOR, width=ROUTE_LINE_WIDTH_PX, joint="curve")


def _draw_attribution(draw, canvas_size, font):
    w, h = canvas_size
    bbox = draw.textbbox((0, 0), GSI_TILE_ATTRIBUTION, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 4
    draw.rectangle((w - tw - 2 * pad - 4, h - th - 2 * pad - 4, w - 4, h - 4), fill=(255, 255, 255, 200))
    draw.text((w - tw - pad - 4, h - th - pad - 4), GSI_TILE_ATTRIBUTION, font=font, fill=(40, 40, 40, 255))


def _spread_duplicate_points(pixel_points: list):
    """Groups pixel_points (px, py, pt) by shared true coordinate (pt["lat"],
    pt["lon"]) -- e.g. a named waypoint visited on both the outbound and
    return leg of a multi-day out-and-back trip, which sit at the exact
    same lat/lon and would otherwise be drawn as two icons stacked
    perfectly on top of each other. Groups of 1 pass through unchanged
    (anchor=None -- the icon already sits exactly on its real coordinate,
    no leader line needed). For a group with >1 member, each member's
    plotted position is nudged out onto a small fixed ring around the
    shared true pixel position; anchor is that true position, so the
    caller can draw a short leader line from it to each member (and a
    small dot marking the real spot), making clear the icons are split
    apart from one shared location rather than independent points.

    Returns a new list of (px, py, pt, anchor) -- px/py is the (possibly
    offset) plotting position, anchor is (true_px, true_py) or None."""
    groups = {}
    for px, py, pt in pixel_points:
        groups.setdefault((pt["lat"], pt["lon"]), []).append((px, py, pt))

    # The first member of a group always sits at (0, 0) -- exactly the true
    # coordinate, no offset -- so the ring radius needed to clear it is
    # 2*ICON_VISUAL_RADIUS_PX (both icons' ray-tip radius, not just one)
    # plus a small buffer, not just ICON_VISUAL_RADIUS_PX alone (an earlier
    # revision used 30px here, sized against a single icon's radius, which
    # left two icons' rays overlapping -- caught visually on a real
    # multi-day map before this was corrected).
    RING_R = 2 * ICON_VISUAL_RADIUS_PX + 6
    OFFSET_RING = [
        (0, 0), (0, -RING_R), (RING_R, 0), (0, RING_R), (-RING_R, 0),
        (round(RING_R * 0.7), -round(RING_R * 0.7)), (-round(RING_R * 0.7), round(RING_R * 0.7)),
        (round(RING_R * 0.7), round(RING_R * 0.7)), (-round(RING_R * 0.7), -round(RING_R * 0.7)),
    ]

    out = []
    for members in groups.values():
        if len(members) == 1:
            px, py, pt = members[0]
            out.append((px, py, pt, None))
            continue
        true_px, true_py = members[0][0], members[0][1]
        for i, (_, _, pt) in enumerate(members):
            dx, dy = OFFSET_RING[i % len(OFFSET_RING)]
            out.append((true_px + dx, true_py + dy, pt, (true_px, true_py)))
    return out


def render_route_weather_map(waypoints: list, start_time: str = None,
                              out_dir: str = MAPS_DIR, all_waypoints: list = None,
                              track_points: list = None) -> str:
    """Render a PNG map (GSI 淡色地図 basemap) with one weather icon per
    waypoint, each placed using the weather at that point's *actual
    estimated arrival time* (compute_arrival_times()) rather than one
    forecast hour applied to the whole route. Each icon carries a small
    label (name / arrival time / temperature).

    start_time: None (default) uses the GPX plan's own <time> for every
    point as-is -- Yamareco's 登山計画書 <time> tags are the plan's actual
    designated date+time, not a placeholder (see _gpx_time_to_local()'s
    docstring), so the default is simply "trust the plan". Pass "HH:MM" to
    instead leave at that clock time, with every other point's arrival
    shifted by the same amount (see compute_arrival_times()) -- e.g.
    because you're actually departing earlier/later than the plan said,
    not because you're climbing on different days.

    Each waypoint's target date is resolved from ITS OWN arrival time (not
    one shared date for the whole route -- see _forecast_date_status()),
    so a multi-day trip's later-day visits are checked and fetched against
    their own calendar date. A waypoint whose resolved date is already
    past or beyond the forecast horizon is skipped with a printed message,
    same as a failed fetch -- the rest of the route still renders.

    waypoints MAY contain multiple entries for the same named place (pass
    parse_gpx_waypoints(gpx_path, dedupe=False) filtered to your selection
    -- see main_gpx_route()) -- e.g. a multi-day out-and-back's outbound
    and return visits to the same hut, each with its own <time>. Entries
    sharing a coordinate are detected and spread apart so both remain
    visible (_spread_duplicate_points()) rather than drawn on top of each
    other; pass the deduped list (parse_gpx_waypoints()'s default) for a
    single-visit-per-place map as before.

    all_waypoints: the full route (in file order, matching whatever
    dedupe= you used for waypoints), used as compute_arrival_times()'s
    departure-point anchor so it stays correct even when waypoints (what
    gets plotted) is a smaller user-selected subset. Defaults to waypoints
    when omitted.

    track_points: the route's actual trail shape, as (lat, lon) tuples in
    file order -- from parse_gpx_track_points(), which (unlike
    parse_gpx_waypoints()) keeps every trkpt, not just named ones, so the
    drawn line follows the real path (switchbacks, contour-hugging curves)
    instead of cutting straight lines between named waypoints. Optional --
    when omitted, no route line is drawn (e.g. for a hand-built waypoints
    list with no backing GPX track).

    Reuses fetch_waypoint_hourly() (the same per-point pipeline
    diagnose_route() uses), so the icons/temperatures shown are consistent
    with that function's text tables for the same waypoints/date. Needs
    Pillow -- see this section's banner comment for why that's a lazy
    import here, and for the GSI tile terms-of-use tradeoff this function's
    approach accepts.

    Returns the saved PNG's path, or None if no waypoint had usable data."""
    from PIL import ImageDraw

    arrivals = compute_arrival_times(waypoints, all_waypoints=all_waypoints, start_time_str=start_time)
    points = []
    for wp in waypoints:
        arrival_dt = arrivals.get(id(wp))
        if arrival_dt is None:
            print(f"{strip_furigana(wp['name'])}: GPXに時刻情報がないため地図から除外します。")
            continue
        ok, kind = _forecast_date_status(arrival_dt.date())
        if not ok:
            reason = "既に過去の日付" if kind == "past" else f"予報範囲({FORECAST_DAYS}日先まで)より先"
            print(f"{strip_furigana(wp['name'])}({arrival_dt.date().isoformat()}): "
                  f"計画日が{reason}のため地図から除外します。")
            continue
        try:
            _, _, day_rows = fetch_waypoint_hourly(wp, arrival_dt.date().isoformat())
        except requests.exceptions.RequestException as e:
            print(f"{strip_furigana(wp['name'])}: 取得に失敗したため地図から除外します: {e}")
            continue
        row = _closest_hour_row(day_rows, arrival_dt, arrival_dt.strftime("%H:%M"))
        if row is None:
            print(f"{strip_furigana(wp['name'])}: 対象日のデータがないため地図から除外します。")
            continue
        points.append({
            "wp": wp,
            "lat": wp["lat"],
            "lon": wp["lon"],
            "arrival": arrival_dt,
            "temp": row["temp_est"],
            "icon": pick_weather_icon(row["cloud_summit"], row["precip"], row["precip_mm"]),
        })

    if not points:
        print("地図に表示できる地点がありませんでした。")
        return None

    distinct_dates = sorted({p["arrival"].date() for p in points})
    multi_day = len(distinct_dates) > 1

    zoom = _auto_zoom(points)
    canvas, tile_x0, tile_y0 = _stitch_basemap(points, zoom)
    canvas = canvas.convert("RGBA")

    pixel_points = [
        (*_lonlat_to_pixel(pt["lon"], pt["lat"], zoom, tile_x0, tile_y0), pt) for pt in points
    ]
    xs = [p[0] for p in pixel_points]
    ys = [p[1] for p in pixel_points]
    crop_box = (
        max(0, min(xs) - MAP_CROP_MARGIN_PX),
        max(0, min(ys) - MAP_CROP_MARGIN_PX),
        min(canvas.width, max(xs) + MAP_CROP_MARGIN_PX),
        min(canvas.height, max(ys) + MAP_CROP_MARGIN_PX),
    )
    canvas = canvas.crop(tuple(int(v) for v in crop_box))
    draw = ImageDraw.Draw(canvas, "RGBA")
    ox, oy = crop_box[0], crop_box[1]

    if track_points:
        track_pixel_points = [
            (_lonlat_to_pixel(lon, lat, zoom, tile_x0, tile_y0)[0] - ox,
             _lonlat_to_pixel(lon, lat, zoom, tile_x0, tile_y0)[1] - oy)
            for lat, lon in track_points
        ]
        _draw_route_line(draw, track_pixel_points)

    icon_font = _load_jp_font(13)
    attribution_font = _load_jp_font(11)

    # Spread multi-visit points (same coordinate, different day/time) apart
    # before computing icon obstacle boxes/drawing, so a revisited place
    # gets two distinguishable icons instead of one hiding the other.
    spread_points = _spread_duplicate_points([(px - ox, py - oy, pt) for px, py, pt in pixel_points])

    # Anchor dots + leader lines for spread groups, drawn first (under
    # everything) so icons/labels sit on top of them cleanly.
    drawn_anchors = set()
    for cx, cy, pt, anchor in spread_points:
        if anchor is None or anchor in drawn_anchors:
            continue
        drawn_anchors.add(anchor)
        draw.ellipse((anchor[0] - 3, anchor[1] - 3, anchor[0] + 3, anchor[1] + 3),
                      fill=(90, 90, 90, 220), outline=(255, 255, 255, 230), width=1)
    for cx, cy, pt, anchor in spread_points:
        if anchor is not None:
            draw.line((anchor[0], anchor[1], cx, cy), fill=(100, 100, 100, 190), width=1)

    # Margin uses ICON_VISUAL_RADIUS_PX (icon circle + ray length), not just
    # ICON_RADIUS_PX -- otherwise a label could sit clear of the icon's
    # circle but still clip the sunny icon's rays.
    icon_margin = ICON_VISUAL_RADIUS_PX + 3
    icon_boxes = [
        (cx - icon_margin, cy - icon_margin, cx + icon_margin, cy + icon_margin)
        for cx, cy, pt, anchor in spread_points
    ]
    label_boxes = []
    for cx, cy, pt, anchor in spread_points:
        _draw_weather_icon(draw, cx, cy, pt["icon"])
        arrival_label = pt["arrival"].strftime("%H:%M")
        if multi_day:
            arrival_label = f'{pt["arrival"].month}/{pt["arrival"].day} {arrival_label}'
        lines = [strip_furigana(pt["wp"]["name"]), f'{arrival_label} {fmt(pt["temp"], 0)}\u2103']
        # Obstacles = every point's icon footprint (this point's own
        # included -- see _place_label_box()'s docstring for why its
        # candidate geometry clears its own icon by construction, so this
        # is defense-in-depth rather than load-bearing) + every label
        # already placed.
        obstacles = icon_boxes + label_boxes
        label_boxes.append(_draw_label(draw, icon_font, cx, cy, lines, obstacles))

    _draw_attribution(draw, canvas.size, attribution_font)
    if multi_day:
        date_label = f"対象日: {distinct_dates[0].isoformat()}〜{distinct_dates[-1].isoformat()}"
    else:
        date_label = f"対象日: {format_date_with_weekday(distinct_dates[0].isoformat())}"
    draw.rectangle((4, 4, draw.textbbox((0, 0), date_label, font=attribution_font)[2] + 12, 22),
                    fill=(255, 255, 255, 200))
    draw.text((8, 6), date_label, font=attribution_font, fill=(40, 40, 40, 255))

    os.makedirs(out_dir, exist_ok=True)
    date_stamp = f"{distinct_dates[0]}_{distinct_dates[-1]}" if multi_day else str(distinct_dates[0])
    out_path = os.path.join(out_dir, f"route_weather_{date_stamp}_{datetime.now().strftime('%H%M%S')}.png")
    canvas.convert("RGB").save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# ECMWF ensemble confidence score (2026-09 addition; detail.py's single-
# mountain diagnosis only -- see below for why mvp.py doesn't get this).
#
# mountain_climb_score's inputs all come from ONE deterministic run each of
# jma_msm (near-term, ~39h) and ecmwf_ifs025 (out to FORECAST_DAYS) --
# fetch_forecast()'s merge is completely unaffected by anything in this
# section. This section adds a SEPARATE, independent signal: how much do
# ECMWF's 50 perturbed ensemble members (+ 1 unperturbed control run --
# Open-Meteo's ensemble API returns that as the plain "cloudcover" etc. key
# alongside "cloudcover_memberNN" for NN=01..50, 51 runs total, matching
# ECMWF's operational ENS size) actually agree with each other for a given
# day? A day where all 51 runs roughly agree is a more trustworthy forecast
# than a day where they're scattered, even if the deterministic score
# looks identical on paper. This is NOT folded into mountain_climb_score
# itself (mountain_climb_score's inputs/weights are unchanged) -- it's
# printed as a separate "予報/確信度" line per day, for the reader to weigh
# alongside the score, not something the score already accounts for.
#
# Deliberately MSM-only excluded, ECMWF-only, per explicit design decision:
# jma_msm has no published ensemble product this project can use (JMA's
# real ensemble system, MEPS, requires a paid 気象業務支援センター feed and
# GRIB2 decoding -- out of scope for a personal/non-commercial tool), and
# folding an MSM confidence in would need a different, incompatible data
# source. The near-term (~39h) MSM-covered days stay purely deterministic,
# same as before.
#
# mvp.py (探索モード) does NOT get this: it already ranks 75 mountains x
# 3 models each run (see "生JSONキャッシュ層" in SKILL.md) and takes
# multiple minutes on a cold cache; adding a 51-member ensemble fetch per
# mountain would multiply that cost substantially for a broad-scan mode
# whose whole point is fast cross-mountain comparison, not a deep dive on
# one mountain. detail.py's single-mountain mode -- one mountain, already
# doing a deeper per-hour breakdown -- is where this fits. Revisit if a
# future need justifies the added cost.
ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODEL = "ecmwf_ifs025"
ENSEMBLE_MEMBER_COUNT = 50  # + the unperturbed control run = 51 total (see banner comment)
# How many of the nearest forecast days to skip before trusting a day as
# "ECMWF-only" -- jma_msm's ~39h reach covers today (day 0) and can still
# reach into tomorrow morning (day 1)'s ridge window (8-11h) depending on
# what time fetch_forecast() runs, and this module has no cheap way to
# tell, after fetch_forecast()'s merge, which hours actually came from MSM
# vs. the ECMWF fallback (the merge just keeps whichever was non-null).
# Skipping days 0-1 entirely is a deliberately conservative simplification
# rather than tracking that precisely.
CONFIDENCE_MIN_DAYS_OUT = 2
# Precipitation confidence's "did this member produce precipitation"
# threshold (mm/h) -- same order of magnitude as precip_penalty()'s/
# wet_chill_adjustment_c()'s own "is it actually wet" cutoffs elsewhere in
# this file (0.5mm/h), but lower: a per-member yes/no split is more
# sensitive to drizzle-level amounts than a single blended value, so a
# stricter (lower) threshold avoids calling a mostly-dry ensemble "wet".
ENSEMBLE_PRECIP_WET_THRESHOLD_MM = 0.1


def _ensemble_cache_path(lat: float, lon: float, days: int) -> str:
    # Deliberately a different filename shape than _cache_path() (which
    # fetch_open_meteo() uses) -- an "ecmwf_ifs025" cache entry from the
    # deterministic pipeline and one from here must never collide, since
    # the deterministic entry has no _memberNN keys the ensemble reader
    # expects.
    return os.path.join(CACHE_DIR, f"ensemble_{ENSEMBLE_MODEL}_{lat:.4f}_{lon:.4f}_{days}d.json")


def fetch_ensemble(lat: float, lon: float, hourly: list, days: int = FORECAST_DAYS,
                    force_refresh: bool = False) -> dict:
    """Cached fetch from Open-Meteo's Ensemble API -- independent of
    fetch_open_meteo()/fetch_forecast() (the deterministic MSM+ECMWF
    pipeline), per this section's banner comment. hourly: base variable
    names (e.g. "cloudcover_850hPa", "precipitation", "temperature_700hPa")
    -- each one expands, in the response, into that name plus
    "<name>_member01".."<name>_member50" (see banner comment); pass base
    names here, not member-suffixed ones.

    Caching mirrors fetch_open_meteo()'s design (same CACHE_TTL_SECONDS,
    same merge-in-missing-variables behavior) but keyed/stored separately
    (_ensemble_cache_path()) since the response shape (50x the columns per
    variable) is different enough to not share a cache file with the
    deterministic entries."""
    path = _ensemble_cache_path(lat, lon, days)
    cached = None if force_refresh else _load_cache(path)

    is_fresh = cached is not None and (time_module.time() - cached.get("_fetched_at", 0)) < CACHE_TTL_SECONDS
    have = set(cached.get("hourly", {}).keys()) if is_fresh else set()
    # A variable counts as "already cached" if its member01 column is
    # present -- cheaper than checking all 51 columns, and either the
    # whole variable was fetched together or none of it was.
    missing = [v for v in hourly if f"{v}_member01" not in have]

    if is_fresh and not missing:
        return cached

    fetch_vars = missing if is_fresh else hourly
    params = {
        "latitude": lat, "longitude": lon, "forecast_days": days,
        "timezone": "Asia/Tokyo", "models": ENSEMBLE_MODEL, "hourly": fetch_vars,
    }
    resp = get_with_retry(ENSEMBLE_API_URL, params)
    fresh = resp.json()

    if is_fresh:
        merged = cached
        merged.setdefault("hourly", {}).update(fresh.get("hourly", {}))
    else:
        merged = fresh
        merged.setdefault("hourly", {})

    merged["_fetched_at"] = time_module.time()
    _save_cache(path, merged)
    return merged


def _percentile(values: list, pct: float):
    """Linear-interpolated percentile (0-100) of values. None/empty ->
    None. Plain-Python implementation (sort + interpolate) rather than
    statistics.quantiles() -- this file has no other percentile need, and
    spelling it out avoids relying on quantiles()'s method= semantics
    matching what's intended here."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    k = (len(clean) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(clean) - 1)
    if f == c:
        return clean[f]
    return clean[f] + (clean[c] - clean[f]) * (k - f)


def cloudcover_confidence(std_pct) -> float:
    """0-100 confidence from the ensemble members' cloud-cover standard
    deviation (percentage points) -- smaller spread = more members agree =
    higher confidence. 0-40 percentage points of spread mapped linearly to
    100-0; 40 chosen as "members are all over the map" (a std this large
    means some members show near-clear sky and others near-total overcast)
    -- a tentative threshold, not derived from any statistical target,
    same status as mountain_climb_score's own penalty curves. Retune
    against real multi-day comparisons the way those were."""
    if std_pct is None:
        return 0.0
    return max(0.0, 100.0 - (std_pct / 40.0) * 100.0)


def precip_confidence(wet_fraction) -> float:
    """0-100 confidence from the fraction of ensemble members that produced
    precipitation >= ENSEMBLE_PRECIP_WET_THRESHOLD_MM at this hour.
    wet_fraction near 0 (nearly all members dry) or near 1 (nearly all
    members wet) means the members agree -> high confidence; near 0.5
    (an even split) means they disagree on whether it even rains at all
    -> low confidence, regardless of amount. Linear in distance from 0.5,
    scaled so 0.5 itself -> 0 and 0/1 -> 100."""
    if wet_fraction is None:
        return 0.0
    return max(0.0, min(100.0, abs(wet_fraction - 0.5) * 200.0))


def temp_confidence(p10_p90_width_c) -> float:
    """0-100 confidence from the ensemble members' 10th-90th percentile
    temperature spread (°C) -- narrower spread = higher confidence. 0-8°C
    mapped linearly to 100-0; 8°C chosen as a wide-enough spread to call
    the forecast temperature genuinely uncertain for deciding what to wear/
    whether wind chill matters. Tentative, same status as
    cloudcover_confidence()'s 40-point threshold -- retune against real
    data."""
    if p10_p90_width_c is None:
        return 0.0
    return max(0.0, 100.0 - (p10_p90_width_c / 8.0) * 100.0)


def combine_confidence(cloud_c, precip_c, temp_c) -> float:
    """Combines the three per-variable confidence scores into one number.
    Simple unweighted average, per this feature's design instructions
    ("単純平均または重み付き平均...重みは実データを見ながら調整") -- an
    equal-weights average was chosen as the starting point specifically
    because there's no real-data basis yet to justify weighting one
    variable's agreement over another's; revisit once there's a comparison
    (e.g. against てんきとくらす/Windy over several forecasts, the same way
    the "既知の精度傾向" section in SKILL.md was built) to weight by."""
    parts = [v for v in (cloud_c, precip_c, temp_c) if v is not None]
    if not parts:
        return 0.0
    return round(sum(parts) / len(parts), 1)


def compute_ensemble_confidence_by_day(lat: float, lon: float, cloud_var: str, temp_var: str,
                                        daily_sunrise: dict, days: int = FORECAST_DAYS) -> dict:
    """Per-day ECMWF ensemble confidence for one mountain's own
    elevation-matched pressure level (cloud_var/temp_var -- same variable
    names wind_vars_for_elevation()/temp_var_for_elevation() give the
    deterministic pipeline, so this reflects agreement on the SAME number
    the score is built from, not a generic surface reading). precipitation
    has no pressure-level variant (it's a column/surface quantity
    everywhere in this file, matching precip_penalty()'s own surface-only
    input), so it always uses the base "precipitation" variable.

    daily_sunrise: the same {date_str: ISO sunrise} dict compute_day_scores()
    uses (forecast["_daily_sunrise"]), passed in by the caller rather than
    fetched again here -- the deterministic fetch_forecast() call already
    happens earlier in main_single_mountain(), and sunrise is astronomical
    (model-independent), so there's no reason to hit the network twice for
    the same value.

    For each day (skipping the first CONFIDENCE_MIN_DAYS_OUT -- see that
    constant's comment) and each hour in the sunrise-relative ridge-dwell
    window (see MAIN_TIME_END_HOUR/RIDGE_DWELL_TRIM_HOURS's comment,
    matching the score's own ridge-window scope, since that's the window the
    score's cloud/temp inputs are drawn from), computes that hour's
    cloud/precip/temp confidence, then averages each across the window and
    combines them (combine_confidence()). Also
    returns a representative cloud%/precip_prob-proxy/precip_mm for the
    window -- built from the ensemble's own member spread (wet_fraction*100
    standing in for a precip probability, since the ensemble doesn't
    provide precipitation_probability directly), NOT from the deterministic
    pipeline's day_scores -- so pick_weather_icon() can label the day
    ("晴れ"/"曇り"/"雨") using data self-consistent with the confidence
    number sitting next to it, without needing changes to
    compute_day_scores() (which stays identical across mvp.py/detail.py,
    per SKILL.md -- touching it here would create exactly the kind of
    two-file drift that file warns against).

    Returns {date_str: {"confidence": ..., "cloud_confidence": ...,
    "precip_confidence": ..., "temp_confidence": ..., "cloud_pct": ...,
    "precip_prob": ..., "precip_mm": ...}}, only for days that pass the
    CONFIDENCE_MIN_DAYS_OUT cutoff and have at least one ridge-window hour
    of data."""
    ensemble = fetch_ensemble(lat, lon, [cloud_var, temp_var, "precipitation"], days=days)
    hourly = ensemble.get("hourly", {})
    times = hourly.get("time", [])

    cloud_members = [f"{cloud_var}_member{i:02d}" for i in range(1, ENSEMBLE_MEMBER_COUNT + 1)]
    temp_members = [f"{temp_var}_member{i:02d}" for i in range(1, ENSEMBLE_MEMBER_COUNT + 1)]
    precip_members = [f"precipitation_member{i:02d}" for i in range(1, ENSEMBLE_MEMBER_COUNT + 1)]

    cutoff_date = date.today() + timedelta(days=CONFIDENCE_MIN_DAYS_OUT)
    by_day = {}
    for idx, t in enumerate(times):
        t_dt = datetime.fromisoformat(t)
        if t_dt.date() < cutoff_date:
            continue
        day_str = t_dt.date().isoformat()
        sunrise_iso = daily_sunrise.get(day_str)
        if sunrise_iso is None:
            continue
        main_start = datetime.fromisoformat(sunrise_iso) + timedelta(hours=TRIP_START_OFFSET_HOURS)
        main_end = t_dt.replace(hour=MAIN_TIME_END_HOUR, minute=0, second=0, microsecond=0)
        ridge_start = main_start + timedelta(hours=RIDGE_DWELL_TRIM_HOURS)
        ridge_end = main_end - timedelta(hours=RIDGE_DWELL_TRIM_HOURS)
        if not (ridge_start <= t_dt < ridge_end):
            continue

        cloud_vals = [hourly[m][idx] for m in cloud_members if m in hourly and hourly[m][idx] is not None]
        temp_vals = [hourly[m][idx] for m in temp_members if m in hourly and hourly[m][idx] is not None]
        precip_vals = [hourly[m][idx] for m in precip_members if m in hourly and hourly[m][idx] is not None]

        cloud_std = statistics.pstdev(cloud_vals) if len(cloud_vals) >= 2 else None
        wet_fraction = (
            sum(1 for v in precip_vals if v >= ENSEMBLE_PRECIP_WET_THRESHOLD_MM) / len(precip_vals)
            if precip_vals else None
        )
        temp_width = None
        if len(temp_vals) >= 2:
            p10, p90 = _percentile(temp_vals, 10), _percentile(temp_vals, 90)
            if p10 is not None and p90 is not None:
                temp_width = p90 - p10

        e = by_day.setdefault(day_str, {
            "cloud_conf": [], "precip_conf": [], "temp_conf": [],
            "cloud_pct": [], "precip_prob": [], "precip_mm": [],
        })
        e["cloud_conf"].append(cloudcover_confidence(cloud_std))
        e["precip_conf"].append(precip_confidence(wet_fraction))
        e["temp_conf"].append(temp_confidence(temp_width))
        if cloud_vals:
            e["cloud_pct"].append(sum(cloud_vals) / len(cloud_vals))
        if wet_fraction is not None:
            e["precip_prob"].append(wet_fraction * 100)
        if precip_vals:
            e["precip_mm"].append(sum(precip_vals) / len(precip_vals))

    result = {}
    for day_str, e in by_day.items():
        cloud_c = safe_avg_or_none(e["cloud_conf"])
        precip_c = safe_avg_or_none(e["precip_conf"])
        temp_c = safe_avg_or_none(e["temp_conf"])
        result[day_str] = {
            "confidence": combine_confidence(cloud_c, precip_c, temp_c),
            "cloud_confidence": round(cloud_c, 1) if cloud_c is not None else None,
            "precip_confidence": round(precip_c, 1) if precip_c is not None else None,
            "temp_confidence": round(temp_c, 1) if temp_c is not None else None,
            "cloud_pct": round(sum(e["cloud_pct"]) / len(e["cloud_pct"]), 1) if e["cloud_pct"] else None,
            "precip_prob": round(sum(e["precip_prob"]) / len(e["precip_prob"]), 1) if e["precip_prob"] else None,
            "precip_mm": round(sum(e["precip_mm"]) / len(e["precip_mm"]), 1) if e["precip_mm"] else None,
        }
    return result


def print_ensemble_confidence_table(mtn: dict, confidence_by_day: dict):
    """予報/確信度 line per ECMWF-covered day -- see this section's banner
    comment for what "確信度" does and doesn't mean (a separate agreement
    signal, not folded into mountain_climb_score)."""
    if not confidence_by_day:
        print(f"\n(ECMWF確信度: {CONFIDENCE_MIN_DAYS_OUT}日先以降のデータがありませんでした)")
        return
    print(f"\n=== {mtn['name']}({mtn['elevation_m']}m) ECMWFアンサンブル確信度"
          f"({CONFIDENCE_MIN_DAYS_OUT}日先以降、稜線帯(日の出{TRIP_START_OFFSET_HOURS + RIDGE_DWELL_TRIM_HOURS:+.0f}h〜"
          f"{MAIN_TIME_END_HOUR - RIDGE_DWELL_TRIM_HOURS}時)、51メンバー) ===")
    print("(参考値: mountain_climb_scoreには含まれていません。予報のブレの大きさの目安です)\n")
    header = pad("日付", 18) + pad("予報", 8) + pad("確信度", 8) + pad("雲量/降水/気温内訳", 20)
    print(header)
    for day_str in sorted(confidence_by_day):
        c = confidence_by_day[day_str]
        icon_kind = pick_weather_icon(c["cloud_pct"], c["precip_prob"], c["precip_mm"])
        label = {"sunny": "晴れ", "cloudy": "曇り", "rain": "雨"}[icon_kind]
        breakdown = f'{fmt(c["cloud_confidence"], 0)}/{fmt(c["precip_confidence"], 0)}/{fmt(c["temp_confidence"], 0)}'
        row = (
            pad(format_date_with_weekday(day_str), 18) + pad(label, 8)
            + pad(fmt(c["confidence"], 0), 8) + pad(breakdown, 20)
        )
        print(row)


# ---------------------------------------------------------------------------
# JMA surface weather map screenshot (optional; not wired into main()).
# Separate from the Open-Meteo forecast pipeline above -- this is a Windy/SCW
# -style "eyeball the real synoptic chart" aid, not a data source the score
# functions consume. Needs Playwright + its Chromium binary, which is NOT
# part of this project's normal `pip install requests` dependency -- see
# SKILL.md's "運用上の注意点" before using this on a new machine.
# ---------------------------------------------------------------------------
JMA_WEATHER_MAP_URL = "https://www.jma.go.jp/bosai/weather_map/"
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


JMA_FORECAST_STEP_SUFFIXES = ["plus24h", "plus48h"]
# JMA's public weather map page's "ひとつ後の時間を表示" (step-forward)
# button, confirmed by testing (2026-09) live against the site, advances
# exactly two steps past 実況 -- to tomorrow's 09時の予想, then the day
# after's 09時の予想 -- and stops (a third click leaves the frame
# unchanged). This is JMA's standard public forecast surface chart product
# (24h/48h-ahead, fixed at 09時 JST), not an extended-range forecast; two
# is not an arbitrary self-imposed cap, it's what the page itself offers.


def fetch_jma_weather_map(out_dir: str = SCREENSHOT_DIR, include_forecast: bool = False):
    """Screenshot the JMA surface weather map's default view (real-time
    "実況" -- the page opens on this tab already, no tab-switching needed)
    and save it to out_dir as jma_weathermap_<YYYY-MM-DD>_<HHMM>.png
    (local-time capture timestamp).

    include_forecast=False (default): unchanged from before this option
    existed -- returns the saved 実況 file's path as a plain string.

    include_forecast=True (2026-09 addition): after the 実況 capture, also
    clicks JMA's own step-forward control (see JMA_FORECAST_STEP_SUFFIXES'
    comment) to capture its +24h and +48h 予想天気図 (forecast surface
    charts) -- in the SAME browser session/page load as the 実況 capture,
    so this is still "one site visit" for the 節度あるアクセス policy
    below even though it saves up to 3 files. Returns a list of saved
    paths in display order ([実況, +24h, +48h] normally; shorter if JMA's
    own control stops advancing earlier than the two steps above, which
    this checks for rather than assumes). Saved as
    jma_weathermap_<timestamp>_plus24h.png / _plus48h.png (same timestamp
    as that session's 実況 file, so the three sort/group together).

    The page renders the map via JS into an
    <img src="data/png/<timestamped filename>"> element rather than serving
    it at a fixed static URL, so a plain HTTP GET can't get today's map --
    that's why this needs a real browser (Playwright) rather than requests.
    Screenshotting just that <img> element (not the full page) crops out
    the menu/sidebar/ads for free and lands on the map's native resolution
    (600x581px as of 2026-09).

    Not wired into main(); call directly, e.g.:

        import mountain_weather_detail as m
        path = m.fetch_jma_weather_map()
        paths = m.fetch_jma_weather_map(include_forecast=True)

    Be a considerate caller: this hits JMA's live site, not our own cached
    Open-Meteo layer. Don't call this in a loop or on every script run --
    once per diagnosis session is plenty (see SKILL.md).
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = os.path.join(out_dir, f"jma_weathermap_{timestamp}.png")
    saved_paths = [out_path]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.goto(JMA_WEATHER_MAP_URL, wait_until="networkidle", timeout=30000)
            page.wait_for_selector("img[src^='data/png/']", timeout=15000)
            map_img = page.query_selector("img[src^='data/png/']")
            # .complete/.naturalWidth, not just wait_for_selector -- the <img>
            # tag exists in the DOM before its src has actually finished
            # loading; screenshotting too early risks a half-rendered frame.
            page.wait_for_function(
                "(el) => el.complete && el.naturalWidth > 0", arg=map_img, timeout=15000
            )
            map_img.screenshot(path=out_path)

            if include_forecast:
                # title=, not a hardcoded pixel position -- confirmed via the
                # live page's DOM (2026-09) to uniquely identify the forward
                # (not backward, not the separate movie/animation-play)
                # control regardless of its on-screen layout.
                forward_button = page.query_selector("img[title='ひとつ後の時間を表示']")
                for suffix in JMA_FORECAST_STEP_SUFFIXES:
                    if forward_button is None:
                        break
                    prev_src = map_img.get_attribute("src")
                    forward_button.click()
                    page.wait_for_timeout(1500)
                    if map_img.get_attribute("src") == prev_src:
                        break  # JMA's own control stopped advancing -- nothing further to capture
                    page.wait_for_function(
                        "(el) => el.complete && el.naturalWidth > 0", arg=map_img, timeout=15000
                    )
                    forecast_path = os.path.join(out_dir, f"jma_weathermap_{timestamp}_{suffix}.png")
                    map_img.screenshot(path=forecast_path)
                    saved_paths.append(forecast_path)
        finally:
            browser.close()

    return saved_paths if include_forecast else out_path


def main_single_mountain():
    print_mountain_list()
    mtn = prompt_selection()

    wind_hpa, wind_speed_var, wind_dir_var = wind_vars_for_elevation(mtn["elevation_m"])
    temp_var = temp_var_for_elevation(mtn["elevation_m"])
    extra_vars = ([wind_speed_var, wind_dir_var, temp_var, f"cloudcover_{wind_hpa}hPa"]
                  + moisture_vars_for_summit(wind_hpa)
                  + list(WIND_SPEED_BAND_VARS.values()) + list(WIND_DIR_BAND_VARS.values()))

    try:
        forecast = fetch_forecast(mtn["lat"], mtn["lon"], extra_vars=extra_vars)
    except requests.exceptions.RequestException as e:
        print(f"取得に失敗しました: {e}")
        return

    day_scores = compute_day_scores(forecast, wind_speed_var, wind_hpa, temp_var)
    print_day_score_table(mtn, day_scores)

    rows = build_hourly_rows(forecast, wind_speed_var, wind_dir_var, summit_hpa=wind_hpa, temp_var=temp_var)
    print_hourly_table(mtn, rows)

    opportunities = detect_cloud_sea_opportunity(forecast, wind_hpa)
    print_cloud_sea_opportunities(mtn, opportunities)

    print_weather_transitions(mtn, rows)

    try:
        confidence_by_day = compute_ensemble_confidence_by_day(
            mtn["lat"], mtn["lon"], f"cloudcover_{wind_hpa}hPa", temp_var, forecast["_daily_sunrise"]
        )
        print_ensemble_confidence_table(mtn, confidence_by_day)
    except requests.exceptions.RequestException as e:
        print(f"\n(ECMWF確信度の取得に失敗しました、点予報の表示には影響ありません: {e})")


def main_gpx_route():
    gpx_path = prompt_gpx_path()
    waypoints = parse_gpx_waypoints(gpx_path)
    if not waypoints:
        print("waypoints(<name>付きの地点)が見つかりませんでした。")
        return

    print_waypoint_list(waypoints)
    selected = prompt_waypoint_selection(waypoints)
    target_date = prompt_target_date()

    print(f"\n=== ルート診断: {format_date_with_weekday(target_date)} ===")
    diagnose_route(selected, target_date)

    make_map = input("\n地図PNG(天気アイコン付き)を生成しますか? (y/N): ").strip().lower()
    if make_map == "y":
        start_time_raw = input(
            "出発時刻を入力 (例: 05:00)。"
            "Enterのみでヤマレコの計画書に入っている日時をそのまま使用: "
        ).strip()
        start_time = start_time_raw or None
        try:
            track_points = parse_gpx_track_points(gpx_path)
            # dedupe=False: a multi-day out-and-back revisits the same named
            # place on a later date (see parse_gpx_waypoints()'s docstring
            # and render_route_weather_map()'s multi-visit support) -- the
            # map should show both visits, not just diagnose_route()'s
            # single outbound-leg row. Filtered to the same names the user
            # selected above (selection was made against the deduped list,
            # so indices don't apply here -- names do).
            selected_names = {wp["name"] for wp in selected}
            all_visits = parse_gpx_waypoints(gpx_path, dedupe=False)
            map_waypoints = [wp for wp in all_visits if wp["name"] in selected_names]
            path = render_route_weather_map(map_waypoints, start_time=start_time, all_waypoints=all_visits,
                                             track_points=track_points)
        except ImportError:
            print("Pillowがインストールされていません。`pip install Pillow` を実行してください。")
            return
        if path:
            print(f"地図を保存しました: {path}")


def main():
    print("\n=== モードを選んでください ===")
    print("1: 山を1つ選んで診断")
    print("2: GPX登山計画書からルート診断(waypoints)")
    mode = input("番号を入力 (1-2): ").strip()

    if mode == "2":
        main_gpx_route()
    else:
        main_single_mountain()


if __name__ == "__main__":
    main()
