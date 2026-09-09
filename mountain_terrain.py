"""ヤマビヨリ 地形レイヤー (2026-09-10) -- 国土地理院の標高タイルから、各山の
山頂まわりの地形を「風上斜面の上昇流」「稜線の露出度」として数値化する。

ホシビヨリ(星空観測アプリ、C:\\hoshibiyori/step2_terrain)の DemTileStore /
vincenty_direct を移植したもの。地形は変わらないので、76座分は
precompute_terrain.py で一度だけ計算して terrain_profiles.json に持ち、
実行時は読むだけ(GPXルート診断の任意地点だけ実行時に計算する)。

DEMデータ出典: 国土地理院 地理院タイル(標高タイル) https://cyberjapandata.gsi.go.jp/
  -> 成果物の公開時は「出典:国土地理院」の表示を行うこと(ATTRIBUTION)。
  -> 公開インフラなので同一タイルの重複取得を避ける: terrain_cache/ に
     ディスクキャッシュし、新規取得ごとに小休止を入れる。

何を計算するか(compute_terrain_profile):
  16方位それぞれについて山頂から 25km までを 500m 刻みで標高サンプリングし、
  - drop_m[az]      : 山頂標高 - その方位 2〜15km の平均標高
                      (= その方位から吹く風が乗り越えてくる地形の落差。
                         大きいほど風上斜面の強制上昇が強い)
  - horizon_deg[az] : 10km 以内の最大仰角(地球曲率+大気差込み)。
                      低いほどその方位が開けている(=吹きさらし)
  - near_mean_m/far_mean_m: 2〜10km / 10〜25km の平均標高(参考)
  から
  - exposure_deg    : horizon_deg の平均(小さいほど露出した稜線、大きいほど谷・樹林)
  - ridge_axis_deg  : drop が最小になる方位の軸(稜線の走向。0〜180°)
  を導く。upslope_lift() は MSM の山頂風向・風速と drop から
  w ≈ U × (drop / 水平距離) [m/s] の上昇流指標を返す。

これは 5km 格子の数値予報の値を変えるものではなく、格子が表現できない
「風上側の斜面か、風下側か」「稜線がどれだけ吹きさらしか」という解釈を
与えるための層である(2026-09-09〜10 の他社比較で、蔵王山9/12 の
奥羽山脈東斜面の層雲、唐松岳9/6 の南西風での北アルプス西斜面の湿潤化が
この種の読みだった)。
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import numpy as np
import requests

ATTRIBUTION = "出典: 国土地理院 (地理院タイル 標高タイル, cyberjapandata.gsi.go.jp)"
GSI_DEM_URL = "https://cyberjapandata.gsi.go.jp/xyz/dem/{z}/{x}/{y}.txt"
# ズーム11: 1タイル約20km四方・1セル約76m(北緯36度)。25km半径の16方位
# サンプリングで1座あたり4〜9タイル、隣接する山とはタイルを共有する。
# (ホシビヨリはズーム14=10mメッシュを使うが、ここでは数km規模の地形が
#  分かれば十分で、タイル量を1/64に抑えられる。)
DEM_ZOOM = 11
TERRAIN_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terrain_cache")
TERRAIN_PROFILES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terrain_profiles.json")
TERRAIN_PROFILE_VERSION = 2

_USER_AGENT = "yamabiyori-terrain/0.1 (personal mountain-weather tool; script-based, low-volume, cached)"
_REQUEST_TIMEOUT_SEC = 15
_POLITE_DELAY_SEC = 0.1
EARTH_RADIUS_M = 6371000.0
REFRACTION_K = 0.13

AZIMUTH_STEP_DEG = 22.5
AZIMUTHS = [i * AZIMUTH_STEP_DEG for i in range(16)]
RAY_STEP_M = 500.0
RAY_MAX_M = 25000.0
DROP_BAND_M = (2000.0, 15000.0)     # drop_m: この距離帯の平均標高との差
DROP_MEAN_DISTANCE_M = 8500.0        # upslope_lift の傾斜換算に使う代表水平距離
HORIZON_MAX_M = 10000.0              # horizon_deg: この距離以内の最大仰角(展望・遠景用)
# 露出度は山頂近傍だけを見る: 10km だと隣の高峰に支配されて山頂はほぼ全て
# 0° になり弁別できない(76座で確認)。3km・250m 刻みなら「隣の肩や樹林に
# 囲まれた小ピーク」と「吹きさらしの稜線」が分かれる(唐松岳 2.0° / 高尾山 0°)。
# 山頂は定義上の極大なので本来の用途は GPX の任意地点(登山口・小屋・谷)。
NEAR_HORIZON_MAX_M = 3000.0
NEAR_HORIZON_STEPS_M = (250.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0, 2500.0, 3000.0)
# MOUNTAINS の座標が DEM 上の山頂から外れているかの目安: 座標セル周辺
# (±5セル ≈ ±400m)の DEM 最大標高と登録標高の差がこれ以上なら要確認。
SUMMIT_MISMATCH_FLAG_M = 300.0
NEAR_BAND_M = (2000.0, 10000.0)
FAR_BAND_M = (10000.0, 25000.0)

# upslope_lift() の w [m/s] の目安(参考値、ラベルには使わない)。地形性
# 上昇流は 0.1〜1 m/s 程度が典型。
LIFT_MODERATE_MS = 0.2
LIFT_STRONG_MS = 0.6
# exposure_deg の目安(地平線仰角の平均)。稜線なら 0〜3°、谷や樹林帯は 10°超。
EXPOSURE_OPEN_DEG = 3.0
EXPOSURE_SHELTERED_DEG = 10.0

COMPASS_16 = ["北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
              "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西"]


# ---------------------------------------------------------------------------
# 測地線(ホシビヨリ geodesy.py の vincenty_direct を移植)
# ---------------------------------------------------------------------------
def vincenty_direct(lat1_deg: float, lon1_deg: float, azimuth_deg: float, distance_m: float) -> tuple:
    """WGS84 楕円体上で、始点・方位角・距離から到達点(緯度, 経度)を返す。"""
    a = 6378137.0
    f = 1 / 298.257223563
    b = (1 - f) * a
    lat1 = math.radians(lat1_deg)
    lon1 = math.radians(lon1_deg)
    alpha1 = math.radians(azimuth_deg)
    sin_alpha1, cos_alpha1 = math.sin(alpha1), math.cos(alpha1)
    tan_u1 = (1 - f) * math.tan(lat1)
    cos_u1 = 1 / math.sqrt(1 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1
    sigma1 = math.atan2(tan_u1, cos_alpha1)
    sin_alpha = cos_u1 * sin_alpha1
    cos_sq_alpha = 1 - sin_alpha * sin_alpha
    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    sigma = distance_m / (b * big_a)
    for _ in range(100):
        cos2_sigma_m = math.cos(2 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        delta_sigma = big_b * sin_sigma * (cos2_sigma_m + big_b / 4 * (
            cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)
            - big_b / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos2_sigma_m ** 2)))
        sigma_new = distance_m / (b * big_a) + delta_sigma
        if abs(sigma_new - sigma) < 1e-12:
            sigma = sigma_new
            break
        sigma = sigma_new
    sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
    cos2_sigma_m = math.cos(2 * sigma1 + sigma)
    tmp = sin_u1 * sin_sigma - cos_u1 * cos_sigma * cos_alpha1
    lat2 = math.atan2(sin_u1 * cos_sigma + cos_u1 * sin_sigma * cos_alpha1,
                      (1 - f) * math.sqrt(sin_alpha * sin_alpha + tmp * tmp))
    lam = math.atan2(sin_sigma * sin_alpha1, cos_u1 * cos_sigma - sin_u1 * sin_sigma * cos_alpha1)
    big_c = f / 16 * cos_sq_alpha * (4 + f * (4 - 3 * cos_sq_alpha))
    big_l = lam - (1 - big_c) * f * sin_alpha * (
        sigma + big_c * sin_sigma * (cos2_sigma_m + big_c * cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)))
    lon2 = lon1 + big_l
    return math.degrees(lat2), math.degrees(lon2)


# ---------------------------------------------------------------------------
# 地理院標高タイル(ホシビヨリ gsi_dem.py の DemTileStore を移植、ズーム可変)
# ---------------------------------------------------------------------------
def deg2tile_frac(lat_deg: float, lon_deg: float, zoom: int) -> tuple:
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    x = (lon_deg + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


class DemTileStore:
    """標高タイルのディスク+メモリキャッシュ。欠測(海など)は NaN、
    get_elevation() では 0.0(海面)として返す。"""

    def __init__(self, cache_dir: str = TERRAIN_CACHE_DIR, zoom: int = DEM_ZOOM, session=None):
        self.zoom = zoom
        self.cache_dir = Path(cache_dir) / f"dem_z{zoom}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})
        self._mem: dict = {}
        self.downloaded_count = 0
        self.cache_hit_count = 0
        self.failed_count = 0

    def _disk_path(self, x: int, y: int) -> Path:
        return self.cache_dir / f"{self.zoom}_{x}_{y}.npy"

    @staticmethod
    def _parse_txt(text: str) -> np.ndarray:
        rows = []
        for line in text.strip("\n").split("\n"):
            rows.append([np.nan if v.strip() in ("e", "E", "") else float(v) for v in line.split(",")])
        arr = np.array(rows, dtype=np.float64)
        if arr.shape != (256, 256):
            padded = np.full((256, 256), np.nan)
            padded[: arr.shape[0], : arr.shape[1]] = arr
            arr = padded
        return arr

    def get_tile(self, x: int, y: int):
        if (x, y) in self._mem:
            return self._mem[(x, y)]
        path = self._disk_path(x, y)
        if path.exists():
            arr = np.load(path)
            self._mem[(x, y)] = arr
            self.cache_hit_count += 1
            return arr
        try:
            resp = self.session.get(GSI_DEM_URL.format(z=self.zoom, x=x, y=y), timeout=_REQUEST_TIMEOUT_SEC)
        except requests.RequestException:
            self.failed_count += 1
            return None
        if resp.status_code == 404:
            arr = np.full((256, 256), np.nan)
        elif resp.status_code == 200:
            arr = self._parse_txt(resp.text)
        else:
            self.failed_count += 1
            return None
        np.save(path, arr)
        self._mem[(x, y)] = arr
        self.downloaded_count += 1
        time.sleep(_POLITE_DELAY_SEC)
        return arr

    def max_elevation_near(self, lat_deg: float, lon_deg: float, radius_cells: int = 5):
        """座標セルの周囲 ±radius_cells セル内の DEM 最大標高(座標ズレの検出用)。"""
        xf, yf = deg2tile_frac(lat_deg, lon_deg, self.zoom)
        x, y = int(math.floor(xf)), int(math.floor(yf))
        tile = self.get_tile(x, y)
        if tile is None:
            return None
        px = min(int((xf - x) * 256), 255)
        py = min(int((yf - y) * 256), 255)
        sub = tile[max(0, py - radius_cells):py + radius_cells + 1, max(0, px - radius_cells):px + radius_cells + 1]
        v = np.nanmax(sub) if np.isfinite(sub).any() else np.nan
        return None if np.isnan(v) else float(v)

    def get_elevation(self, lat_deg: float, lon_deg: float):
        xf, yf = deg2tile_frac(lat_deg, lon_deg, self.zoom)
        x, y = int(math.floor(xf)), int(math.floor(yf))
        tile = self.get_tile(x, y)
        if tile is None:
            return None
        px = min(int((xf - x) * 256), 255)
        py = min(int((yf - y) * 256), 255)
        v = tile[py, px]
        return 0.0 if np.isnan(v) else float(v)


# ---------------------------------------------------------------------------
# 地形プロファイル
# ---------------------------------------------------------------------------
def apparent_elevation_angle_deg(observer_m: float, target_m: float, distance_m: float,
                                 k: float = REFRACTION_K) -> float:
    drop = (1 - k) * distance_m * distance_m / (2 * EARTH_RADIUS_M)
    return math.degrees(math.atan2(target_m - observer_m - drop, distance_m))


def compute_terrain_profile(lat: float, lon: float, summit_m: float, store: DemTileStore = None) -> dict:
    """1地点の地形プロファイル(モジュール docstring 参照)。通信失敗した
    サンプル点は飛ばす(全滅した方位は None)。"""
    store = store or DemTileStore()
    distances = [RAY_STEP_M * i for i in range(1, int(RAY_MAX_M // RAY_STEP_M) + 1)]
    dem_summit = store.get_elevation(lat, lon)
    dem_summit_max = store.max_elevation_near(lat, lon)
    drop, horizon, horizon_near, near, far = [], [], [], [], []
    for az in AZIMUTHS:
        hn = []
        for d in NEAR_HORIZON_STEPS_M:
            la, lo = vincenty_direct(lat, lon, az, d)
            e = store.get_elevation(la, lo)
            if e is not None:
                hn.append(apparent_elevation_angle_deg(summit_m, e, d))
        horizon_near.append(round(max(hn), 2) if hn else None)
        elevs = []
        for d in distances:
            la, lo = vincenty_direct(lat, lon, az, d)
            e = store.get_elevation(la, lo)
            elevs.append(e)
        band = [e for d, e in zip(distances, elevs) if e is not None and DROP_BAND_M[0] <= d <= DROP_BAND_M[1]]
        nb = [e for d, e in zip(distances, elevs) if e is not None and NEAR_BAND_M[0] <= d <= NEAR_BAND_M[1]]
        fb = [e for d, e in zip(distances, elevs) if e is not None and FAR_BAND_M[0] <= d <= FAR_BAND_M[1]]
        hz = [apparent_elevation_angle_deg(summit_m, e, d) for d, e in zip(distances, elevs)
              if e is not None and d <= HORIZON_MAX_M]
        drop.append(round(summit_m - sum(band) / len(band), 1) if band else None)
        near.append(round(sum(nb) / len(nb), 1) if nb else None)
        far.append(round(sum(fb) / len(fb), 1) if fb else None)
        horizon.append(round(max(hz), 2) if hz else None)
    hz_clean = [h for h in horizon_near if h is not None]
    exposure = round(sum(max(0.0, h) for h in hz_clean) / len(hz_clean), 2) if hz_clean else None
    # 稜線の走向: 向かい合う2方位の drop の和が最小になる軸
    axis = None
    if all(v is not None for v in drop):
        pair = min(range(8), key=lambda i: drop[i] + drop[i + 8])
        axis = AZIMUTHS[pair]
    return {
        "lat": lat, "lon": lon, "summit_m": summit_m,
        "dem_summit_m": round(dem_summit, 1) if dem_summit is not None else None,
        "dem_summit_max_near_m": round(dem_summit_max, 1) if dem_summit_max is not None else None,
        "summit_coord_suspect": (dem_summit_max is not None and abs(summit_m - dem_summit_max) >= SUMMIT_MISMATCH_FLAG_M),
        "azimuths_deg": AZIMUTHS,
        "drop_m": drop, "horizon_deg": horizon, "horizon_near_deg": horizon_near,
        "near_mean_m": near, "far_mean_m": far,
        "exposure_deg": exposure, "ridge_axis_deg": axis,
    }


def _interp_az(values: list, az_deg: float):
    """16方位の値を任意方位へ線形補間(None を含む方位はその隣で代用)。"""
    if az_deg is None:
        return None
    a = az_deg % 360.0
    i = int(a // AZIMUTH_STEP_DEG) % 16
    j = (i + 1) % 16
    t = (a - i * AZIMUTH_STEP_DEG) / AZIMUTH_STEP_DEG
    vi, vj = values[i], values[j]
    if vi is None and vj is None:
        return None
    if vi is None:
        return vj
    if vj is None:
        return vi
    return vi + (vj - vi) * t


def upslope_lift(profile: dict, wind_from_deg, wind_speed_ms):
    """MSM の山頂風(風向=吹いてくる方位, 風速 m/s)に対する風上斜面の
    上昇流指標。{"w_ms", "slope", "drop_m", "rel_drop", "from_deg", "label"}
    を返す。データ不足なら None。

    w_ms   = U × (風上方位の drop / 代表水平距離): その方位から来る空気が
             山体を乗り越えるときの平均上昇速度 [m/s]。
    rel_drop = 風上方位の drop / 16方位の平均 drop: 孤立峰(蔵王山のように
             全方位 drop 1000m 超)では w_ms が常に「強」になって弁別に
             ならないため、「その山にとって風上が特に開けた側か」を表す
             相対値を併記する(1.0 = 平均並み、>1.2 = 風上側が深い谷・盆地
             = 湿った空気が集まりやすい、<0.8 = 風上側に高い地形がある
             = 一度乗り越えて乾いた空気 = フェーン側)。label は rel_drop
             で決める。"""
    if profile is None or wind_from_deg is None or wind_speed_ms is None:
        return None
    drop = _interp_az(profile["drop_m"], wind_from_deg)
    if drop is None:
        return None
    clean = [v for v in profile["drop_m"] if v is not None]
    mean_drop = sum(clean) / len(clean) if clean else None
    rel = (drop / mean_drop) if mean_drop and mean_drop > 0 else None
    slope = max(0.0, drop) / DROP_MEAN_DISTANCE_M
    w = wind_speed_ms * slope
    return {"w_ms": round(w, 2), "slope": round(slope, 3), "drop_m": round(drop, 0),
            "rel_drop": round(rel, 2) if rel is not None else None,
            "from_deg": round(wind_from_deg, 0), "label": lift_label(rel)}


REL_DROP_WINDWARD = 1.15   # 風上側が平均より深い: 湿った空気が集まる側
REL_DROP_LEE = 0.85        # 風上側に高い地形: 乗り越えてきた乾いた空気(風下側)


def lift_label(rel_drop) -> str:
    """rel_drop(upslope_lift 参照)の3段階ラベル。"""
    if rel_drop is None:
        return "-"
    if rel_drop >= REL_DROP_WINDWARD:
        return "風上(谷側)"
    if rel_drop <= REL_DROP_LEE:
        return "風下(山越え)"
    return "並"


def exposure_label(exposure_deg) -> str:
    if exposure_deg is None:
        return "-"
    if exposure_deg <= EXPOSURE_OPEN_DEG:
        return "高(吹きさらし)"
    if exposure_deg >= EXPOSURE_SHELTERED_DEG:
        return "低(谷・樹林)"
    return "中"


def compass16(deg) -> str:
    if deg is None:
        return "-"
    return COMPASS_16[int(((deg % 360.0) + AZIMUTH_STEP_DEG / 2) // AZIMUTH_STEP_DEG) % 16]


# ---------------------------------------------------------------------------
# 事前計算した 76 座のプロファイル(precompute_terrain.py が書く)
# ---------------------------------------------------------------------------
_profiles_cache = None


def load_terrain_profiles(path: str = TERRAIN_PROFILES_PATH) -> dict:
    """terrain_profiles.json を読む({山名: profile})。無ければ空 dict。"""
    global _profiles_cache
    if _profiles_cache is None:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _profiles_cache = data.get("profiles", {})
        except (OSError, json.JSONDecodeError):
            _profiles_cache = {}
    return _profiles_cache


def terrain_for(mountain_name: str):
    return load_terrain_profiles().get(mountain_name)


def upslope_lift_series(hourly: dict, profile: dict, wind_dir_var: str, wind_speed_var: str) -> list:
    """時間ごとの upslope_lift() (風速列は km/h 前提 -> m/s に換算)。"""
    dirs = hourly.get(wind_dir_var) or []
    spds = hourly.get(wind_speed_var) or []
    out = []
    for d, s in zip(dirs, spds):
        out.append(upslope_lift(profile, d, s / 3.6 if s is not None else None))
    return out


def summarize_lift(lifts: list) -> dict:
    """活動時間などの窓に含まれる upslope_lift() の結果を集計:
    {"windward_hours", "lee_hours", "neutral_hours", "mean_w_ms", "max_w_ms",
     "label", "from"}。label は多数派(風上/風下/並)、from は多数派の風向
    (16方位)、データ無しは "-"。"""
    clean = [l for l in lifts if l]
    if not clean:
        return {"windward_hours": 0, "lee_hours": 0, "neutral_hours": 0, "mean_w_ms": None, "max_w_ms": None,
                "label": "-", "from": "-"}
    ww = sum(1 for l in clean if l["label"].startswith("風上"))
    lee = sum(1 for l in clean if l["label"].startswith("風下"))
    neu = len(clean) - ww - lee
    ws = [l["w_ms"] for l in clean]
    label = "風上" if ww > max(lee, neu) else "風下" if lee > max(ww, neu) else "並"
    dirs = [compass16(l["from_deg"]) for l in clean]
    from_ = max(set(dirs), key=dirs.count)
    return {"windward_hours": ww, "lee_hours": lee, "neutral_hours": neu,
            "mean_w_ms": round(sum(ws) / len(ws), 2), "max_w_ms": round(max(ws), 2), "label": label, "from": from_}


def terrain_cell(summit_summary: dict, base_summary: dict = None) -> str:
    """表セル用。'頂:並(南西) 底:風上(東)8h w0.4' -- 山頂風の読みと、稜線帯
    下端(山頂-CLIMB_LAYER_DEPTH_M)の風の読み。層雲が山頂より下にあるときは
    後者が効く(蔵王山9/12: 山頂は西風、1000m付近は東風で東斜面に層雲)。"""
    if not summit_summary or summit_summary["label"] == "-":
        return "-"
    top = f"頂:{summit_summary['label']}({summit_summary['from']})"
    if not base_summary or base_summary["label"] == "-":
        return f"{top} w{summit_summary['mean_w_ms']:.1f}"
    strong = max(base_summary["windward_hours"], base_summary["lee_hours"])
    return f"{top} 底:{base_summary['label']}({base_summary['from']}){strong}h w{base_summary['mean_w_ms']:.1f}"
