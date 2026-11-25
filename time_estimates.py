"""時間の目安(実測だけ)と、作業の始まりに出す 1 行の小さなヘルパー(探索・診断・展望図の見積もりで共通)。

値は AGENTS.md「長い作業の前に伝える」の表と同じ出どころの実測。測っていない作業は None(時間を書かない)。
予報の計算・スコア・標準出力には関わらない(始まりの 1 行は標準エラーに出す)。
"""
import math
import sys

# 探索モード 全 76 座(予報の保存が無いとき): 2026-11-24、この PC、1 回 246.0 秒(利用者の実行。出力の「実行時間の内訳」)。
# 2026-11-18 の空のキャッシュの 2 回も 250・246 秒(docs/mountain-coordinates-fix-report.md 4 章)
EXPLORE_ALL_SEC = 246.0
# 診断モード 1 座(予報の保存が無いとき): 2026-11-24、この PC、1 回 4.6 秒(富士山・剣ヶ峰。展望図なし。--out・--json-out つき)。
# 保存があるときは 1.6 秒(高尾山)・1.1 秒(筑波山)。表示は about() で 10 秒単位(約 10 秒)に丸める
DIAGNOSIS_SEC = 4.6

# 展望図: 標高タイル 1 枚あたりの秒(566 枚で 12 分 14 秒。2026-11-20、大菩薩嶺、空のフォルダから。README・docs/panorama-v1.10-finish-report.md)
PANORAMA_TILE_SEC = (12 * 60 + 14) / 566
# 展望図: タイルのあとの予報と図づくり(地形の計算の保存が無いとき)。2026-11-24、この PC、高尾山、1 回 190 秒(予報 115 回・地形の計算 99.8 秒。
# 山頂と展望地の図 2 枚。PC のメモリの空きが少ないときの値)。2026-11-20 の大菩薩嶺(2 枚)は 109 秒(地形の計算 36 秒)だった。
# 短く言いすぎないよう、新しいほう(長いほう)を使う
PANORAMA_FIRST_SEC = 190.0
# 展望図: タイルと地形の計算は保存済みで、予報の保存が無いとき。2026-11-24、この PC、高尾山、1 回 49.3 秒(予報 135 回。
# 山頂と展望地の図 2 枚とも地形の計算は保存済み)
PANORAMA_FORECAST_SEC = 50.0
# 展望図: タイル・地形の計算・予報がすべて保存済みのとき(約 20 秒。20.7 秒・22.4 秒、大菩薩嶺。README・docs/panorama-v1.10-report.md)
PANORAMA_SAVED_SEC = 20.0


def about(sec) -> str:
    """秒 → 「約 20 秒」「約 5 分」(1 分未満は 10 秒単位、それ以上は分に四捨五入)。"""
    if sec < 55:
        return f"約 {max(10, int(math.floor(sec / 10 + 0.5)) * 10)} 秒"
    return f"約 {max(1, int(math.floor(sec / 60 + 0.5)))} 分"


def start_line(what: str, sec=None, note: str = "") -> str:
    """例「76 座の予報を取ります(時間の目安: 約 5 分)」。sec が None(未実測)なら作業の名前だけ。"""
    if sec is None:
        return what
    return f"{what}(時間の目安: {about(sec)}{note})"


def announce(text: str) -> None:
    """標準エラーに 1 行出す(標準出力は変えない。標準エラーが無い環境では何もしない)。"""
    if sys.stderr is not None:
        print(text, file=sys.stderr, flush=True)
