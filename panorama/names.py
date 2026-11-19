"""展望図の部品: 山名の材料(「日本の主な山岳標高」の表があれば使い、無ければ MOUNTAINS だけ)・山名辞書(data/)・呼び名・格。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

from collections import Counter
from mountain_weather_core import MOUNTAINS
import glob
import json
import os
import re
import requests

from . import config, geometry  # noqa: E402


# ---- 元: experiments/panorama/stage1_daibosatsu.py ----

class GsiMissing(Exception):
    """「日本の主な山岳標高」(1003 山)の表が手元に無い。"""


class StatusSession(requests.Session):
    """DemTileStore に渡す Session。応答の状態コードを記録するだけ(User-Agent は DemTileStore が付ける)。"""

    def __init__(self):
        super().__init__()
        self.statuses = []

    def get(self, url, **kw):
        try:
            r = super().get(url, **kw)
        except requests.RequestException as e:
            self.statuses.append(type(e).__name__)
            raise
        self.statuses.append(r.status_code)
        return r

def norm(s: str) -> str:
    s = s.strip()
    for a, b in (("ケ", "ヶ"), ("ッ", "ツ"), ("峯", "峰"), ("の", "ノ"), ("　", ""), (" ", "")):
        s = s.replace(a, b)
    return s

def name_variants(name: str) -> set:
    """「大雪山（ヌタプカウシペ）＜黒岳＞」「乗鞍岳(剣ヶ峰)」などから呼び名の候補を作る。"""
    out = set()
    base = re.sub(r"[（(].*?[）)]", "", name)
    base = re.sub(r"＜.*?＞", "", base)
    out.add(base)
    out.update(re.findall(r"[（(](.*?)[）)]", name))
    out.update(re.findall(r"＜(.*?)＞", name))
    return {norm(v) for v in out if v.strip()}

GSI_PAGE_URL = "https://www.gsi.go.jp/kihonjohochousa/kihonjohochousa41139.html"

def gsi_missing_message(gsi_dir):
    return (f"国土地理院「日本の主な山岳標高(1003山)」の GeoJSON が {gsi_dir} にありません。\n"
            f"  ブラウザで {GSI_PAGE_URL} を開き、GeoJSON の zip を取得して展開し、\n"
            f"  中の .geojson を cache/panorama/gsi_1003/ に置いてから、もう一度実行してください。\n"
            f"  (このスクリプトは国土地理院のサイトには接続しません)")

def load_gsi(gsi_dir=None):
    """保存済みの 1003山の GeoJSON を読む。無ければ GsiMissing(自分では取りに行かない。国土地理院のサイトは
    ふつうの接続では取れないため。指示書 T5)。"""
    gsi_dir = gsi_dir or config.gsi_dir()
    paths = sorted(glob.glob(os.path.join(gsi_dir, "*.geojson")))
    if not paths:
        raise GsiMissing(gsi_missing_message(gsi_dir))
    gj = json.load(open(paths[0], encoding="utf-8-sig"))
    out = []
    for f in gj["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        out.append({"src": "gsi1003", "id": f"gsi{p['連番']}", "name": p["山名＜山頂名＞"],
                    "yomi": p["山名よみ＜山頂名よみ＞"], "ele_m": float(p["標高値(m)"]),
                    "lat": float(lat), "lon": float(lon), "kind": p["種別"], "pref": p["都道府県"],
                    "variants": name_variants(p["山名＜山頂名＞"])})
    return out, os.path.basename(paths[0])

SUMMITS_FILE = "summits.json"


def summits_path(dict_dir=None):
    return os.path.join(dict_dir or config.DATA_DIR, SUMMITS_FILE)


def load_summits(dict_dir=None):
    """リポジトリに入れた山頂の点(日本百名山と 76 座に当たる点だけ。tools/build_panorama_summits.py が作る)を、
    load_gsi と同じ形の材料で返す。返り値: (材料, {連番の id: 呼び名})。ファイルが無ければ ([], {})。"""
    path = summits_path(dict_dir)
    if not os.path.exists(path):
        return [], {}
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    out, disp = [], {}
    for p in d["summits"]:
        mid = f"gsi{p['連番']}"
        out.append({"src": "gsi1003", "id": mid, "name": p["name"], "yomi": p["yomi"], "ele_m": float(p["ele_m"]),
                    "lat": float(p["lat"]), "lon": float(p["lon"]), "kind": p["kind"], "pref": p["pref"],
                    "variants": name_variants(p["name"]), "from_summits_file": True})
        if p.get("display"):
            disp[mid] = p["display"]
    return out, disp


def load_materials():
    """返り値: (材料, {gsi_file, n_gsi, pairs, summits_only, summit_display})。
    1003 山の表が手元にあれば表の全部を材料にする(そのほかの山の名前も出る)。
    無ければ、リポジトリの山頂の点(summits.json。百名山と 76 座だけ)と MOUNTAINS を材料にする。"""
    summit_display = {}
    try:
        gsi, gsi_file = load_gsi()
        summits_only = False
    except GsiMissing:
        gsi, summit_display = load_summits()
        gsi_file = None
        summits_only = True
    mts = []
    for m in MOUNTAINS:
        mts.append({"src": "MOUNTAINS", "id": m["id"], "name": m["name"], "ele_m": float(m["elevation_m"]),
                    "lat": m["lat"], "lon": m["lon"], "variants": name_variants(m["name"])})
    # MOUNTAINS と 1003山の対応(名前が重なり 3km 以内、なければ 500m 以内の最寄り)
    pairs = []
    only_mts = []
    for m in mts:
        best = None
        for q in gsi:
            d, _ = geometry.vincenty_inverse(m["lat"], m["lon"], q["lat"], q["lon"])
            named = bool(m["variants"] & q["variants"]) or any(
                v in w or w in v for v in m["variants"] for w in q["variants"] if len(v) >= 2 and len(w) >= 2)
            if (named and d <= 3000) or d <= 500:
                score = d - (10000 if named else 0)
                if best is None or score < best[0]:
                    best = (score, q, d, named)
        if best:
            q, d = best[1], best[2]
            m["gsi_id"] = q["id"]
            q.setdefault("mountains_ids", []).append(m["id"])
            pairs.append({"mountains": f"{m['id']} {m['name']}", "gsi": q["name"], "offset_m": round(d, 1),
                          "ele_mountains": m["ele_m"], "ele_gsi": q["ele_m"], "name_match": best[3]})
        else:
            only_mts.append(m)
    materials = gsi + only_mts   # 両方にある山は 1003山を優先
    return materials, {"gsi_file": gsi_file, "n_gsi": len(gsi), "pairs": pairs, "summits_only": summits_only,
                       "summit_display": summit_display}

def r(v, n=3):
    return None if v is None else round(float(v), n)


# ---- 元: experiments/panorama/stage15_common.py ----


# ---- 元: experiments/panorama/peak_dictionary.py ----


DICT_DIR = config.DATA_DIR

TIER_BIG, TIER_MID, TIER_SMALL = "大", "中", "小"

TIER_RANK = {TIER_BIG: 0, TIER_MID: 1, TIER_SMALL: 2}

def _load(name, dict_dir=DICT_DIR):
    with open(os.path.join(dict_dir, name), encoding="utf-8") as fh:
        return json.load(fh)

def load(dict_dir=DICT_DIR) -> dict:
    return {"hyakumeizan": _load("hyakumeizan.json", dict_dir)["mountains"],
            "m76": _load("mountains76.json", dict_dir)["mountains"],
            "similar": _load("similar_names.json", dict_dir)["groups"]}

def strip_decor(name: str) -> str:
    s = re.sub(r"＜.*?＞", "", name)
    s = re.sub(r"[（(].*?[）)]", "", s)
    return s.strip()

def summit_name(name: str):
    m = re.search(r"＜(.*?)＞", name)
    if not m:
        return None
    return re.sub(r"[（(].*?[）)]", "", m.group(1)).strip()

def tier_of(is_hyakumeizan: bool, is_m76: bool):
    """格の決まり(大・中。小は場所ごとに別に決める)。どれでもなければ None。"""
    if is_hyakumeizan:
        return TIER_BIG
    if is_m76:
        return TIER_MID
    return None

def key_of(m) -> tuple:
    return (m["name"], int(round(m["ele_m"])), m.get("pref"))

class PeakDictionary:
    def __init__(self, dict_dir=DICT_DIR):
        d = load(dict_dir)
        self.hy = d["hyakumeizan"]
        self.m76 = d["m76"]
        self.similar = d["similar"]
        self.hy_by_key = {(h["material"]["name"], h["material"]["ele_m"], h["material"]["pref"]): h for h in self.hy}
        self.m76_by_key = {}
        self.m76_by_id = {e["id"]: e for e in self.m76}
        for e in self.m76:
            if e["material"]:
                self.m76_by_key.setdefault((e["material"]["name"], e["material"]["ele_m"], e["material"]["pref"]),
                                           []).append(e)
        self._base_top = None

    def attach(self, materials: list) -> dict:
        """材料(1003 山 + MOUNTAINS だけの山)に印を付ける。返り値: 材料の id → 印。
        材料に無い百名山・76 座(対応が付かないもの)は missing に入れる。"""
        top = {}
        for m in materials:
            if m["src"] != "gsi1003":
                continue
            b = strip_decor(m["name"])
            if b not in top or m["ele_m"] > top[b]:
                top[b] = m["ele_m"]
        self._base_top = top
        out = {}
        seen_hy, seen_m76 = set(), set()
        for m in materials:
            hy, m76s = None, []
            if m["src"] == "gsi1003":
                k = key_of(m)
                hy = self.hy_by_key.get(k)
                m76s = self.m76_by_key.get(k, [])
            elif m["src"] == "MOUNTAINS":
                e = self.m76_by_id.get(m["id"])
                if e and e["match"] == "mountains_only":
                    m76s = [e]
            tier = tier_of(hy is not None, bool(m76s))
            if hy:
                seen_hy.add(hy["no"])
            for e in m76s:
                seen_m76.add(e["id"])
            out[m["id"]] = {"tier": tier, "display": self.display(m, hy, m76s),
                            "hyakumeizan_no": hy["no"] if hy else None, "hyakumeizan": hy["name"] if hy else None,
                            "m76_ids": [e["id"] for e in m76s], "m76_names": [e["display"] for e in m76s],
                            "uncertain": bool(hy and hy["uncertain"]),
                            "representative": hy.get("representative") if hy else None,
                            "alt_name": (m76s[0]["name"] if (not hy and m76s and m76s[0]["name"] != m76s[0]["display"])
                                         else None),
                            "own_name": summit_name(m["name"]) or strip_decor(m["name"])}
        self.missing = {"hyakumeizan": [h["name"] for h in self.hy if h["no"] not in seen_hy],
                        "m76": [e["id"] for e in self.m76 if e["id"] not in seen_m76]}
        return out

    def display(self, m, hy=None, m76s=()):
        if hy:
            return hy["name"]
        if m76s:
            return m76s[0]["display"]
        return self.default_display(m["name"], m["ele_m"])

    def default_display(self, name: str, ele: float) -> str:
        base = strip_decor(name)
        sn = summit_name(name)
        if sn and self._base_top is not None and ele < self._base_top.get(base, ele):
            return sn
        return base or name

def dedupe_labels(labels: list) -> list:
    """同じ図に同じ呼び名が 2 つ以上あるとき、格の低いほう(同じ格なら両方)に自分の峰の名前か標高を足す。
    labels: [{"display","tier","own_name","ele_m", ...}]。display を書き換えた写しを返す。"""
    out = [dict(x) for x in labels]
    by = {}
    for i, x in enumerate(out):
        by.setdefault(x["display"], []).append(i)
    for name, ids in by.items():
        if len(ids) < 2:
            continue
        best = min(TIER_RANK.get(out[i]["tier"], 9) for i in ids)
        n_best = sum(1 for i in ids if TIER_RANK.get(out[i]["tier"], 9) == best)
        for i in ids:
            x = out[i]
            alt = x.get("alt_name")       # 76 座は MOUNTAINS の name(例: 黒岳(御坂山地))
            own = x.get("own_name")
            if alt and alt != name:
                x["display"] = alt
            elif own and own != name:
                x["display"] = f"{name}({own})"
            elif not (TIER_RANK.get(x["tier"], 9) == best and n_best == 1):
                x["display"] = f"{name}({int(round(x['ele_m']))}m)"
    return out


# ---- 元: experiments/panorama/stage25_names.py ----

NAME_OVERRIDE_M76 = {"m002": "赤岳"}

def alias_parts(name: str) -> list:
    return [x.strip() for x in re.findall(r"[＜（(](.*?)[＞）)]", name)]

def shares_bigram(a: str, b: str) -> bool:
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    return bool(ga & gb)

def hyakumeizan_label(hy_name: str, material_name: str, same_name_count: int):
    """76 座でない百名山の呼び名と、その理由。返り値 (呼び名, 理由)。"""
    stripped = strip_decor(material_name)
    if stripped == hy_name:
        return hy_name, "同じ"
    if same_name_count >= 2:
        return hy_name, "段階2 のまま(1003 山に同じ山名が複数)"
    if hy_name in alias_parts(material_name):
        return hy_name, "段階2 のまま(百名山の呼び名が別名・山頂名の部分)"
    if not shares_bigram(hy_name, stripped):
        return hy_name, "段階2 のまま(名前に共通の部分が無い)"
    return stripped, "1003 山の山名"

def m76_label(entry: dict):
    if entry["id"] in NAME_OVERRIDE_M76:
        return NAME_OVERRIDE_M76[entry["id"]], f"例外(括弧を外すと「{entry['display']}」)"
    return entry["display"], "MOUNTAINS の name"

def apply(marks: dict, materials: list, dictionary: PeakDictionary) -> tuple:
    """段階2 の印(PeakDictionary.attach の返り値)の display を、段階2.5 の呼び名に直した写しと、変わった一覧を返す。"""
    cnt = Counter(strip_decor(m["name"]) for m in materials if m["src"] == "gsi1003")
    by_id = {m["id"]: m for m in materials}
    m76 = dictionary.m76_by_id
    out, changed = {}, []
    for mid, mk in marks.items():
        mk2 = dict(mk)
        if mk.get("m76_ids"):
            e = m76[mk["m76_ids"][0]]
            lab, why = m76_label(e)
            mk2["display"] = lab
            mk2["alt_name"] = e["name"] if e["name"] != lab else None
        elif mk.get("hyakumeizan"):
            mat = by_id[mid]
            lab, why = hyakumeizan_label(mk["hyakumeizan"], mat["name"], cnt[strip_decor(mat["name"])])
            mk2["display"] = lab
        else:
            out[mid] = mk2
            continue
        mk2["name_rule"] = why
        if mk2["display"] != mk["display"]:
            changed.append({"material_id": mid, "tier": mk["tier"], "stage2": mk["display"], "stage25": mk2["display"], "why": why})
        out[mid] = mk2
    return out, changed


# ---- 元: experiments/panorama/stage3_names.py ----

DISPLAY_OVERRIDE_M76 = {"m055": "御嶽山"}

def apply_overrides(marks: dict) -> tuple:
    """段階2.5 の印(stage25_names.apply の返り値)のうち、76 座の図に出す名前を表で直した写しと、変えた一覧。"""
    out, changed = {}, []
    for mid, mk in marks.items():
        mk2 = dict(mk)
        ids = mk.get("m76_ids") or []
        if ids and ids[0] in DISPLAY_OVERRIDE_M76:
            new = DISPLAY_OVERRIDE_M76[ids[0]]
            if mk2.get("display") != new:
                changed.append({"material_id": mid, "m76_id": ids[0], "stage25": mk2.get("display"), "stage3": new})
                mk2["display"] = new
        out[mid] = mk2
    return out, changed

def figure_labels(rows: list) -> list:
    """図に名前を出す行(順番どおり)の呼び名。同じ図に同じ呼び名が 2 つ以上あるときだけ括弧を付ける
    (付け方は peak_dictionary.dedupe_labels)。"""
    items = [{"display": r["display"], "tier": r["tier"] or "小", "own_name": r.get("own_name"),
              "alt_name": r.get("alt_name"), "ele_m": r["ele_m"]} for r in rows]
    return [x["display"] for x in dedupe_labels(items)]
