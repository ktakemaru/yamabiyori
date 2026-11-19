"""展望図の置き場所(保存先)。本体の cache/ の場所の決め方は変えない。

- 展望図の保存(1003 山の表・OSM の展望地・予報・図の地形): 既定は <リポジトリ>/cache/panorama/。
  環境変数 YAMABIYORI_PANORAMA_CACHE で替えられる(「初めての利用者」の確かめ・テスト用)。
- 標高タイル: 既定は本体と同じ terrain_cache/(mountain_terrain.TERRAIN_CACHE_DIR)。環境変数 YAMABIYORI_TERRAIN_CACHE で替えられる。
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def cache_dir() -> str:
    return os.environ.get("YAMABIYORI_PANORAMA_CACHE") or os.path.join(ROOT, "cache", "panorama")


def terrain_dir() -> str:
    import mountain_terrain as mt
    return os.environ.get("YAMABIYORI_TERRAIN_CACHE") or mt.TERRAIN_CACHE_DIR


def gsi_dir() -> str:
    return os.path.join(cache_dir(), "gsi_1003")


def osm_dir() -> str:
    return os.path.join(cache_dir(), "osm")


def weather_dir() -> str:
    return os.path.join(cache_dir(), "weather")


def parts_dir() -> str:
    return os.path.join(cache_dir(), "parts")


def out_dir() -> str:
    return os.path.join(ROOT, "out", "panorama")
