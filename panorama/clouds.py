"""展望図の部品: 雲の絵の部品(帯・雲海・山にかかるかたまり・縁の弧・影)。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

import bisect
import hashlib
import html
import math
import numpy as np
import random


# ---- 元: experiments/panorama/stage35_clouds.py ----



# ---- 元: experiments/panorama/stage36_clouds.py ----

START_PCT = 10.0

OPACITY_MIN = 0.15

OPACITY_SPAN = 0.80

REP_DIST_KM = 50.0            # 帯の代表の距離(指示書 4.2)

THIN_RATIO = 0.35             # うすい雲の濃さ = 帯の × これ(仮)

CLUMP_BELOW_SUMMIT_M = 300.0  # 山ごとのかたまりは、山頂 − これ より上の雲だけ(仮)

PAD_MIN_PX = 2.0              # 帯の上下の余白(雲量 0% のとき。仮)

PAD_SPAN_PX = 8.0             # 雲量 100% で足す余白(仮)

UP_ARC_PX = (14.0, 52.0)      # 上の縁の弧の幅(雲量 100% のとき。雲量が少ないと小さく)

UP_ARC_H = (0.25, 0.50)       # 上の縁の弧の高さ / 幅

LO_ARC_PX = (10.0, 30.0)      # 下の縁の弧の幅

LO_ARC_H = (0.10, 0.22)       # 下の縁の弧の高さ / 幅(浅く)

THIN_ARC_PX = (6.0, 28.0)     # うすい雲の上の縁の弧の幅(小さく)

THIN_DIP = 0.6                # うすい雲の上の縁を、ところどころ厚みの最大この割合まで下げる(まばらに)

ARC_SAMPLES = 6

CLUMP_MAX_ASPECT = 0.7       # かたまりの高さは幅のこの倍まで(仮。高い面まで続く雲でも、山頂のまわりの 1 つにする)

CELL_KM = 5.0                 # かたまりの幅(段階3.5 と同じ式)

MAX_DEG_CAP = 4.0

MAX_DEG_FLOOR = 1.2

MIN_RATIO = 0.35

MIN_DEG_FLOOR = 0.6

CLOUD_FILL = "#ffffff"

def opacity(c_pct) -> float:
    c = max(0.0, min(100.0, float(c_pct)))
    return OPACITY_MIN + OPACITY_SPAN * c / 100.0

def is_cloud(c, start_pct=START_PCT) -> bool:
    return c is not None and float(c) >= start_pct

def seed_of(*parts) -> int:
    s = "|".join(str(p) for p in parts)
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:12], 16)

def size_bounds_deg(dist_km: float) -> tuple:
    d = max(0.5, float(dist_km))
    mx = math.degrees(math.atan(CELL_KM / d))
    mx = max(MAX_DEG_FLOOR, min(MAX_DEG_CAP, mx))
    mn = max(MIN_DEG_FLOOR, mx * MIN_RATIO)
    return mn, max(mn, mx)

def width_deg(c_pct: float, dist_km: float) -> float:
    mn, mx = size_bounds_deg(dist_km)
    c = max(0.0, min(100.0, float(c_pct)))
    return mn + (mx - mn) * c / 100.0

def level_profile(points: dict, hpas) -> dict:
    """points: {方位: [(levels, 地面の高さ m or None), ...]}(levels は stage35_sky.levels_at の返り値)。
    返り値: {"azs", "hpas"(低い面から), "c": [[雲量 or None]*方位]*面, "alt": 同じ形の高さ}。
    雲量・高さは、その方位の点(距離ごと)の平均。点の地面より低い面は入れない。"""
    hp = sorted(hpas, reverse=True)
    azs = sorted(points)
    C = [[None] * len(azs) for _ in hp]
    A = [[None] * len(azs) for _ in hp]
    for j, a in enumerate(azs):
        for k, h in enumerate(hp):
            cs, al = [], []
            for levels, ground in points[a]:
                for alt, c, hpa, *_ in levels or []:
                    if hpa != h or c is None:
                        continue
                    if ground is not None and alt < float(ground):
                        continue
                    cs.append(float(c))
                    al.append(float(alt))
            if cs:
                C[k][j] = sum(cs) / len(cs)
                A[k][j] = sum(al) / len(al)
    return {"azs": azs, "hpas": hp, "c": C, "alt": A}

def segments_at(profile: dict, j: int, start_pct=START_PCT) -> list:
    """方位 j の、上下に続く雲の面の区間。返り値: [{"lo","hi"(面の番号), "bottom","top"(m), "c"(区間の最大),
    "thin_top"(すぐ上の面が雲なしならその高さ。無ければ None)}]。"""
    C, A = profile["c"], profile["alt"]
    n = len(C)
    out, k = [], 0
    while k < n:
        if not is_cloud(C[k][j], start_pct) or A[k][j] is None:
            k += 1
            continue
        lo = k
        while k + 1 < n and is_cloud(C[k + 1][j], start_pct) and A[k + 1][j] is not None:
            k += 1
        hi = k
        up = hi + 1
        thin = A[up][j] if up < n and C[up][j] is not None and A[up][j] is not None else None
        out.append({"lo": lo, "hi": hi, "bottom": A[lo][j], "top": A[hi][j],
                    "c": max(C[q][j] for q in range(lo, hi + 1)), "thin_top": thin})
        k += 1
    return out

def build_bands(profile: dict, start_pct=START_PCT, max_bottom_m=None, az_step=10.0, seg_fn=None) -> list:
    """帯の一覧。隣の方位で面の範囲が重なる区間を 1 対 1 でつなぐ(重なりの大きい組から。全周の格子なら 350° と 0° も)。
    1 本の帯は、1 つの方位に区間を 1 つしか持たない(雲なしの面をはさむ 2 つの区間を 1 つにまとめない)。
    max_bottom_m: 区間の下の面がこれより高いもの(図の枠の上にはみ出す雲)は入れない。
    返り値: [{"full": 全周か, "cols": [{"az"(つながる向きにほどいた方位), "j", "lo","hi","bottom","top","c","thin_top"}],
    "hpas": [その帯に入る面]}](方位の順)。"""
    azs = profile["azs"]
    n = len(azs)
    circular = n > 1 and abs(n * az_step - 360.0) < 1e-6
    seg_fn = seg_fn or segments_at
    segs = {j: [s for s in seg_fn(profile, j, start_pct) if max_bottom_m is None or s["bottom"] <= max_bottom_m]
            for j in range(n)}
    nxt, prv = {}, {}
    for j in range(n):
        if not circular and j + 1 >= n:
            continue
        j2 = (j + 1) % n
        if j2 == j:
            continue
        pairs = []
        for i, s in enumerate(segs[j]):
            for i2, t in enumerate(segs[j2]):
                ov = min(s["hi"], t["hi"]) - max(s["lo"], t["lo"])
                if ov >= 0:
                    pairs.append((-ov, abs(s["lo"] - t["lo"]), i, i2))
        for _, _, i, i2 in sorted(pairs):
            if (j, i) not in nxt and (j2, i2) not in prv:
                nxt[(j, i)] = (j2, i2)
                prv[(j2, i2)] = (j, i)
    used, bands = set(), []

    def chain(start):
        keys, k = [], start
        cols_seen = set()
        while k is not None and k not in used and k[0] not in cols_seen:
            keys.append(k)
            used.add(k)
            cols_seen.add(k[0])
            k = nxt.get(k)
        return keys, k == start
    starts = [(j, i) for j in range(n) for i in range(len(segs[j])) if (j, i) not in prv]
    rest = [(j, i) for j in range(n) for i in range(len(segs[j]))]
    for st in starts + rest:
        if st in used:
            continue
        keys, closed = chain(st)
        full = circular and closed and len(keys) == n
        a0 = azs[keys[0][0]]
        out_cols = [dict(segs[j][i], j=j, az=a0 + az_step * k) for k, (j, i) in enumerate(keys)]
        hp = sorted({profile["hpas"][q] for c in out_cols for q in range(c["lo"], c["hi"] + 1)}, reverse=True)
        bands.append({"full": full, "cols": out_cols, "hpas": hp})
    bands.sort(key=lambda b: (b["cols"][0]["az"], b["cols"][0]["bottom"]))
    return bands

def cover_by_az(bands: list, n_az: int, az_step=10.0) -> dict:
    """方位の番号 → [(下の面の高さ, 上の面の高さ, うすい雲を含めた上の高さ)](帯がその方位で包む高さの範囲)。"""
    out = {j: [] for j in range(n_az)}
    for b in bands:
        for c in b["cols"]:
            out[c["j"]].append((c["bottom"], c["top"], c["thin_top"] if c["thin_top"] is not None else c["top"]))
    return out

def covered(cover: list, alt_m: float) -> bool:
    """帯かうすい雲が、その高さを包んでいるか。"""
    return any(b <= alt_m <= t for b, _, t in cover)

def hidden_by_band(cover: list, summit_m: float, viewer_m: float) -> bool:
    """山頂が帯に隠れるか: 山頂が帯の中(下の面〜上の面)にある、または視点が帯の上面より上で山頂が上面より低い。
    うすい雲(高さが分からない範囲)は数えない。"""
    for b, t, _ in cover:
        if summit_m < t and (summit_m >= b or viewer_m > t):
            return True
    return False

def _interp_cols(cols, full, a, key, step=10.0):
    m = len(cols)
    p = (a - cols[0]["az"]) / step
    if full:
        i0 = int(math.floor(p))
        t = p - i0
        v0, v1 = cols[i0 % m][key], cols[(i0 + 1) % m][key]
    else:
        p = max(0.0, min(m - 1.0, p))
        i0 = int(math.floor(p))
        i1 = min(m - 1, i0 + 1)
        t = p - i0
        v0, v1 = cols[i0][key], cols[i1][key]
    return v0 * (1 - t) + v1 * t

def band_extent(band, step=10.0):
    cols = band["cols"]
    if band["full"]:
        return cols[0]["az"] - step / 2.0, cols[0]["az"] - step / 2.0 + 360.0 + 1e-9
    return cols[0]["az"] - step / 2.0, cols[-1]["az"] + step / 2.0

def clump_levels(levels: list, summit_m: float, start_pct=START_PCT, cover=None, all_hpas=None) -> list:
    """levels: [(高さ m, 雲量 %, hPa)]。山頂 − CLUMP_BELOW_SUMMIT_M より上の雲の面で、帯に包まれていないもの。
    上下に続く面(all_hpas の並びで隣どうし)は 1 つにまとめる。返り値: [{"bottom","top","c","hpas"}]。"""
    order = sorted(all_hpas or [h for _, _, h in levels], reverse=True)
    pos = {h: i for i, h in enumerate(order)}
    ok = []
    for alt, c, hpa in sorted(levels, key=lambda x: x[0]):
        if not is_cloud(c, start_pct) or alt < summit_m - CLUMP_BELOW_SUMMIT_M:
            continue
        if cover and covered(cover, alt):
            continue
        ok.append((alt, float(c), hpa))
    groups = []
    for alt, c, hpa in ok:
        if groups and pos.get(hpa, -9) - pos.get(groups[-1]["hpas"][-1], -99) == 1:
            gp = groups[-1]
            gp["top"], gp["c"] = alt, max(gp["c"], c)
            gp["hpas"].append(hpa)
        else:
            groups.append({"bottom": alt, "top": alt, "c": c, "hpas": [hpa]})
    return groups

def thin_at_summit(levels: list, summit_m: float, start_pct=START_PCT, all_hpas=None):
    """山頂が「高さが分からない範囲」(すぐ下の面が雲・すぐ上の面が雲なし。面は all_hpas の並びで隣どうし)にあるか。
    levels: [(高さ, 雲量, hPa)]。返り値: {"bottom","top","c"(下の面の雲量)} or None。"""
    order = sorted(all_hpas or [h for _, _, h in levels], reverse=True)
    pos = {h: i for i, h in enumerate(order)}
    lv = sorted((float(a), c, h) for a, c, h in levels if c is not None)
    for (a0, c0, h0), (a1, c1, h1) in zip(lv, lv[1:]):
        if pos.get(h1, -9) - pos.get(h0, -99) != 1:
            continue
        if is_cloud(c0, start_pct) and not is_cloud(c1, start_pct) and a0 < summit_m <= a1:
            return {"bottom": a0, "top": a1, "c": float(c0)}
    return None

def clump_box(groups: list, angle_fn, dist_m: float, width_px: float, px_v: float):
    """山 1 つにかたまりは 1 つ: いちばん低いまとまり(山頂に近い雲)だけを使う。下の端はその雲の下の面の見上げ角、
    高さは面の範囲の画素(+ 幅 × 0.42)を、幅 × CLUMP_MAX_ASPECT までに抑える。返り値: (下の端の見上げ角, 高さ px, まとまり) or None。"""
    if not groups:
        return None
    gp = groups[0]
    ab, at = angle_fn(gp["bottom"], dist_m), angle_fn(gp["top"], dist_m)
    h = min((at - ab) * px_v + width_px * 0.42, width_px * CLUMP_MAX_ASPECT)
    return ab, h, gp

def clump_shape(seed: int, width_px: float, height_px: float) -> list:
    """かたまり 1 つの形: 白い丸の並び(中心 0,0 の相対座標)。下の段に 2〜4 個、上に 2〜5 個を、乱数でずらして置く。"""
    rnd = random.Random(seed)
    w, h = float(width_px), max(float(height_px), float(width_px) * 0.3)
    out = []
    nb = rnd.randint(2, 4)
    for i in range(nb):
        t = (i + 0.5) / nb
        r = h * rnd.uniform(0.22, 0.34)
        cx = (t - 0.5) * (w - 2 * r) + rnd.uniform(-0.06, 0.06) * w
        out.append((cx, h / 2 - r, r))
    nt = rnd.randint(2, 5)
    for _ in range(nt):
        r = h * rnd.uniform(0.25, 0.45)
        cx = rnd.uniform(-0.32, 0.32) * w
        cy = rnd.uniform(-0.15, 0.2) * h
        out.append((cx, cy, r))
    return [(round(x, 2), round(y, 2), round(r, 2)) for x, y, r in out]


# ---- 元: experiments/panorama/stage37_clouds.py ----


SEA_BOUNDARIES_M = (3000.0, 10000.0, 20000.0, 35000.0, 60000.0, 100000.0, 150000.0)   # 図の層の区切り(DEFAULT_BANDS の最後を除く)

GRID_DISTS_KM = (20.0, 50.0, 100.0)

SHADOW_FILL = "#e2e7ee"          # 白(#fff)の明るさを約 1 割下げた、青みのある明るい灰色(仮)

SHADOW_FRAC = 0.40               # 帯・かたまりの下からこの割合が影(仮)

SEA_SHADOW_PX = 10.0             # 雲海の帯: 上の縁からこの画素より下が影(仮)

SHADOW_ARC_RATIO = 0.35          # 影の上の縁の弧の高さ = 縁の弧の × これ(ゆるいもこもこ)

RIDGE_HIDE_ALPHA = 0.5           # 3.3(仮)

NAME_FADE_SPAN = 0.6             # 3.4(仮)

SKY_UNKNOWN_RATIO = THIN_RATIO   # 3.5(仮。山のうすい雲と同じ 0.35)






ARC_W_SCALE = 0.75               # 弧の幅は、段階3.6 の「雲量で 0.45〜1.0 倍」の真ん中に固定(形が時刻で飛ばないように)


SEA_ARC_REF_KM = 35.0            # 雲海の帯の弧の大きさ・影の深さ: 区切りの距離がこれより遠いと、距離に反比例して小さく(仮)

SEA_ARC_MIN_SCALE = 0.35         # その下限(仮)

def sea_scale(d_km: float) -> float:
    """遠い区切りの雲海の帯ほど、縁の弧を小さく(遠くの雲は小さく見える。段階3.6 のかたまりの大きさと同じ考え方)。"""
    return max(SEA_ARC_MIN_SCALE, min(1.0, SEA_ARC_REF_KM / max(1e-6, float(d_km))))

def unknown_add(levels: list, viewer_m: float, start_pct=START_PCT, ratio=SKY_UNKNOWN_RATIO) -> float:
    """levels: [(高さ, 雲量, hPa, ...)]。視点のすぐ下の面が雲・すぐ上の面が雲なしなら 下の面の雲量 × ratio。なければ 0。"""
    lv = sorted((float(a), None if c is None else float(c)) for a, c, *_ in levels or [])
    lower = [x for x in lv if x[0] <= viewer_m]
    upper = [x for x in lv if x[0] > viewer_m]
    if not lower or not upper:
        return 0.0
    lo, up = lower[-1], upper[0]
    if is_cloud(lo[1], start_pct) and up[1] is not None and not is_cloud(up[1], start_pct):
        return lo[1] * ratio
    return 0.0

def sky_with_unknown(base, levels, viewer_m, start_pct=START_PCT):
    """空の値(段階3.6 の sky_value_above)に、分からない範囲の分を足す(100 まで)。base が None なら None のまま。"""
    if base is None:
        return None
    return min(100.0, float(base) + unknown_add(levels, viewer_m, start_pct))

def interp_profile(profiles: dict, d_km: float) -> dict:
    """profiles: {距離 km(0 = 視点): level_profile の形}(方位・面の並びは同じ)。d_km をはさむ 2 つの距離の値を線形に補間。
    片方が無ければもう片方。いちばん遠い距離より遠ければその値。"""
    ds = sorted(profiles)
    if d_km >= ds[-1]:
        d0 = d1 = ds[-1]
    elif d_km <= ds[0]:
        d0 = d1 = ds[0]
    else:
        i = bisect.bisect_right(ds, d_km)
        d0, d1 = ds[i - 1], ds[i]
    t = 0.0 if d1 == d0 else (d_km - d0) / (d1 - d0)
    p0, p1 = profiles[d0], profiles[d1]
    C = [[None] * len(p0["azs"]) for _ in p0["hpas"]]
    A = [[None] * len(p0["azs"]) for _ in p0["hpas"]]
    for k in range(len(p0["hpas"])):
        for j in range(len(p0["azs"])):
            c0, c1 = p0["c"][k][j], p1["c"][k][j]
            a0, a1 = p0["alt"][k][j], p1["alt"][k][j]
            if c0 is None or a0 is None:
                c0, a0 = c1, a1
            if c1 is None or a1 is None:
                c1, a1 = c0, a0
            if c0 is None or a0 is None:
                continue
            C[k][j] = c0 * (1 - t) + c1 * t
            A[k][j] = a0 * (1 - t) + a1 * t
    return {"azs": list(p0["azs"]), "hpas": list(p0["hpas"]), "c": C, "alt": A}

def sea_column(profile: dict, j: int, viewer_m: float, start_pct=START_PCT):
    """方位 j の雲海: 上の面が視点より低い区間のうち、いちばん高いもの。無ければ None。"""
    segs = [s for s in segments_at(profile, j, start_pct) if s["top"] < viewer_m]
    if not segs:
        return None
    return max(segs, key=lambda s: s["top"])

def sea_runs(profile: dict, viewer_m: float, start_pct=START_PCT, az_step=10.0) -> list:
    """雲海の列を、方位で続く範囲ごとにまとめる(全周の格子なら 350° と 0° もつなぐ)。
    返り値: [{"full", "cols": [{"az"(ほどいた方位), "j", "top","bottom","c","thin_top"}]}]。"""
    azs = profile["azs"]
    n = len(azs)
    col = [sea_column(profile, j, viewer_m, start_pct) for j in range(n)]
    have = [c is not None for c in col]
    circular = n > 1 and abs(n * az_step - 360.0) < 1e-6
    if not any(have):
        return []
    if all(have) and circular:
        return [{"full": True, "cols": [dict(col[j], j=j, az=azs[j]) for j in range(n)]}]
    start = 0
    if circular:
        start = next(j for j in range(n) if not have[j])     # 雲の無い方位から回り始める
    runs, cur = [], None
    for k in range(n):
        j = (start + k) % n
        if have[j]:
            if cur is None:
                cur = []
                runs.append(cur)
            cur.append(j)
        else:
            cur = None
    out = []
    for r in runs:
        a0 = azs[r[0]]
        out.append({"full": False, "cols": [dict(col[j], j=j, az=a0 + az_step * k) for k, j in enumerate(r)]})
    return out

def upper_segments_filter(viewer_m):
    """段階3.6 の帯(50km 先の記号)に残す区間: 視点の高さをまたぐか、視点より上の区間。"""
    return lambda s: s["top"] >= viewer_m

def build_bands_filtered(profile: dict, start_pct, max_bottom_m, keep) -> list:
    """stage36_clouds.build_bands と同じ(1 対 1 でつなぐ)。区間を keep(seg) で選んでから。"""
    def seg_f(p, j, sp=START_PCT):
        return [s for s in segments_at(p, j, sp) if keep(s)]
    return build_bands(profile, start_pct, max_bottom_m, seg_fn=seg_f)

class ArcPattern:
    """図の 0〜360°(画素で 0〜period)を、幅 w_rng の弧で埋めた列。種だけで決まる。value(x) は 0〜1 の山形 × 高さ比。"""

    def __init__(self, seed, w_rng, h_rng, period_px, scale=ARC_W_SCALE, dip_ratio=0.0):
        rnd = random.Random(seed)
        self.period = float(period_px)
        self.x0s, self.ws, self.hk, self.dip = [], [], [], []
        x = 0.0
        while x < self.period - 1e-6:
            w = max(3.0, rnd.uniform(*w_rng) * scale)
            if self.period - (x + w) < w_rng[0] * scale * 0.4:
                w = self.period - x
            self.x0s.append(x)
            self.ws.append(w)
            self.hk.append(rnd.uniform(*h_rng))
            self.dip.append(rnd.random() * THIN_DIP if (dip_ratio and rnd.random() < dip_ratio) else 0.0)
            x += w

    def _find(self, x):
        u = x % self.period
        i = max(0, bisect.bisect_right(self.x0s, u) - 1)
        return i, u

    def bump(self, x):
        """弧の高さ(画素)= 弧の幅 × 高さ比 × sin(π t)。"""
        i, u = self._find(x)
        t = (u - self.x0s[i]) / self.ws[i]
        return self.ws[i] * self.hk[i] * math.sin(math.pi * max(0.0, min(1.0, t)))

    def bump_max(self, x):
        i, _ = self._find(x)
        return self.ws[i] * self.hk[i]

    def dip_at(self, x):
        i, _ = self._find(x)
        return self.dip[i]

    def samples(self, xa, xb):
        """xa〜xb の間で、弧ごとに ARC_SAMPLES 等分した x(両端を含む)。"""
        out = [xa]
        k = math.floor(xa / self.period)
        while k * self.period < xb:
            off = k * self.period
            for x0, w in zip(self.x0s, self.ws):
                for s in range(1, ARC_SAMPLES + 1):
                    x = off + x0 + w * s / ARC_SAMPLES
                    if xa + 1e-6 < x < xb - 1e-6:
                        out.append(x)
            k += 1
        out.append(xb)
        return out

def _interp(cols, full, a, key, step=10.0):
    return _interp_cols(cols, full, a, key, step)

def _stops(cols, full, a0, a1, step):
    stops = []
    span = (a1 - a0) or 1.0
    seq = list(cols) + ([cols[0]] if full else [])
    for k, c in enumerate(seq):
        az = cols[0]["az"] + step * k if full else c["az"]
        ab = opacity(c["c"])
        at_ = THIN_RATIO * ab if c["thin_top"] is not None else 0.0
        stops.append((round((az - a0) / span, 4), round((ab - at_) / (1.0 - at_), 3), round(at_, 3), round(ab, 3)))
    return stops

def path_d(pts) -> str:
    """閉じた輪郭(段階3.6 の path_d と同じ形)を、相対座標(l)で短く書く。点は 0.1 画素に丸めてから差を取る(ずれがたまらない)。"""
    if not pts:
        return ""
    q = [(round(x * 10), round(y * 10)) for x, y in pts]
    out = [f"M{q[0][0] / 10:.1f},{q[0][1] / 10:.1f}l"]
    prev = q[0]
    segs = []
    for x, y in q[1:]:
        dx, dy = x - prev[0], y - prev[1]
        prev = (x, y)
        if dx == 0 and dy == 0:
            continue
        segs.append(f"{_n(dx)},{_n(dy)}")
    return out[0] + " ".join(segs) + "Z"

def _n(v10):
    """0.1 単位の整数を短い文字に(-0.5 → -.5、1.0 → 1)。"""
    s = f"{v10 / 10:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s

def _y_on(pts, x):
    """折れ線 pts(x の昇順)の x での y。"""
    xs = [p[0] for p in pts]
    i = bisect.bisect_left(xs, x)
    if i <= 0:
        return pts[0][1]
    if i >= len(xs):
        return pts[-1][1]
    (xa, ya), (xb, yb) = pts[i - 1], pts[i]
    t = 0.0 if xb == xa else (x - xa) / (xb - xa)
    return ya + (yb - ya) * t

def lower_edge(geom):
    if geom["kind"] == "sea":
        return [(geom["x1"], geom["y_bottom"]), (geom["x0"], geom["y_bottom"])]
    return geom["lower"][::-1]

def band_svg37(geom, bid, wrap_px=None, title="", clip_id=None) -> str:
    """bid は図の中で一意(時刻を入れる)。不透明度は mask(横方向のグラデーション)。中の白い面と影の面は不透明で、
    重ねても 1 つの雲の中では濃くならない。"""
    lo = lower_edge(geom)
    x0, x1 = geom["x0"], geom["x1"]
    ys = [y for _, y in geom["upper"] + lo] + [y for _, y in (geom["thin_upper"] or [])]
    y0, y1 = min(ys) - 2, max(ys) + 2

    def grad(gid, idx):
        st = "".join(f'<stop offset="{s[0]:.4f}" stop-color="#fff" stop-opacity="{s[idx]:.3f}"/>' for s in geom["stops"])
        return (f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{x0:.1f}" y1="0" x2="{x1:.1f}" y2="0">'
                f'{st}</linearGradient>')
    has_thin = bool(geom["thin_upper"])
    defs = grad(f"mg{bid}", 1)
    defs += (f'<mask id="mk{bid}" maskUnits="userSpaceOnUse" x="{x0 - 1:.1f}" y="{y0:.1f}" width="{x1 - x0 + 2:.1f}" '
             f'height="{y1 - y0:.1f}"><rect x="{x0 - 1:.1f}" y="{y0:.1f}" width="{x1 - x0 + 2:.1f}" height="{y1 - y0:.1f}" '
             f'fill="url(#mg{bid})"/></mask>')
    if has_thin:
        defs += grad(f"tg{bid}", 2)
    t = f"<title>{title}</title>" if title else ""
    body = ""
    if has_thin:
        body += f'<path class="cloud-thin" d="{path_d(geom["thin_upper"] + lo)}" fill="url(#tg{bid})"/>'
    body += (f'<g mask="url(#mk{bid})"><path class="cloud-band-body" d="{path_d(geom["upper"] + lo)}" fill="{CLOUD_FILL}"/>'
             f'<path class="cloud-shadow" d="{path_d(geom["shadow_upper"] + lo)}" fill="{SHADOW_FILL}"/></g>')
    inner = f'<g id="bw{bid}">{body}</g>'
    uses = "".join(f'<use href="#bw{bid}" transform="translate({dx:.1f},0)"/>' for dx in wrap_px or [])
    cp = f' clip-path="url(#{clip_id})"' if clip_id else ""
    cls = "cloud-band cloud-sea" if geom["kind"] == "sea" else "cloud-band"
    return f'<g class="{cls}"{cp}>{t}<defs>{defs}</defs>{inner}{uses}</g>'

def band_alpha_fn(geom, wrap_px=None):
    """図の点 (x, y) での、その帯の不透明度(帯の中なら帯の値、うすい雲だけの所はうすい雲の値、外は 0)。3.3 用。"""
    lo = lower_edge(geom)
    up = geom["upper"]
    th = geom["thin_upper"]
    x0, x1 = geom["x0"], geom["x1"]
    stops = geom["stops"]
    offs = [s[0] for s in stops]

    def alpha_at_off(o, idx):
        i = bisect.bisect_left(offs, o)
        if i <= 0:
            return stops[0][idx]
        if i >= len(offs):
            return stops[-1][idx]
        oa, ob = offs[i - 1], offs[i]
        t = 0.0 if ob == oa else (o - oa) / (ob - oa)
        return stops[i - 1][idx] + (stops[i][idx] - stops[i - 1][idx]) * t
    lo_sorted = sorted(lo)

    def f(x, y):
        best = 0.0
        for dx in [0.0] + list(wrap_px or []):
            xx = x - dx
            if not (x0 <= xx <= x1):
                continue
            ylo = _y_on(lo_sorted, xx)
            if y > ylo:
                continue
            o = (xx - x0) / ((x1 - x0) or 1.0)
            if y >= _y_on(up, xx):
                best = max(best, alpha_at_off(o, 3))
            elif th and y >= _y_on(th, xx):
                best = max(best, alpha_at_off(o, 2))
        return best
    return f

def visible_ridge_runs(pts, alpha_fns, limit=RIDGE_HIDE_ALPHA) -> list:
    """稜線の折れ線 pts を、手前の帯(alpha_fns)の重なった不透明度が limit 以上の点で切る。返り値: 見える区間の点の列の一覧。"""
    runs, cur = [], []
    for x, y in pts:
        keep = 1.0
        for fn in alpha_fns:
            keep *= (1.0 - fn(x, y))
        hidden = (1.0 - keep) >= limit
        if hidden:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
        else:
            cur.append((x, y))
    if len(cur) >= 2:
        runs.append(cur)
    return runs

def clump_svg37(x, y, c_pct, width_px, height_px, seed, title="", cid="", thin=False) -> str:
    """段階3.6 の clump_svg / thin_clump_svg と同じ形・不透明度。中の下のほう(SHADOW_FRAC)に影の面を、丸の形で切り抜いて重ねる
    (グループの不透明度の中なので濃くならない)。cid は図の中で一意。"""
    t = f"<title>{title}</title>" if title else ""
    shape = clump_shape(seed, width_px, height_px)
    circles = "".join(f'<circle cx="{x + cx:.1f}" cy="{y + cy:.1f}" r="{r:.1f}"/>' for cx, cy, r in shape)
    ybot = max(y + cy + r for cx, cy, r in shape)
    ytop = min(y + cy - r for cx, cy, r in shape)
    xl = min(x + cx - r for cx, cy, r in shape) - 1
    xr = max(x + cx + r for cx, cy, r in shape) + 1
    ys = ybot - (ybot - ytop) * SHADOW_FRAC
    rnd = random.Random(seed ^ 0x5A5A)
    pts, xx = [], xl
    while xx < xr:
        w = max(3.0, (xr - xl) * rnd.uniform(0.22, 0.4))
        hk = rnd.uniform(*UP_ARC_H) * SHADOW_ARC_RATIO
        for s in range(ARC_SAMPLES + 1):
            if pts and s == 0:
                continue
            tt = s / ARC_SAMPLES
            pts.append((xx + w * tt, ys - w * hk * math.sin(math.pi * tt)))
        xx += w
    sh = path_d(pts + [(xr + 3, ybot + 1), (xl, ybot + 1)])
    op = opacity(c_pct) * (THIN_RATIO if thin else 1.0)
    cls = "cloud-clump cloud-thin-clump" if thin else "cloud-clump"
    return (f'<g class="{cls}" opacity="{op:.3f}" fill="{CLOUD_FILL}">{t}<defs><clipPath id="cc{cid}">{circles}</clipPath></defs>'
            f'{circles}<path class="cloud-shadow" d="{sh}" fill="{SHADOW_FILL}" clip-path="url(#cc{cid})"/></g>')

def hide_amount(summit_cloud, sea_hits) -> float:
    """sea_hits: その山より手前の雲海の帯で、上の縁が山頂より上にあるものの雲量の一覧。"""
    vals = [float(v) for v in sea_hits]
    if summit_cloud is not None:
        vals.append(float(summit_cloud))
    return max(vals) if vals else 0.0

def name_opacity(amount_pct, span=NAME_FADE_SPAN) -> float:
    a = max(0.0, min(100.0, float(amount_pct)))
    return round(1.0 - span * a / 100.0, 3)


# ---- 元: experiments/panorama/stage38_clouds.py ----


FADE_DEG = 15.0                  # 3.1 帯の端の濃さ・厚みを下げる幅(仮)

NEAR_THIN_KM = 10.0              # 3.2 これ以下の区切りの雲海は、うすい雲で(仮)

NEAR_THIN_RATIO = THIN_RATIO  # その濃さ = 帯の × これ(段階3.6 の倍率 0.35)

STEP = 10.0

SEA_END_DROP_PX = 40.0           # 雲海の帯の端で上の縁が下がる量の上限(画素。仮。指示書に無い値)

def smoothstep(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u * u * (3.0 - 2.0 * u)

def crossing_offset(c_end: float, c_nb, start_pct=START_PCT, step=STEP) -> float:
    """端の列の雲量 c_end(描き始め以上)から、隣の方位の雲量 c_nb(帯の外。None は 0)へ線形に下げたとき、
    描き始めを下回る所までの方位の差(0〜step)。"""
    nb = 0.0 if c_nb is None else max(0.0, float(c_nb))
    c_end = float(c_end)
    if nb >= start_pct:          # 隣も描き始め以上(別の帯につながる)なら、隣の列のちょうど手前まで
        return step
    if c_end <= start_pct:
        return 0.0
    return step * (c_end - start_pct) / (c_end - nb)

def run_ends(cols, full, nb_left, nb_right, start_pct=START_PCT, step=STEP):
    """帯の「終わり」の方位(ほどいた方位)。全周なら None, None。"""
    if full:
        return None, None
    a_l = cols[0]["az"] - crossing_offset(cols[0]["c"], nb_left, start_pct, step)
    a_r = cols[-1]["az"] + crossing_offset(cols[-1]["c"], nb_right, start_pct, step)
    return a_l, a_r

def edge_factor(a: float, a_l, a_r, width=FADE_DEG) -> float:
    """端の係数(0〜1)。終わり a_l・a_r を真ん中にした width の幅で、smoothstep で 0 まで。全周(None)なら 1。"""
    if a_l is None:
        return 1.0
    h = width / 2.0
    return min(smoothstep((a - (a_l - h)) / width), smoothstep(((a_r + h) - a) / width))

def extent(a_l, a_r, width=FADE_DEG):
    h = width / 2.0
    return a_l - h, a_r + h

def c_along(cols, full, a, nb_left, nb_right, step=STEP) -> float:
    """方位 a の雲量: 帯の列の間は線形、端の列の外は隣の方位の値(None は 0)へ step の間で線形、その先は隣の値。"""
    if full:
        return _interp_cols(cols, True, a, "c", step)
    a0, a1 = cols[0]["az"], cols[-1]["az"]
    # 隣の値が端の列より大きい(隣は別の種類の雲: 視点の高さをまたぐ帯など)ときは、上げずに端の値のまま(端の係数で下げる)
    nb_left = None if nb_left is None else min(float(nb_left), cols[0]["c"])
    nb_right = None if nb_right is None else min(float(nb_right), cols[-1]["c"])
    if a < a0:
        nb = 0.0 if nb_left is None else float(nb_left)
        t = min(1.0, (a0 - a) / step)
        return cols[0]["c"] * (1 - t) + nb * t
    if a > a1:
        nb = 0.0 if nb_right is None else float(nb_right)
        t = min(1.0, (a - a1) / step)
        return cols[-1]["c"] * (1 - t) + nb * t
    return _interp_cols(cols, False, a, "c", step)

def alpha_along(cols, full, a, nb_left, nb_right, a_l, a_r, width=FADE_DEG, step=STEP) -> float:
    """方位 a の帯の不透明度 = 雲量の不透明度 × 端の係数。"""
    return opacity(max(0.0, c_along(cols, full, a, nb_left, nb_right, step))) * edge_factor(a, a_l, a_r, width)

def stop_azimuths(cols, full, a_l, a_r, width=FADE_DEG, step=STEP) -> list:
    """グラデーションの止まりの方位: 格子の点と、端の width の範囲の 1° ごと(と範囲の両端)。昇順・重複なし。"""
    if full:
        a0 = cols[0]["az"]
        return [a0 - step / 2.0] + [c["az"] for c in cols] + [a0 + 360.0 - step / 2.0]
    x0, x1 = extent(a_l, a_r, width)
    s = {round(x0, 3), round(x1, 3)}
    s |= {round(c["az"], 3) for c in cols if x0 <= c["az"] <= x1}
    for lo, hi in ((x0, x0 + width), (x1 - width, x1)):
        k = math.ceil(lo)
        while k <= hi:
            if x0 <= k <= x1:
                s.add(float(k))
            k += 1
    # 帯の列から端の外の隣の方位(±step)までの、雲量の折れ目
    for a in (cols[0]["az"] - step, cols[-1]["az"] + step):
        if x0 < a < x1:
            s.add(round(a, 3))
    return sorted(s)

def stops38(cols, full, nb_left, nb_right, a_l, a_r, alpha_scale=1.0, width=FADE_DEG, step=STEP) -> list:
    """段階3.7 の _stops と同じ形 [(offset, 帯の(うすい雲と重ねる前の)不透明度, うすい雲の不透明度, 帯の不透明度)]。
    alpha_scale: 3.2 の近い区切り(うすい雲で描く)のときの倍率。"""
    azs = stop_azimuths(cols, full, a_l, a_r, width, step)
    x0, x1 = azs[0], azs[-1]
    span = (x1 - x0) or 1.0
    out = []
    for a in azs:
        ab = alpha_along(cols, full, a, nb_left, nb_right, a_l, a_r, width, step) * alpha_scale
        has_thin = _nearest(cols, full, a, step).get("thin_top") is not None
        at_ = THIN_RATIO * ab if has_thin else 0.0
        body = (ab - at_) / (1.0 - at_) if at_ < 1.0 else 0.0
        out.append((round((a - x0) / span, 4), round(body, 3), round(at_, 3), round(ab, 3)))
    return out

def _nearest(cols, full, a, step=STEP):
    m = len(cols)
    p = (a - cols[0]["az"]) / step
    i = int(round(p))
    return cols[i % m] if full else cols[max(0, min(m - 1, i))]

def neighbor_sea_c(profile: dict, j: int, viewer_m: float):
    """雲海の帯の外の方位 j の「同じ高さの範囲」の雲量: 視点より低い面の雲量の最大(無ければ None)。"""
    vals = [profile["c"][k][j] for k in range(len(profile["hpas"]))
            if profile["c"][k][j] is not None and profile["alt"][k][j] is not None and profile["alt"][k][j] < viewer_m]
    return max(vals) if vals else None

def neighbor_band_c(profile: dict, j: int, lo: int, hi: int):
    """視点の高さをまたぐ・上の帯の外の方位 j の、端の列と同じ面(lo〜hi)の雲量の最大(無ければ None)。"""
    vals = [profile["c"][k][j] for k in range(lo, hi + 1) if profile["c"][k][j] is not None]
    return max(vals) if vals else None

def attach_neighbors_sea(runs: list, profile: dict, viewer_m: float, step=STEP):
    """sea_runs の各帯に nb_left・nb_right(隣の方位の雲量)を付ける。"""
    n = len(profile["azs"])
    for run in runs:
        if run["full"]:
            run["nb"] = (None, None)
            continue
        jl = (run["cols"][0]["j"] - 1) % n
        jr = (run["cols"][-1]["j"] + 1) % n
        run["nb"] = (neighbor_sea_c(profile, jl, viewer_m), neighbor_sea_c(profile, jr, viewer_m))
    return runs

def attach_neighbors_band(bands: list, profile: dict):
    n = len(profile["azs"])
    for b in bands:
        if b["full"]:
            b["nb"] = (None, None)
            continue
        c0, c1 = b["cols"][0], b["cols"][-1]
        b["nb"] = (neighbor_band_c(profile, (c0["j"] - 1) % n, c0["lo"], c0["hi"]),
                   neighbor_band_c(profile, (c1["j"] + 1) % n, c1["lo"], c1["hi"]))
    return bands

def sea_band_geometry38(run, y_of_alt, x_of_az, px_per_deg, y_bottom, pats, scale=1.0, near_thin=False,
                        start_pct=START_PCT, width=FADE_DEG, step=STEP) -> dict:
    """run: sea_runs の 1 つに nb を付けたもの。上の縁の弧の頂は雲の上面の見上げ角(段階3.7 と同じ)。
    端: 終わりを真ん中にした width の幅で、濃さを 0 まで下げ、上の縁を雲の下の面の高さまで下げる(厚みが細くなる)。
    near_thin: 3.2 の近い区切り。うすい雲(濃さ × NEAR_THIN_RATIO、影なし)で描く。"""
    cols = run["cols"]
    full = run["full"]
    nb_l, nb_r = run.get("nb", (None, None))
    a_l, a_r = run_ends(cols, full, nb_l, nb_r, start_pct, step)
    if full:
        a0, a1 = band_extent({"cols": cols, "full": True}, step)
    else:
        a0, a1 = extent(a_l, a_r, width)
    for c in cols:
        c["_thin_add"] = (c["thin_top"] - c["top"]) if c["thin_top"] is not None else 0.0
        c["_has_thin"] = 1.0 if c["thin_top"] is not None else 0.0
    x0, x1 = x_of_az(a0), x_of_az(a1)

    def a_of_x(x):
        return a0 + (x - x0) / px_per_deg

    def at(x):
        a = a_of_x(x)
        cc = max(0.0, c_along(cols, full, a, nb_l, nb_r, step))
        f = edge_factor(a, a_l, a_r, width)
        top = _interp(cols, full, a, "top", step)
        bot = _interp(cols, full, a, "bottom", step)
        # 厚みが細くなる: 上の縁が下の面(図の下の端より下なら下の端)へ向かって下がる。下がる量は SEA_END_DROP_PX まで
        # (下の面が図のずっと下にあると、上の縁が図の下の端まで斜めの線になって見えたため。仮)
        y_full = y_of_alt(top)
        y_floor = min(y_of_alt(bot), y_bottom)
        drop = min(max(0.0, y_floor - y_full), SEA_END_DROP_PX) * (1.0 - f)
        y_top = y_full + drop
        has_thin = _interp(cols, full, a, "_has_thin", step) > 0
        y_thin = (y_of_alt(top + _interp(cols, full, a, "_thin_add", step)) + drop) if has_thin else None
        amp = (0.45 + 0.55 * cc / 100.0) * (0.5 + 0.5 * f)
        return cc, f, y_top, y_thin, amp
    upper, thin_up, shadow_up = [], [], []
    for x in pats["up"].samples(x0, x1):
        cc, f, y_top, y_thin, amp = at(x)
        hmax = pats["up"].bump_max(x) * amp
        y = y_top + hmax - pats["up"].bump(x) * amp
        upper.append((round(x, 1), round(min(y, y_bottom), 1)))
        ys = y_top + SEA_SHADOW_PX * scale + pats["shadow"].bump_max(x) * amp * SHADOW_ARC_RATIO \
            - pats["shadow"].bump(x) * amp * SHADOW_ARC_RATIO
        ys = max(ys, y + 2.0)
        shadow_up.append((round(x, 1), round(min(ys, y_bottom) if not near_thin else y_bottom, 1)))
    if any(c["thin_top"] is not None for c in cols):
        for x in pats["thin"].samples(x0, x1):
            cc, f, y_top, y_thin, amp = at(x)
            yb_ = y_top + pats["up"].bump_max(x) * amp - pats["up"].bump(x) * amp
            if y_thin is None:
                yt = yb_
            else:
                h = min(pats["thin"].bump(x) * amp, max(0.0, yb_ - y_thin) * 0.8 + 1.5)
                yt = min(y_thin - h, yb_)
            thin_up.append((round(x, 1), round(min(yt, y_bottom), 1)))
    k = NEAR_THIN_RATIO if near_thin else 1.0
    stops = (stops38(cols, full, nb_l, nb_r, a_l, a_r, k, width, step) if not full
             else _scale_stops(_stops(cols, True, a0, a1, step), k))
    return {"upper": upper, "thin_upper": thin_up or None, "shadow_upper": shadow_up, "x0": x0, "x1": x1,
            "y_bottom": y_bottom, "stops": stops, "a0": a0, "a1": a1, "a_l": a_l, "a_r": a_r, "kind": "sea",
            "near_thin": near_thin}

def _scale_stops(stops, k):
    """全周の帯の止まりに倍率 k(3.2 の近い区切り)。帯とうすい雲を重ねた値が k 倍になるように付け直す。"""
    if k == 1.0:
        return stops
    out = []
    for o, _, at_, ab in stops:
        ab2, at2 = ab * k, at_ * k
        body = (ab2 - at2) / (1.0 - at2) if at2 < 1.0 else 0.0
        out.append((o, round(body, 3), round(at2, 3), round(ab2, 3)))
    return out

def upper_band_geometry38(band, y_of_alt, x_of_az, px_per_deg, pats, start_pct=START_PCT, width=FADE_DEG,
                          step=STEP) -> dict:
    """段階3.7 の upper_band_geometry と同じ決まり。端のつぼめ(段階3.6 の 5°)を、端の係数(width の幅)に替えた。"""
    cols = band["cols"]
    full = band["full"]
    nb_l, nb_r = band.get("nb", (None, None))
    a_l, a_r = run_ends(cols, full, nb_l, nb_r, start_pct, step)
    if full:
        a0, a1 = band_extent(band, step)
    else:
        a0, a1 = extent(a_l, a_r, width)
    for c in cols:
        c["_thin_add"] = (c["thin_top"] - c["top"]) if c["thin_top"] is not None else 0.0
        c["_has_thin"] = 1.0 if c["thin_top"] is not None else 0.0
    x0, x1 = x_of_az(a0), x_of_az(a1)

    def a_of_x(x):
        return a0 + (x - x0) / px_per_deg

    def edges(x):
        a = a_of_x(x)
        cc = max(0.0, c_along(cols, full, a, nb_l, nb_r, step))
        pad = PAD_MIN_PX + PAD_SPAN_PX * cc / 100.0
        yt = y_of_alt(_interp(cols, full, a, "top", step)) - pad
        yb = y_of_alt(_interp(cols, full, a, "bottom", step)) + pad * 0.6
        has_thin = _interp(cols, full, a, "_has_thin", step) > 0
        ythin = y_of_alt(_interp(cols, full, a, "top", step) + _interp(cols, full, a, "_thin_add", step)) \
            if has_thin else yt
        tf = edge_factor(a, a_l, a_r, width)
        mid = (yt + yb) / 2.0
        up = mid - (mid - yt) * tf
        lo = mid + (yb - mid) * tf
        th = min(mid - (mid - ythin) * tf, up)
        amp = (0.45 + 0.55 * cc / 100.0) * (0.5 + 0.5 * tf)
        return up, lo, th, tf, amp
    upper, lower, thin, shadow = [], [], [], []
    for x in pats["up"].samples(x0, x1):
        up, lo, th, tf, amp = edges(x)
        h = min(pats["up"].bump(x) * amp * tf, (lo - up) * 1.2 + 6.0)
        upper.append((round(x, 1), round(up - h, 1)))
        ys = lo - (lo - up) * SHADOW_FRAC
        hs = pats["shadow"].bump(x) * amp * tf * SHADOW_ARC_RATIO
        shadow.append((round(x, 1), round(min(ys - hs, lo), 1)))
    for x in pats["lo"].samples(x0, x1):
        up, lo, th, tf, amp = edges(x)
        h = pats["lo"].bump(x) * amp * tf
        lower.append((round(x, 1), round(lo + h, 1)))
    if any(c["thin_top"] is not None for c in cols):
        for x in pats["thin"].samples(x0, x1):
            up, lo, th, tf, amp = edges(x)
            dip = pats["thin"].dip_at(x)
            base = th + (up - th) * dip
            h = min(pats["thin"].bump(x) * amp * tf, max(0.0, up - base) * 0.8 + 1.5)
            thin.append((round(x, 1), round(min(base - h, up), 1)))
    stops = stops38(cols, full, nb_l, nb_r, a_l, a_r, 1.0, width, step) if not full else _stops(cols, True, a0, a1, step)
    return {"upper": upper, "lower": lower, "thin_upper": thin or None, "shadow_upper": shadow, "x0": x0, "x1": x1,
            "stops": stops, "a0": a0, "a1": a1, "a_l": a_l, "a_r": a_r, "kind": "upper"}

NEAR_TOP_KM = 3.0                 # 足もとの地形(段階2.5 の決まり)

NEAR_CLIP_TEXT = "足もとの地形(上は省略)"

def clamp_near_peaks(peaks: list, far_top_max: float, near_km=NEAR_TOP_KM):
    """図の上端 = 3km より遠い地形のいちばん高い点と、3km より遠い山の点の高い方。3km 以内の山の点は、上端より上なら
    上端に置く(図の外に出さない)。返り値: (新しい peaks, 上端の角度, 上端に置いた山の名前)。"""
    far = [p["draw_angle"] for p in peaks if p["dist_km"] > near_km]
    top = max([float(far_top_max)] + far)
    out, clamped = [], []
    for p in peaks:
        if p["dist_km"] <= near_km and p["draw_angle"] > top:
            out.append(dict(p, draw_angle=top))
            clamped.append(p["label"])
        else:
            out.append(p)
    return out, top, clamped


# ---- 元: experiments/panorama/stage39_clouds.py ----

MERGE_DEG = 4.0              # 4 章: この方位の幅以内に並ぶ山のかたまりを 1 つにまとめる(仮)

SPREAD_RANGE = (0.85, 1.2)   # 4 章: 左右の広がり(幅の倍率。仮)

BAND_MERGE_PX = 6.0          # 5 章: 上の縁の高さがこれ以内の帯は 1 本にまとめる(仮)

BAND_ALPHA_CAP = 0.85        # 5 章: 同じ方位で重なる帯の濃さの合計の上限(仮)

SAME_SHAPE_NEIGHBOR_DEG = 8.0    # 確かめ: 「となり」とみなす方位の差(数えるためだけ)

SAME_SHAPE_TOL = 0.1             # 確かめ: 幅・高さがこの割合以内なら同じ大きさ

OPTS = {"clumps": True, "bands": False}   # 5 章は既定にしない(前と後のどちらが良いか迷うため。samples/ で利用者が選ぶ)

def clump_shape39(seed: int, width_px: float, height_px: float) -> list:
    """段階3.6 の clump_shape と同じ作り(下の段の丸 + 上の丸)で、丸の数・大きさ・左右の広がり・上下の位置を山ごとに変える。"""
    rnd = random.Random(seed)
    spread = rnd.uniform(*SPREAD_RANGE)
    w = float(width_px) * spread
    h = max(float(height_px), float(width_px) * 0.3)
    dy = rnd.uniform(-0.12, 0.12) * h
    out = []
    nb = rnd.randint(2, 5)
    rb = rnd.uniform(0.2, 0.34)
    for i in range(nb):
        t = (i + 0.5) / nb
        r_ = h * rb * rnd.uniform(0.85, 1.15)
        cx = (t - 0.5) * (w - 2 * r_) + rnd.uniform(-0.08, 0.08) * w
        out.append((cx, h / 2 - r_ + dy, r_))
    nt = rnd.randint(1, 6)
    lean = rnd.uniform(-0.15, 0.15)
    for _ in range(nt):
        r_ = h * rnd.uniform(0.2, 0.47)
        cx = (rnd.uniform(-0.34, 0.34) + lean) * w
        cy = rnd.uniform(-0.2, 0.22) * h + dy
        out.append((cx, cy, r_))
    return [(round(x, 2), round(y, 2), round(r_, 2)) for x, y, r_ in out]

def group_neighbors(specs, px, merge_deg=MERGE_DEG):
    """specs: [{"x", ...}](同じ層・同じ種類)。x の順に並べ、となりとの差が merge_deg 以内なら同じまとまり。"""
    s = sorted(specs, key=lambda q: q["x"])
    out = []
    for q in s:
        if out and q["x"] - out[-1][-1]["x"] <= merge_deg * px + 1e-9:
            out[-1].append(q)
        else:
            out.append([q])
    return out

def _shadow_path(xl, xr, ys_fn, ybot, rnd):
    pts, xx = [], xl
    while xx < xr:
        wd = max(3.0, (xr - xl) * rnd.uniform(0.12, 0.3)) if xr - xl > 60 else max(3.0, (xr - xl) * rnd.uniform(0.22, 0.4))
        hk = rnd.uniform(*UP_ARC_H) * SHADOW_ARC_RATIO
        for s in range(ARC_SAMPLES + 1):
            if pts and s == 0:
                continue
            tt = s / ARC_SAMPLES
            x = xx + wd * tt
            pts.append((x, ys_fn(x) - wd * hk * math.sin(math.pi * tt)))
        xx += wd
    return path_d(pts + [(xr + 3, ybot + 1), (xl, ybot + 1)])

def clump_svg39(x, y, c_pct, width_px, height_px, seed, title="", cid="", thin=False) -> str:
    """1 つの山のかたまり(形は clump_shape39)。それ以外は段階3.7 の clump_svg37 と同じ作り。"""
    t = f"<title>{title}</title>" if title else ""
    shape = clump_shape39(seed, width_px, height_px)
    circles = "".join(f'<circle cx="{x + cx:.1f}" cy="{y + cy:.1f}" r="{r_:.1f}"/>' for cx, cy, r_ in shape)
    ybot = max(y + cy + r_ for cx, cy, r_ in shape)
    ytop = min(y + cy - r_ for cx, cy, r_ in shape)
    xl = min(x + cx - r_ for cx, cy, r_ in shape) - 1
    xr = max(x + cx + r_ for cx, cy, r_ in shape) + 1
    ys = ybot - (ybot - ytop) * SHADOW_FRAC
    sh = _shadow_path(xl, xr, lambda _x: ys, ybot, random.Random(seed ^ 0x5A5A))
    op = opacity(c_pct) * (THIN_RATIO if thin else 1.0)
    cls = "cloud-clump cloud-thin-clump" if thin else "cloud-clump"
    return (f'<g class="{cls}" opacity="{op:.3f}" fill="{CLOUD_FILL}">{t}<defs><clipPath id="cc{cid}">{circles}</clipPath></defs>'
            f'{circles}<path class="cloud-shadow" d="{sh}" fill="{SHADOW_FILL}" clip-path="url(#cc{cid})"/></g>'), \
        {"w": xr - xl, "h": ybot - ytop, "n": len(shape)}

def merged_clump_svg(members, cid, thin=False) -> tuple:
    """members: x の順の [{"x","y","c","w","h","seed","key","tt"}](2 つ以上)。1 つのひと続きの雲。"""
    circles = []
    for m in members:
        circles += [(m["x"] + cx, m["y"] + cy, r_) for cx, cy, r_ in clump_shape39(m["seed"], m["w"], m["h"])]
    for a, b in zip(members, members[1:]):
        rnd = random.Random(seed_of("bridge", a["key"], b["key"]))
        hh = (a["h"] + b["h"]) / 2.0
        n = max(1, int(math.ceil((b["x"] - a["x"]) / max(4.0, 0.5 * hh))))
        for i in range(1, n + 1):
            t = i / (n + 1.0)
            x = a["x"] + (b["x"] - a["x"]) * t
            y = a["y"] + (b["y"] - a["y"]) * t
            h = a["h"] + (b["h"] - a["h"]) * t
            circles.append((x + rnd.uniform(-0.1, 0.1) * h, y + rnd.uniform(-0.12, 0.05) * h, h * rnd.uniform(0.26, 0.4)))
            circles.append((x + rnd.uniform(-0.15, 0.15) * h, y + h * rnd.uniform(0.12, 0.24), h * rnd.uniform(0.22, 0.32)))
    xl = min(x - r_ for x, y, r_ in circles) - 1
    xr = max(x + r_ for x, y, r_ in circles) + 1
    ytop = min(y - r_ for x, y, r_ in circles)
    ybot = max(y + r_ for x, y, r_ in circles)
    xs = [m["x"] for m in members]
    ysh = []
    for m in members:
        cs = [(x, y, r_) for x, y, r_ in circles if abs(x - m["x"]) <= m["w"]]
        b_ = max(y + r_ for x, y, r_ in cs)
        t_ = min(y - r_ for x, y, r_ in cs)
        ysh.append(b_ - (b_ - t_) * SHADOW_FRAC)
    sh = _shadow_path(xl, xr, lambda x: float(np.interp(x, xs, ysh)), ybot, random.Random(seed_of("mshadow", members[0]["key"])))
    k = THIN_RATIO if thin else 1.0
    stops = "".join(f'<stop offset="{(m["x"] - xl) / (xr - xl):.4f}" stop-color="#fff" stop-opacity="{opacity(m["c"]) * k:.3f}"/>'
                    for m in members)
    circ = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r_:.1f}"/>' for x, y, r_ in circles)
    t = "<title>" + html.escape(" / ".join(html.unescape(m["tt"]) for m in members)) + "</title>"
    defs = (f'<linearGradient id="mcg{cid}" gradientUnits="userSpaceOnUse" x1="{xl:.1f}" y1="0" x2="{xr:.1f}" y2="0">{stops}'
            f'</linearGradient><mask id="mcm{cid}" maskUnits="userSpaceOnUse" x="{xl - 1:.1f}" y="{ytop - 2:.1f}" '
            f'width="{xr - xl + 2:.1f}" height="{ybot - ytop + 4:.1f}"><rect x="{xl - 1:.1f}" y="{ytop - 2:.1f}" '
            f'width="{xr - xl + 2:.1f}" height="{ybot - ytop + 4:.1f}" fill="url(#mcg{cid})"/></mask>'
            f'<clipPath id="cc{cid}">{circ}</clipPath>')
    cls = "cloud-clump cloud-clump-merged" + (" cloud-thin-clump" if thin else "")
    svg = (f'<g class="{cls}" fill="{CLOUD_FILL}">{t}<defs>{defs}</defs><g mask="url(#mcm{cid})">{circ}'
           f'<path class="cloud-shadow" d="{sh}" fill="{SHADOW_FILL}" clip-path="url(#cc{cid})"/></g></g>')
    return svg, {"w": xr - xl, "h": ybot - ytop, "n": len(circles)}

def same_shape_runs(objs, px, deg=SAME_SHAPE_NEIGHBOR_DEG, tol=SAME_SHAPE_TOL):
    """objs: [{"x","w","h","bi","n"}](1 つの状態)。同じ層で、x の順にとなり(deg 以内)どうしが同じ大きさ(幅・高さが tol 以内)
    で並ぶ所(2 つ以上の続き)の数。"""
    n = 0
    by = {}
    for o in objs:
        by.setdefault(o["bi"], []).append(o)
    for lst in by.values():
        lst.sort(key=lambda o: o["x"])
        run = 1
        for a, b in zip(lst, lst[1:]):
            same = (b["x"] - a["x"] <= deg * px and abs(a["w"] - b["w"]) <= tol * max(a["w"], b["w"])
                    and abs(a["h"] - b["h"]) <= tol * max(a["h"], b["h"]))
            if same:
                run += 1
                if run == 2:
                    n += 1
            else:
                run = 1
    return n

def _interp_upper(geom, x):
    up = sorted(geom["upper"])
    xs = [p[0] for p in up]
    if x < xs[0] or x > xs[-1]:
        return None
    return float(np.interp(x, xs, [p[1] for p in up]))

def _alpha_at(geom, x, idx=3):
    """止まりの idx 番目(1: 帯の白い面の mask・2: うすい雲・3: 帯の不透明度)を x で。"""
    x0, x1 = geom["x0"], geom["x1"]
    if not (x0 <= x <= x1) or x1 <= x0:
        return 0.0
    o = (x - x0) / (x1 - x0)
    offs = [s[0] for s in geom["stops"]]
    return float(np.interp(o, offs, [s[idx] for s in geom["stops"]]))

def band_factors(bands, X, px, period, merge_px=BAND_MERGE_PX, cap=BAND_ALPHA_CAP, fade_deg=None):
    """bands: [{"bi", "geom", "wrap"}](1 つの状態の雲海の帯)。返り値: (帯ごとの係数の配列(方位 1° ごと、0〜360)・
    まとめた方位の数・上限で薄めた方位の数)。係数は 0〜1(まとめた 2 本のうすいほうは 0)。"""
    fade_deg = FADE_DEG if fade_deg is None else fade_deg
    n = len(bands)
    fac = np.ones((n, 360))
    n_merge = n_cap = 0
    for a in range(360):
        x = X(a + 0.5)
        cov = []
        for k, b in enumerate(bands):
            for dx in [0.0] + list(b["wrap"] or []):
                xx = x - dx
                y = _interp_upper(b["geom"], xx)
                al = _alpha_at(b["geom"], xx, 3)
                if y is not None and al > 0:
                    cov.append([k, b["bi"], y, al])
                    break
        if len(cov) < 2:
            continue
        cov.sort(key=lambda q: q[1])            # 手前(区切りの近い)から
        merged = False
        for i in range(len(cov)):
            for j in range(i + 1, len(cov)):
                if cov[j][3] > 0 and cov[i][3] > 0 and abs(cov[i][2] - cov[j][2]) <= merge_px:
                    # 濃いほうを残し、うすいほうを 0 に(近い区切りのうすい雲に、奥の濃い帯をまとめて消さないように)
                    lo_ = j if cov[j][3] <= cov[i][3] else i
                    fac[cov[lo_][0], a] = 0.0
                    cov[lo_][3] = 0.0
                    merged = True
        n_merge += merged
        # 上限: 雲海の帯は下の端が図の下の端まで続くので、同じ方位の帯はすべて縦に重なる
        tot = 1.0 - np.prod([1.0 - q[3] for q in cov])
        if tot > cap + 1e-9:
            n_cap += 1
            for q in sorted(cov, key=lambda q: -q[1]):          # 奥から
                if q[3] <= 0:
                    continue
                rest = 1.0 - np.prod([1.0 - p[3] for p in cov if p is not q])
                # 1 − (1 − rest)(1 − a') = cap → a' = 1 − (1 − cap)/(1 − rest)
                need = 1.0 - (1.0 - cap) / max(1e-9, 1.0 - rest) if rest < cap else 0.0
                new = max(0.0, min(q[3], need))
                fac[q[0], a] *= (new / q[3]) if q[3] > 0 else 0.0
                q[3] = new
                tot = 1.0 - np.prod([1.0 - p[3] for p in cov])
                if tot <= cap + 1e-9:
                    break
    # 方位に沿ってなめらかに: まず幅 fade_deg の中の最小をとり(薄めた所のまわりを先に薄める)、次に同じ幅の三角形の重みで
    # ならす(全周でつなぐ)。こうすると、薄めた方位の値は元の係数を超えない(上限を守る)。
    half = max(1, int(round(fade_deg / 2.0)))
    ker = np.array([half + 1 - abs(i) for i in range(-half, half + 1)], dtype=float)
    ker /= ker.sum()
    out = np.empty_like(fac)
    for k in range(n):
        ext = np.concatenate([fac[k, -2 * half:], fac[k], fac[k, :2 * half]])
        er = np.array([ext[i - half:i + half + 1].min() for i in range(half, len(ext) - half)])
        out[k] = np.convolve(er, ker, mode="valid")
    return out, n_merge, n_cap

def apply_factor(geom, fac_k, X, px, period):
    """帯の濃さの止まり(stops)を、方位 1° ごとの係数 fac_k で掛け直す(止まりは元の止まりと、1° ごとの点)。"""
    if np.allclose(fac_k, 1.0):
        return geom
    x0, x1 = geom["x0"], geom["x1"]
    offs = sorted(set([s[0] for s in geom["stops"]] +
                      [((X(a + 0.5) + dk * period) - x0) / (x1 - x0) for a in range(360) for dk in (-1, 0, 1)
                       if x0 <= X(a + 0.5) + dk * period <= x1]))
    st = []
    for o in offs:
        x = x0 + o * (x1 - x0)
        a = int(math.floor(((x - X(0)) / px) % 360.0))
        f = float(fac_k[a])
        st.append((o,) + tuple(min(1.0, _alpha_at(geom, x, i) * f) for i in (1, 2, 3)))
    g = dict(geom)
    g["stops"] = st
    return g
