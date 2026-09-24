"""--out 用の小さな入出力ヘルパー(探索モード・診断モード共通。スコアリングとは無関係)。

Windows PowerShell 5.1 で `>` を使うと出力が UTF-16 になり日本語も化けるため、スクリプト側で
UTF-8(BOM 付き: PS 5.1 の Get-Content でも化けない)のファイルへ直接書き出せるようにする。
"""
import contextlib
import os


def run_with_output_file(path: str, func) -> int:
    """func() の標準出力を path へ書き、画面には保存先・行数・成否の1行だけを出す。func の終了コードを返す。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)   # out/ などが無ければ作る
        f = open(path, "w", encoding="utf-8-sig", newline="")
    except OSError as e:
        print(f"[NG] 出力ファイルに書き込めませんでした: {path} ({e})")
        return 2
    with f, contextlib.redirect_stdout(f):
        code = func()
    with open(path, encoding="utf-8-sig") as f:
        lines = sum(1 for _ in f)
    where = os.path.abspath(path)
    if code == 0:
        print(f"[OK] 出力を保存しました: {where}({lines}行、UTF-8)")
    else:
        print(f"[NG] 終了コード{code}。出力を保存しました: {where}({lines}行、UTF-8)。ファイルの末尾に理由があります")
    return code
