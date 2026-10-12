#!/usr/bin/env python3
"""気象庁の天気図(実況+予想)とひまわり画像を取得し、保存先と出典を JSON で返す。

レポートの「気圧配置と実況」「前線の接近」節の材料。既存の関数
(`mountain_weather_detail.fetch_jma_weather_map` / `mountain_weather_core.fetch_himawari_image`)を
1回ずつ呼ぶだけの薄いラッパーで、スコアや予報の処理には一切触れない。

使い方(リポジトリのルートで):
    ./venv/Scripts/python.exe -X utf8 scripts/fetch_synoptic.py --mountain 槍ヶ岳
    ./venv/Scripts/python.exe -X utf8 scripts/fetch_synoptic.py --lat 36.0 --lon 138.0 --tile-span 5   # 探索モード(本州全体)

- 気象庁サイトへのアクセスは「診断1回につき1回」まで(SKILL.md の節度あるアクセス運用)。ループや再実行を避ける。
- 天気図の取得には Playwright(Chromium)が必要。ひまわりは Pillow が必要。どちらか失敗しても、取れた分は返す。
  失敗は `failed` に理由つきで入る。失敗を黙って捨てず、レポートの `images_omitted` に理由を書くこと。
- ネットワークが拒否されたら回避策を取らず、承認をユーザーに求める(AGENTS.md)。
出力(標準出力、--out でも保存): {"images":[{"src","kind","cap","attribution"}], "failed":[{"what","reason"}], "fetched_at"}
`src` は --rel-to(既定 out/)からの相対パス。レポート JSON(out/ に置く)の `images` ブロックにそのまま使える。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JMA_ATTRIBUTION = "出典:気象庁ホームページ (https://www.jma.go.jp/bosai/weather_map/)"


def rel(path, base):
    return Path(os.path.relpath(path, base)).as_posix()


def jst_from_basetime(basetime):
    """ひまわりの basetime(UTC, YYYYMMDDHHMMSS)→ JST 表示。"""
    try:
        t = datetime.strptime(basetime, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return (t + timedelta(hours=9)).strftime("%m/%d %H:%M JST")
    except (TypeError, ValueError):
        return str(basetime)


def main(argv=None):
    ap = argparse.ArgumentParser(description="気象庁の天気図とひまわり画像を取得する")
    ap.add_argument("--mountain", metavar="番号|山名", help="山の座標でひまわり画像を切り出す(診断モード・計画モード)")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--tile-span", type=int, default=3, help="ひまわりのタイル数(奇数、既定3。探索モードは5など)")
    ap.add_argument("--himawari-type", default="ir", choices=("ir", "strengthen"), help="ir=赤外 / strengthen=雲頂強調(雷リスクのとき)")
    ap.add_argument("--no-forecast", action="store_true", help="予想天気図(+24h/+48h)を取らず実況だけにする")
    ap.add_argument("--out-dir", default=str(ROOT / "out" / "images"), help="画像の保存先(既定 out/images。gitignore 済み)")
    ap.add_argument("--rel-to", default=str(ROOT / "out"), help="返す src の基準フォルダ(既定 out/)")
    ap.add_argument("--out", help="結果 JSON の保存先(省略時は標準出力のみ)")
    args = ap.parse_args(argv)

    lat, lon, label = args.lat, args.lon, ""
    try:
        import mountain_weather_detail as det
        import mountain_weather_core as core
    except Exception as e:  # 依存パッケージ未導入など
        print(f"エラー: 本体モジュールを読み込めません: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.mountain is not None:
        mtn, candidates = det.resolve_mountain(args.mountain)
        if mtn is None:
            names = "、".join(f"{i}:{m['name']}" for i, m in candidates) or "(該当なし)"
            print(f"エラー: --mountain {args.mountain!r} を一意に決められません。候補: {names}", file=sys.stderr)
            return 2
        lat, lon, label = mtn["lat"], mtn["lon"], mtn["name"]
    if (lat is None) != (lon is None):
        print("エラー: --lat と --lon は両方指定する", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    images, failed = [], []

    # --- 天気図(Playwright。実況+予想を同じブラウザ操作で取得) ---
    try:
        paths = det.fetch_jma_weather_map(out_dir=args.out_dir, include_forecast=not args.no_forecast)
        paths = [paths] if isinstance(paths, str) else list(paths)
        kinds = [("jma_now", "実況天気図"), ("jma_plus24h", "+24h予想天気図"), ("jma_plus48h", "+48h予想天気図")]
        for p, (kind, cap) in zip(paths, kinds):
            images.append({"src": rel(p, args.rel_to), "kind": kind, "cap": cap, "attribution": JMA_ATTRIBUTION})
    except Exception as e:
        failed.append({"what": "気象庁の天気図(実況・予想)", "reason": f"{type(e).__name__}: {e}"})

    # --- ひまわり画像(座標が分かるときだけ) ---
    if lat is not None:
        try:
            res = core.fetch_himawari_image(lat, lon, imgtype=args.himawari_type, tile_span=args.tile_span,
                                            out_path=str(Path(args.out_dir) / f"himawari_{args.himawari_type}.jpg"))
            kind_label = "雲頂強調" if args.himawari_type == "strengthen" else "赤外"
            cap = f"ひまわり{kind_label}画像" + (f"({label}付近)" if label else "") + f"・{jst_from_basetime(res.get('basetime'))}"
            images.append({"src": rel(res["path"], args.rel_to), "kind": "himawari", "cap": cap,
                           "attribution": res.get("attribution", core.HIMAWARI_ATTRIBUTION)})
        except Exception as e:
            failed.append({"what": "ひまわり画像", "reason": f"{type(e).__name__}: {e}"})
    else:
        failed.append({"what": "ひまわり画像", "reason": "座標が未指定(--mountain または --lat/--lon が必要)"})

    result = {"images": images, "failed": failed, "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
