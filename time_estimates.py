"""時間の目安(実測だけ)と、作業の始まりに出す 1 行の小さなヘルパー(探索・診断・展望図の見積もりで共通)。

値は AGENTS.md「長い作業の前に伝える」の表と同じ出どころの実測。測っていない作業は None(時間を書かない)。
予報の計算・スコア・標準出力には関わらない(始まりの 1 行は標準エラーに出す)。
"""
import math
import sys

# 探索モード 全 76 座(予報の保存が無いとき): 2026-11-18、開発用の PC、空のキャッシュで 250・246 秒
# (docs/mountain-coordinates-fix-report.md 4 章)
EXPLORE_ALL_SEC = 250.0
# 診断モード 1 座: 未実測
DIAGNOSIS_SEC = None

# 展望図: 標高タイル 1 枚あたりの秒(566 枚で 12 分 14 秒。2026-11-20、大菩薩嶺、空のフォルダから。README・docs/panorama-v1.10-finish-report.md)
PANORAMA_TILE_SEC = (12 * 60 + 14) / 566
# 展望図: タイルのあとの予報と図づくり(予報 155 回・地形の計算 36 秒を含む。2026-11-20、大菩薩嶺。同上)
PANORAMA_FIRST_SEC = 109.0
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
