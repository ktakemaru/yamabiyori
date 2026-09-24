"""ヤマビヨリのセットアップ確認(スコアの計算はしない。計算の確認は unittest の役割)。

使い方(リポジトリのルートで):
    Windows:      ./venv/Scripts/python.exe -X utf8 scripts/check_setup.py [--network]
    macOS/Linux:  ./venv/bin/python -X utf8 scripts/check_setup.py [--network]

確認すること: Python のバージョン / 必須パッケージ / venv で実行しているか / 日本語と記号を出力できるか /
cache/ に書き込めるか。--network を付けたときだけ Open-Meteo への接続も確認する。
終了コード: 0 = 問題なし(注意のみを含む)、1 = 直す必要がある項目がある。
このスクリプト自体は cp932 の端末でも落ちないよう、記号を使わず [OK]/[NG]/[注意] で表示する。
"""
import argparse
import importlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_PYTHON = (3, 10)
REQUIRED = ["requests", "numpy"]
OPTIONAL = {"playwright": ("playwright", "気象庁天気図の取得"), "PIL": ("Pillow", "ルート天気マップPNG・ひまわり画像")}
SAMPLE = "⚠雷注意 ℃"   # 本体の表に出る文字(cp932 では ⚠ を出力できない)
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")   # このスクリプト自身の出力で落ちないように(本体には影響しない)

problems = 0


def report(ok, label, detail="", fix=""):
    global problems
    mark = "[OK]" if ok is True else ("[注意]" if ok is None else "[NG]")
    if ok is False:
        problems += 1
    print(f"{mark} {label}" + (f": {detail}" if detail else ""))
    if fix and ok is not True:
        print(f"      -> {fix}")


def venv_python():
    return "./venv/Scripts/python.exe" if os.name == "nt" else "./venv/bin/python"


def check_python():
    v = sys.version_info
    report(v >= MIN_PYTHON, "Python のバージョン", f"{v.major}.{v.minor}.{v.micro}",
           f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上を入れ、venv を作り直してください(README「Codexで試す」参照)")


def check_venv():
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    report(True if in_venv else None, "venv の Python で実行しているか", sys.executable,
           f"python -m venv venv で venv を作り、以後は {venv_python()} で実行してください")


def check_packages():
    for name in REQUIRED:
        try:
            mod = importlib.import_module(name)
            report(True, f"必須パッケージ {name}", getattr(mod, "__version__", ""))
        except ImportError:
            report(False, f"必須パッケージ {name}", "見つかりません",
                   f"{venv_python()} -m pip install -r requirements.txt を実行してください")
    for module, (package, use) in OPTIONAL.items():
        try:
            importlib.import_module(module)
            report(True, f"任意パッケージ {package}", use)
        except ImportError:
            print(f"[--] 任意パッケージ {package}: 未インストール({use}に使う。通常の実行には不要)")


def check_utf8():
    enc = getattr(sys.stdout, "encoding", None) or "不明"
    try:
        SAMPLE.encode(enc)
        ok = True
    except (LookupError, UnicodeEncodeError):
        ok = False
    report(ok, "日本語・記号(⚠ など)を出力できるか", f"標準出力の文字コード={enc}, UTF-8モード={sys.flags.utf8_mode}",
           "コマンドに -X utf8 を付けて実行してください(例: " + venv_python() + " -X utf8 mountain_weather_mvp.py --limit 3)。"
           "付けないと Windows の Python 3.14 以前では UnicodeEncodeError で止まります")


def check_cache():
    cache = os.path.join(ROOT, "cache")
    try:
        os.makedirs(cache, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".check_", delete=True) as f:
            f.write(b"ok")
        report(True, "cache/ に書き込めるか", cache)
    except OSError as e:
        report(False, "cache/ に書き込めるか", str(e),
               "フォルダの書き込み権限を確認してください(OneDrive 同期中のフォルダや読み取り専用の場所を避ける)")


def check_network():
    try:
        import requests
    except ImportError:
        report(False, "Open-Meteo への接続", "requests が無いため確認できません")
        return
    try:
        r = requests.get(OPEN_METEO_URL, params={"latitude": 35.36, "longitude": 138.73,
                                                  "hourly": "temperature_2m", "forecast_days": 1}, timeout=20)
        ok = r.status_code == 200 and "hourly" in r.json()
        report(ok, "Open-Meteo への接続", f"HTTP {r.status_code}",
               "Open-Meteo 側の一時的な障害の可能性があります。時間をおいて再実行してください(コードは直さない)")
    except Exception as e:   # noqa: BLE001 -- どんな失敗でも案内を出して続ける
        report(False, "Open-Meteo への接続", f"{type(e).__name__}: {e}",
               "ネットワークに接続できません。エージェント(Codex等)の実行中なら、ネットワーク利用の承認が必要な場合があります")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ヤマビヨリのセットアップ確認")
    parser.add_argument("--network", action="store_true", help="Open-Meteo への接続も確認する")
    args = parser.parse_args(argv)

    print("=== ヤマビヨリ セットアップ確認 ===")
    check_python()
    check_venv()
    check_packages()
    check_utf8()
    check_cache()
    if args.network:
        check_network()
    else:
        print("[--] Open-Meteo への接続: 未確認(確認するときは --network を付ける)")

    if problems:
        print(f"\n結果: 直す必要がある項目が {problems} 件あります(上の -> の案内を参照)。")
        return 1
    print("\n結果: セットアップは問題ありません。次は "
          f"{venv_python()} -X utf8 mountain_weather_mvp.py --limit 3 で動作確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
