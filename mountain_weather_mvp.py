"""
Mountain Weather Dashboard - MVP script
----------------------------------------
Fetches hourly forecast data (cloud cover, precip, wind, temperature, CAPE)
from the free Open-Meteo API for a shortlist of mountains, and ranks them by
a climbing-oriented score combining thunderstorm risk, ridge wind, ridge
visibility, precipitation, and wind-chill across three time windows: AM
(climb), ridge/summit dwell, and PM (descent, weighted toward afternoon
convective/thunderstorm risk). See `mountain_climb_score()`.

Shares its mountain pool, cache layer, and scoring formula with
mountain_weather_detail.py via mountain_weather_core.py (see that module's
docstring for exactly what's shared vs. kept separate).

Requirements:
    pip install requests

Usage:
    python mountain_weather_mvp.py
"""

import requests
from datetime import date, datetime, timedelta

from mountain_weather_core import (
    MOUNTAINS, pad,
    FIXED_ALTITUDE_BANDS_M, ALTITUDE_BAND_HPA, BAND_VARS, WIND_SPEED_BAND_VARS,
    nearest_band, KMH_TO_MS, wind_chill_c, safe_avg, safe_avg_or_none,
    SCORE_WEIGHTS, mountain_climb_score,
    mountain_hazards,
    format_date_with_weekday,
    PRECIP_TIMING_UNCERTAIN_DAYS_OUT, PRECIP_TIMING_BOUNDARY_LOW,
    PRECIP_TIMING_BOUNDARY_HIGH, PRECIP_TIMING_NOTE,
    fetch_open_meteo,
)

# ---------------------------------------------------------------------------
# Step 2: Filters — edit these to narrow the output
#   REGION_FILTER: list of regions to include, e.g. ["関東甲信"]. Empty = all.
#   TOP_N_PER_DAY: how many mountains to show per date
# ---------------------------------------------------------------------------
REGION_FILTER: list[str] = []
TOP_N_PER_DAY = None  # None = show all mountains that clear MIN_SCORE_THRESHOLD; or set an int to cap it
MIN_SCORE_THRESHOLD = 80.0  # see mountain_climb_score(): thunder/wind/cloud/precip/temp blend; below this, exclude

# ---------------------------------------------------------------------------
# Pressure levels / fixed altitude bands / cache layer: shared with
# mountain_weather_detail.py, see mountain_weather_core.py (imported above).
# ---------------------------------------------------------------------------

# Evaluation windows per day, matching the phases of a typical day hike
# rather than just "the morning" -- all now anchored to sunrise/sunset
# (2026-09 redesign, see each constant's own comment below) rather than the
# original fixed clock hours:
#   ACTIVITY (登り〜下山、降水判定) -- sunrise-relative start, sunset+grace
#                       end. Precip across the whole climb-through-descent
#                       span.
#   RIDGE (稜線帯滞在)  -- sunrise-relative, narrower "main climbing time"
#                       window. Cloud/visibility/wind-chill at the altitude
#                       band nearest the summit, i.e. what you'd actually
#                       feel while up there.
#   PM (下山/対流リスク) -- starts at a fixed clock hour, ends at the same
#                       sunset+grace line as ACTIVITY. Convective
#                       thunderstorm risk (CAPE) typically builds through
#                       the afternoon; scoring this window is what makes the
#                       classic "early start, early finish" advice show up
#                       as a lower score for days where afternoon storms are
#                       likely, even if the AM/ridge periods look clear.
TRIP_START_OFFSET_HOURS = -1
PM_START_HOUR = 12

# Ridge-dwell window (2026-09 redesign, sunrise-relative -- replaces the old
# fixed RIDGE_START_HOUR=8/RIDGE_END_HOUR=11 clock hours, which gave only 3
# hourly forecast points and, being unrelated to sunrise, drifted out of sync
# with when a climber is actually near the ridge/summit as day length changes
# across the season).
#
# Built from a "main climbing time" concept -- the user's own framing:
# 「日の出から14時ころまでが登山のメインタイム」(from sunrise to around 14時
# is the main climbing window) -- trimmed by RIDGE_DWELL_TRIM_HOURS on each
# end (the user's own proposal) to exclude the still-climbing-up start and
# the already-descending end, leaving the middle stretch when the climber is
# actually on/near the ridge:
#   main_start = sunrise + TRIP_START_OFFSET_HOURS (same anchor as the
#                activity window's own start)
#   main_end   = MAIN_TIME_END_HOUR (fixed clock hour, 14時 -- deliberately
#                NOT sunset-relative like the activity/PM windows: those were
#                widened specifically to keep catching afternoon/evening
#                hazards (唐松岳9/6), a different job from "when is the view
#                worth having," which this window is for)
#   ridge_start = main_start + RIDGE_DWELL_TRIM_HOURS
#   ridge_end   = main_end - RIDGE_DWELL_TRIM_HOURS
# With RIDGE_DWELL_TRIM_HOURS=2 this comes out to roughly sunrise+1h through
# a fixed 12:00 (main_end - 2h, itself fixed since main_end is a fixed clock
# hour) -- about 5-6 hours and 5-6 hourly points in September, versus the old
# window's fixed 3. Confirmed against real September sunrise/sunset data
# (2026-09-08 session) before adopting: e.g. sunrise 05:25 -> main time
# 04:25-14:00 -> ridge window 06:25-12:00 (5.6h). Widening this window also
# gives any future duration/ratio-based diagnostic (e.g. a ClearViewRatio,
# see the ViewQuality investigation from this same session) far more hourly
# points to work with than the old 3-point window could ever provide.
MAIN_TIME_END_HOUR = 14
RIDGE_DWELL_TRIM_HOURS = 2

# Activity/PM window end line (2026-09 redesign, sunset-based -- replaces the
# separate fixed ACTIVITY_END_HOUR=19/PM_END_HOUR=17 clock hours this project
# used to carry). Both of those were fixed clock hours chosen when this file
# had no daily sunset data of its own (see fetch_forecast()'s docstring for
# the sunset fetch this redesign added) -- fine near the equinox, but wrong
# by 2+ hours at the solstices (9月の日没は18時前後・12月は16時半ごろ・7月は
# 19時近く): a 19時 cutoff in December scored well after dark as still
# "activity time," while a 17時 cutoff in July would have re-created the
# original 唐松岳9/6 bug this project already fixed once (see the git history
# of this constant) by clipping off real 15-18時 danger hours whenever
# sunset falls after 17時.
#
# ACTIVITY_END_GRACE_MINUTES=30 (not a bare sunset cutoff) accounts for a
# descent that's already underway continuing a bit past sunset into civil
# twilight (still-usable ambient light, no headlamp needed yet) rather than
# snapping to "activity" at the very instant of sunset -- 30 minutes was a
# starting judgment call, not a fitted value, easy to retune.
#
# The two former windows are now ONE end line, not two: PM_END_HOUR and
# ACTIVITY_END_HOUR never had a real reason to differ (both were "roughly
# when the climbing day ends," just approximated with different round
# numbers at different times) -- see window_scores_by_day()'s docstring for
# where this single line now governs both the precip/"activity" window and
# the PM cloud/CAPE window.
ACTIVITY_END_GRACE_MINUTES = 30
WET_HOUR_PRECIP_THRESHOLD_PCT = 50.0  # an hour "counts as rain" at/above this probability

# Model priority: we query jma_msm and best_match separately, then merge
# per-hour, preferring jma_msm wherever it has data (roughly the first few
# days) and falling back to best_match beyond that or wherever MSM is null.
# precipitation (mm) and cape (convective available potential energy, a
# standard thunderstorm-risk proxy) are new vs. the original MVP scoring.
HOURLY_VARS = ["cloudcover", "cloudcover_low", "cloudcover_mid",
               "cloudcover_high", "precipitation_probability",
               "precipitation", "cape"]

# Visibility (m) isn't served for the named jma_msm/ecmwf_ifs025 models
# (comes back all-null) -- only for Open-Meteo's blended "best_match", the
# same restriction mountain_weather_detail.py already works around for
# freezing_level_height. So it's fetched via a third, separate call
# (model=None) in fetch_forecast() and merged in by time, rather than being
# part of HOURLY_VARS above.
VISIBILITY_VAR = "visibility"

# Temperature at the same fixed 1000/2000/3000m bands used for cloud cover
# (BAND_VARS, from core) -- kept on the same fixed-band grid (rather than
# each mountain's own exact elevation, as detail.py does) so every mountain
# can still be fetched with one shared variable set. mvp.py-only: detail.py
# derives temperature from the selected mountain's own nearest pressure
# level instead (see temp_var_for_elevation() there).
TEMP_BAND_VARS = {alt: f"temperature_{hpa}hPa" for alt, hpa in ALTITUDE_BAND_HPA.items()}


def fetch_single_model(lat: float, lon: float, model, days: int, with_sunrise: bool = False,
                        extra_hourly: list = None) -> dict:
    hourly_vars = (HOURLY_VARS + list(BAND_VARS.values())
                   + list(WIND_SPEED_BAND_VARS.values()) + list(TEMP_BAND_VARS.values()))
    if extra_hourly:
        hourly_vars = hourly_vars + extra_hourly
    daily_vars = ["sunrise", "sunset"] if with_sunrise else None
    return fetch_open_meteo(lat, lon, hourly=hourly_vars, daily=daily_vars, days=days, model=model)


def fetch_forecast(lat: float, lon: float, days: int = 15) -> dict:
    """Fetch hourly surface cloud cover/precip/cape plus cloud cover, wind
    speed and temperature at the fixed 1000/2000/3000m altitude bands (same
    bands for every mountain) -- merging JMA MSM (preferred, where available)
    with ECMWF IFS 0.25° (fallback for hours MSM doesn't cover, up to 15
    days). Also fetches daily sunrise/sunset times (astronomical, not
    model-dependent) from the ECMWF call, since it covers the full day range
    we need -- sunset (2026-09 addition) is what ACTIVITY_END_GRACE_MINUTES
    is now measured from (see that constant's comment), replacing the old
    fixed-clock-hour ACTIVITY_END_HOUR/PM_END_HOUR. Visibility is fetched via
    a third, separate best_match call
    (model=None) since jma_msm/ecmwf_ifs025 return null for it -- see
    VISIBILITY_VAR's comment. All three calls go through fetch_single_model
    -> fetch_open_meteo, so repeat runs within CACHE_TTL_SECONDS hit the
    on-disk cache/ instead of the network -- see that function's docstring."""
    all_vars = (HOURLY_VARS + list(BAND_VARS.values())
                + list(WIND_SPEED_BAND_VARS.values()) + list(TEMP_BAND_VARS.values()))

    msm = fetch_single_model(lat, lon, "jma_msm", days)
    fallback = fetch_single_model(lat, lon, "ecmwf_ifs025", days, with_sunrise=True)
    best = fetch_single_model(lat, lon, None, days, extra_hourly=[VISIBILITY_VAR])

    # Merge per-timestamp: prefer MSM's value when present and not null,
    # otherwise use best_match's value for that same timestamp.
    fallback_index = {t: i for i, t in enumerate(fallback["hourly"]["time"])}
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

    best_index = {t: i for i, t in enumerate(best["hourly"]["time"])}
    vis_values = best["hourly"].get(VISIBILITY_VAR, [])
    merged_hourly[VISIBILITY_VAR] = [
        vis_values[j] if (j := best_index.get(t)) is not None and j < len(vis_values) else None
        for t in msm["hourly"]["time"]
    ]

    daily_sunrise = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunrise"]))
    daily_sunset = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunset"]))
    return {"hourly": merged_hourly, "_daily_sunrise": daily_sunrise, "_daily_sunset": daily_sunset}


def window_scores_by_day(forecast: dict) -> dict:
    """Compute per-day activity / ridge / PM window summaries, returned as
    {date_str: {"activity": {...}, "ridge": {...}, "pm": {...}, "day_peak": {...}}}.

    - activity: precip over the sunrise-relative climb-through-descent span
      (AM start = TRIP_START_OFFSET_HOURS's start, through sunset +
      ACTIVITY_END_GRACE_MINUTES -- 2026-09 redesign, replaces the old fixed
      ACTIVITY_END_HOUR clock time; see that constant's comment). Reported as
      wet_fraction_pct (see WET_HOUR_PRECIP_THRESHOLD_PCT's comment) -- what
      fraction of that span's hours cross
      WET_HOUR_PRECIP_THRESHOLD_PCT -- plus peak_mm, the highest hourly
      accumulation in the same span. Replaces the old separate AM-average /
      PM-peak probability split: a climber doesn't experience "the AM
      average was low" or "the PM peak was high" as isolated numbers, they
      experience how much of the day was actually wet, which is exactly
      what a single duration-based window is built to answer, without
      needing to reconcile two different windows' worth of numbers.
    - ridge: cloud cover, wind speed and temperature at each fixed altitude
      band, averaged over the sunrise-relative ridge-dwell window (see
      MAIN_TIME_END_HOUR/RIDGE_DWELL_TRIM_HOURS's comment) -- what the
      ridge/summit dwell actually feels like.
    - pm: peak (not average) CAPE over the PM_START_HOUR-to-sunset+grace
      window (PM_END_HOUR, a fixed clock hour, was retired in the same
      2026-09 redesign as ACTIVITY_END_HOUR -- see that constant's comment;
      the two former end lines are now one). Peak, not average, because
      thunderstorm risk is a
      threshold hazard -- a 2-hour spike matters even if the window's mean
      looks tame. Also carries this same window's PEAK (not average)
      per-band cloud cover, for the same reason (2026-09 addition,
      confirmed against a real incident: 唐松岳 9/6 scored ~100 because the
      AM climb window and ridge-dwell window were both dry and clear, while
      rain moved in from midday through the descent). main() takes
      max(ridge, pm) for cloud before calling mountain_climb_score(), so a
      bad PM cloud-up can no longer hide behind a good ridge-window average.
      (Precip used to work the same max(am, pm) way here too, until the
      2026-09 duration-based redesign above replaced both with the single
      "activity" window -- pm no longer carries precip fields.)
    - day_peak: per band, the single highest wind speed (km/h) and the hour
      it occurred at, over the wider AM-start..sunset+grace span (not just the
      ridge window). ridge_wind_ms above is an 8-11 average and can smooth
      away a real gust that happens outside that window -- e.g. a pre-dawn
      squall a 4am early-starter would actually walk into. This doesn't
      feed mountain_climb_score (the ridge-window design is intentional,
      see wind_penalty's docstring); main() uses it only to flag
      WIND_PEAK_WARNING_MS+ gusts sitting outside the scored window.
    - day_total_precip_mm (2026-09 addition): the full calendar day's
      (00:00-23:59 local) summed precipitation, unconditionally -- NOT
      windowed to AM/PM/ridge like everything else here. am's precip_mm
      (what the score itself uses, via precip_penalty) only looks at the AM
      climb window by design -- the right scope for "can I climb in this"
      -- but a reader glancing only at am precip_prob/the score can come
      away thinking a day is fine when heavy rain is actually falling most
      of the day outside that window (confirmed against a real Windy/MSM
      comparison, 2026-09: an AM precip probability of ~50% coexisted with
      a ~114mm calendar-day total). Same reasoning as day_peak above -- a
      hazard the score's own window doesn't cover still deserves to be
      visible somewhere, without folding it into mountain_climb_score
      itself (unchanged weights/inputs). Identical field, same rationale,
      as detail.py's compute_day_scores()."""
    times = forecast["hourly"]["time"]
    precip_prob_series = forecast["hourly"]["precipitation_probability"]
    precip_mm_series = forecast["hourly"]["precipitation"]
    cape_series = forecast["hourly"]["cape"]
    cloud_band_series = {alt: forecast["hourly"][var] for alt, var in BAND_VARS.items()}
    wind_band_series = {alt: forecast["hourly"][var] for alt, var in WIND_SPEED_BAND_VARS.items()}
    temp_band_series = {alt: forecast["hourly"][var] for alt, var in TEMP_BAND_VARS.items()}
    visibility_series = forecast["hourly"][VISIBILITY_VAR]
    daily_sunrise = forecast["_daily_sunrise"]
    daily_sunset = forecast["_daily_sunset"]

    activity_by_day: dict = {}
    ridge_by_day: dict = {}
    pm_by_day: dict = {}
    day_by_day: dict = {}
    day_precip_by_day: dict = {}

    for idx, t in enumerate(times):
        day_str = t.split("T")[0]
        t_dt = datetime.fromisoformat(t)

        # Unconditional (no window gate) -- every hour of the calendar day,
        # unlike activity_by_day/ridge_by_day/pm_by_day below.
        if precip_mm_series[idx] is not None:
            day_precip_by_day[day_str] = day_precip_by_day.get(day_str, 0.0) + precip_mm_series[idx]

        # activity_end (2026-09 redesign): sunset + ACTIVITY_END_GRACE_MINUTES,
        # the single end line that replaced the old fixed ACTIVITY_END_HOUR
        # (precip) / PM_END_HOUR (CAPE/cloud, wind-peak span) clock hours --
        # see ACTIVITY_END_GRACE_MINUTES's comment. Requires actual sunset
        # data for day_str; skip this hour's activity/PM/day-peak
        # accumulation (not the whole day -- other hours of the same day_str
        # still get their own sunset_iso lookup) if that's missing, same
        # graceful-degradation behavior the old sunrise_iso check already had.
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
        if main_start is not None:
            main_end = t_dt.replace(hour=MAIN_TIME_END_HOUR, minute=0, second=0, microsecond=0)
            ridge_start = main_start + timedelta(hours=RIDGE_DWELL_TRIM_HOURS)
            ridge_end = main_end - timedelta(hours=RIDGE_DWELL_TRIM_HOURS)
            if ridge_start <= t_dt < ridge_end:
                entry = ridge_by_day.setdefault(
                    day_str,
                    {**{f"cloud_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M},
                     **{f"wind_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M},
                     **{f"temp_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M},
                     "visibility": []},
                )
                for alt in FIXED_ALTITUDE_BANDS_M:
                    entry[f"cloud_{alt}m"].append(cloud_band_series[alt][idx])
                    entry[f"wind_{alt}m"].append(wind_band_series[alt][idx])
                    entry[f"temp_{alt}m"].append(temp_band_series[alt][idx])
                # Visibility is a single surface-ish value at the mountain's
                # own coordinates (Open-Meteo doesn't serve it per
                # pressure-level band like cloud/wind/temp), so it isn't
                # split by altitude band.
                entry["visibility"].append(visibility_series[idx])

        if main_start is not None and activity_end is not None:
            if main_start <= t_dt < activity_end:
                entry = activity_by_day.setdefault(day_str, {"precip_prob": [], "precip_mm": []})
                entry["precip_prob"].append(precip_prob_series[idx])
                entry["precip_mm"].append(precip_mm_series[idx])

                # Wind-peak detection shares the exact same AM-start..
                # activity_end span as the precip window above (both used to
                # independently approximate "the climbing day," now that
                # PM_END_HOUR/ACTIVITY_END_HOUR are unified there's no reason
                # left for them to differ) -- narrower than nothing, wider
                # than the RIDGE window alone used for scoring.
                entry = day_by_day.setdefault(day_str, {f"wind_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M})
                for alt in FIXED_ALTITUDE_BANDS_M:
                    entry[f"wind_{alt}m"].append((wind_band_series[alt][idx], t_dt.hour))

        if activity_end is not None and PM_START_HOUR <= t_dt.hour and t_dt < activity_end:
            entry = pm_by_day.setdefault(
                day_str,
                {"cape": [], **{f"cloud_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M}},
            )
            entry["cape"].append(cape_series[idx])
            for alt in FIXED_ALTITUDE_BANDS_M:
                entry[f"cloud_{alt}m"].append(cloud_band_series[alt][idx])

    summary = {}
    for day_str in set(activity_by_day) | set(ridge_by_day) | set(pm_by_day):
        activity_vals = activity_by_day.get(day_str, {"precip_prob": [], "precip_mm": []})
        ridge_vals = ridge_by_day.get(day_str, {})
        pm_vals = pm_by_day.get(day_str, {"cape": []})

        ridge = {}
        for alt in FIXED_ALTITUDE_BANDS_M:
            ridge[f"cloud_{alt}m"] = safe_avg(ridge_vals.get(f"cloud_{alt}m", []))
            ridge[f"wind_{alt}m"] = safe_avg(ridge_vals.get(f"wind_{alt}m", []))
            ridge[f"temp_{alt}m"] = safe_avg(ridge_vals.get(f"temp_{alt}m", []))
        ridge["visibility"] = safe_avg_or_none(ridge_vals.get("visibility", []))

        day_vals = day_by_day.get(day_str, {})
        day_peak = {}
        for alt in FIXED_ALTITUDE_BANDS_M:
            clean = [(w, h) for w, h in day_vals.get(f"wind_{alt}m", []) if w is not None]
            if clean:
                wind_kmh, hour = max(clean, key=lambda pair: pair[0])
                day_peak[alt] = {"wind_ms": wind_kmh * KMH_TO_MS, "hour": hour}
            else:
                day_peak[alt] = {"wind_ms": None, "hour": None}

        cape_clean = [v for v in pm_vals["cape"] if v is not None]
        # PM still uses the window's PEAK (not average) for cloud -- same
        # reasoning CAPE already used (a threshold hazard can hide behind a
        # mild average). Precip used to work the same way here, until the
        # 2026-09 duration-based redesign moved it to the "activity" window
        # below (see window_scores_by_day's docstring and
        # ACTIVITY_END_GRACE_MINUTES's comment).
        pm = {"cape": max(cape_clean) if cape_clean else None}
        for alt in FIXED_ALTITUDE_BANDS_M:
            clean_cloud = [v for v in pm_vals.get(f"cloud_{alt}m", []) if v is not None]
            pm[f"cloud_{alt}m"] = max(clean_cloud) if clean_cloud else 0.0

        # Duration-based precip (2026-09 redesign, see ACTIVITY_END_GRACE_MINUTES's
        # comment): what fraction of the activity window's hours are "wet"
        # (>=WET_HOUR_PRECIP_THRESHOLD_PCT), not a single averaged/peaked
        # probability. A single passing-shower hour among many dry ones
        # (通り雨) scores a low fraction; sustained rain through most of the
        # window (終日雨) scores a high one -- even if both had the same
        # peak hourly probability, which the old peak-based approach
        # couldn't distinguish (confirmed against 槍ヶ岳9/5 and 立山9/5: both
        # had one isolated high-probability hour surrounded by calm ones,
        # and real hiking reports described them as good days despite the
        # old approach scoring them down as if the rain had been sustained).
        activity_probs = [v for v in activity_vals["precip_prob"] if v is not None]
        activity_mm = [v for v in activity_vals["precip_mm"] if v is not None]
        wet_hours = sum(1 for v in activity_probs if v >= WET_HOUR_PRECIP_THRESHOLD_PCT)
        activity = {
            "wet_fraction_pct": (100.0 * wet_hours / len(activity_probs)) if activity_probs else 0.0,
            "peak_mm": max(activity_mm) if activity_mm else 0.0,
            "wet_hours": wet_hours,
            "total_hours": len(activity_probs),
        }

        summary[day_str] = {
            "activity": activity,
            "ridge": ridge,
            "pm": pm,
            "day_peak": day_peak,
            "day_total_precip_mm": round(day_precip_by_day[day_str], 1) if day_str in day_precip_by_day else None,
        }
    return summary


def fmt(val, ndigits: int = 1) -> str:
    """None-safe rounded string for table cells."""
    if val is None:
        return "-"
    return str(round(val, ndigits))


def hazard_warning_cell(r) -> str:
    """Thunder/wind/cold(hypothermia) hazard level (see mountain_hazards()
    in core.py) plus the numbers behind each -- CAPE, the day's peak gust
    speed/hour, and wet-adjusted wind-chill -- so the level isn't just a
    bare label. '-' when none are active. This supersedes the old wind-only
    "⚠HH時n.nm/s" out-of-window flag: that only caught gusts outside the
    8-11 ridge window (since inside it, wind fed the score); now that wind
    isn't in the score at all, the peak gust for the whole day is what
    mountain_hazards() itself is built from, so a single cell already
    covers both cases."""
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


def precip_timing_note(day_index: int, rows: list):
    """PRECIP_TIMING_NOTE if this day is PRECIP_TIMING_UNCERTAIN_DAYS_OUT+
    out (day_index is 0 for today), or any row's precip_wet_pct (fraction of
    the activity window's hours that are wet, see main()) falls inside the
    ambiguous 40-60% band, else None."""
    if day_index >= PRECIP_TIMING_UNCERTAIN_DAYS_OUT:
        return PRECIP_TIMING_NOTE
    for r in rows:
        wet_pct = r["precip_wet_pct"]
        if wet_pct is not None and PRECIP_TIMING_BOUNDARY_LOW <= wet_pct <= PRECIP_TIMING_BOUNDARY_HIGH:
            return PRECIP_TIMING_NOTE
    return None


def print_ranking_table(rows):
    # 危険信号 sits right after the mountain name -- deliberately the most
    # visible position, not at the end -- because thunder/wind no longer
    # factor into スコア at all (see SCORE_WEIGHTS' comment in core.py); a
    # high score can no longer be trusted to mean "no storm/gust risk", so
    # the warning has to be impossible to skim past.
    header = (
        pad("順位", 5) + pad("山", 26) + pad("アクセス", 24) + pad("危険信号(雷/強風/低体温症)", 34)
        + pad("スコア", 8) + pad("稜線風速m/s", 12)
        + pad("雷リスクCAPE", 14) + pad("雲量%(稜線/PM)", 14) + pad("視程m(稜線)", 10)
        + pad("雨天割合%(活動時間)", 22) + pad("体感温度℃(稜線)", 16)
        + pad("降水量mm(全日)", 14)
    )
    print(header)
    for i, r in enumerate(rows, start=1):
        wet_cell = f"{fmt(r['precip_wet_pct'])}({r['precip_wet_hours']}/{r['precip_total_hours']}h)"
        row = (
            pad(str(i), 5) + pad(r["mountain"], 26) + pad(r["access"], 24)
            + pad(hazard_warning_cell(r), 34)
            + pad(str(r["score"]), 8) + pad(fmt(r["ridge_wind_ms"]), 12)
            + pad(fmt(r["cape"], 0), 14) + pad(fmt(r["cloud_pct"]), 14)
            + pad(fmt(r["ridge_visibility"], 0), 10)
            + pad(wet_cell, 22) + pad(fmt(r["chill"]), 16)
            + pad(fmt(r.get("day_total_precip_mm"), 0), 14)
        )
        print(row)


def main():
    today = date.today()
    target_dates = [str(today + timedelta(days=i)) for i in range(15)]  # today .. +14

    pool = MOUNTAINS
    if REGION_FILTER:
        pool = [m for m in MOUNTAINS if m["region"] in REGION_FILTER]

    # date -> list of per-mountain results for that date
    by_date: dict[str, list] = {d: [] for d in target_dates}

    for mtn in pool:
        try:
            forecast = fetch_forecast(mtn["lat"], mtn["lon"])
        except requests.exceptions.RequestException as e:
            print(f"  ({mtn['name']}: 取得失敗のためスキップ - {e})")
            continue
        by_day = window_scores_by_day(forecast)
        for d in target_dates:
            if d in by_day:
                windows = by_day[d]
                band = nearest_band(mtn["elevation_m"])
                ridge, activity, pm = windows["ridge"], windows["activity"], windows["pm"]
                day_peak = windows["day_peak"][band]
                ridge_wind_ms = ridge[f"wind_{band}m"] * KMH_TO_MS
                chill = wind_chill_c(ridge[f"temp_{band}m"], ridge[f"wind_{band}m"])
                # Precip is duration-based (% of the activity window's hours
                # that are wet, see window_scores_by_day()'s docstring) --
                # already spans climb-through-descent, no AM/PM max needed.
                # Cloud still takes the worse of ridge-dwell/PM (a dry, clear
                # ridge window shouldn't hide an afternoon that clouds up).
                precip_wet_pct = activity["wet_fraction_pct"]
                precip_mm = activity["peak_mm"]
                cloud_pct = max(ridge[f"cloud_{band}m"], pm[f"cloud_{band}m"])
                score = mountain_climb_score(
                    cloud_pct=cloud_pct,
                    ridge_visibility_m=ridge["visibility"],
                    precip_wet_pct=precip_wet_pct,
                    precip_mm=precip_mm,
                )
                # Thunder/wind/cold no longer feed the score (see
                # SCORE_WEIGHTS' comment) -- surfaced instead as an explicit
                # hazard level. Wind uses the day's PEAK gust (not the 8-11
                # ridge average), since a threshold hazard like a gust is
                # missed by an average and previously needed a separate
                # "outside the window" flag (wind_warning) to catch it; using
                # the peak here supersedes that older, narrower check
                # entirely.
                hazards = mountain_hazards(
                    ridge_wind_ms=day_peak["wind_ms"], pm_cape=pm["cape"],
                    chill_c=chill, precip_wet_pct=precip_wet_pct, precip_mm=precip_mm,
                )
                by_date[d].append(
                    {
                        "mountain": f'{mtn["name"]}({mtn["elevation_m"]}m)',
                        "access": mtn["access"],
                        "region": mtn["region"],
                        "score": score,
                        "ridge_wind_ms": round(ridge_wind_ms, 1),
                        "cape": pm["cape"],
                        "cloud_pct": round(cloud_pct, 1),
                        "ridge_visibility": round(ridge["visibility"]) if ridge["visibility"] is not None else None,
                        "precip_wet_pct": round(precip_wet_pct, 1),
                        "precip_wet_hours": activity["wet_hours"],
                        "precip_total_hours": activity["total_hours"],
                        "chill": round(chill, 1) if chill is not None else None,
                        "day_peak_wind_ms": round(day_peak["wind_ms"], 1) if day_peak["wind_ms"] is not None else None,
                        "day_peak_wind_hour": day_peak["hour"],
                        "hazards": hazards,
                        "day_total_precip_mm": windows["day_total_precip_mm"],
                    }
                )

    region_label = "、".join(REGION_FILTER) if REGION_FILTER else "全地域"
    count_label = "すべて" if TOP_N_PER_DAY is None else f"上位{TOP_N_PER_DAY}件"
    print(f"\n【対象地域: {region_label} / 1日あたり{count_label} / "
          f"score{MIN_SCORE_THRESHOLD}以上のみ / "
          f"登山向け総合スコア=雲量(稜線/PM)・視程・降水(活動時間中の雨天割合)の重み付き幾何平均(各{SCORE_WEIGHTS['cloud']:.2f}) / "
          f"雷・強風・低体温症はスコアに含めず「危険信号」列で別枠警告 / "
          f"稜線帯:日の出{TRIP_START_OFFSET_HOURS + RIDGE_DWELL_TRIM_HOURS:+.0f}h〜{MAIN_TIME_END_HOUR - RIDGE_DWELL_TRIM_HOURS}時 "
          f"活動時間(降水判定・PM雷雲共通):日の出{TRIP_START_OFFSET_HOURS:+.0f}h〜"
          f"日没+{ACTIVITY_END_GRACE_MINUTES}分 PM開始:{PM_START_HOUR}時】")

    for week_label, day_range in (("=== 今週 (0-6日先) ===", range(0, 7)),
                                   ("=== 来週以降 (7-14日先) ===", range(7, 15))):
        print(f"\n{week_label}")
        for i in day_range:
            d = target_dates[i]
            qualifying = [r for r in by_date[d] if r["score"] >= MIN_SCORE_THRESHOLD]
            ranked = sorted(qualifying, key=lambda r: r["score"], reverse=True)
            day_rows = ranked if TOP_N_PER_DAY is None else ranked[:TOP_N_PER_DAY]
            if not day_rows:
                continue
            print(f"\n-- {format_date_with_weekday(d)} --")
            print_ranking_table(day_rows)
            note = precip_timing_note(i, day_rows)
            if note:
                print(note)


if __name__ == "__main__":
    main()
