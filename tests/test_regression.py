"""参照3日の回帰チェック(オフライン)。

scratch_validate_refs.py をコミット済みのフィクスチャ (tests/fixtures/refs/) で実行し、出力が
現行の値と完全に一致することを確かめる。値は CHANGELOG v1.6.0 の「参照日」の値と同じ。
スコア式・閾値・較正を変えるとここが落ちる -- 変更が意図どおりなら CHANGELOG に新しい値と理由を
書いてから EXPECTED を更新すること。
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "refs")

EXPECTED = [
    "唐松岳 2026-09-06: score=55.0 cloud=63.4(raw 30.9) wet=50.0(7/14h M 湿7) vis=None day_total_mm=0.6",
    "立山(雄山) 2026-09-05: score=95.6 cloud=16.4(raw 6.1) wet=0.0(0/14h M) vis=None day_total_mm=0.0",
    "槍ヶ岳 2026-09-05: score=67.8 cloud=75.5(raw 39.8) wet=7.1(1/14h M 湿1) vis=None day_total_mm=0.0",
]


class TestReferenceDays(unittest.TestCase):
    def test_validate_refs_output(self):
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(ROOT, "scratch_validate_refs.py"), "--cache-dir", FIXTURES],
            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), EXPECTED)


class TestWithoutUtf8Mode(unittest.TestCase):
    """-X utf8 を付け忘れても落ちないこと(各スクリプトの __main__ で stdout/stderr を UTF-8 にしている)。
    -X utf8=0 と環境変数の除去で UTF-8 モードを明示的に切るので、Windows ではパイプが cp932/cp1252 になる
    (CI の Windows ランナーで reconfigure が効いていることの確認)。"""

    def run_script(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        proc = subprocess.run([sys.executable, "-X", "utf8=0", *args], cwd=ROOT, capture_output=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8")

    def test_validate_refs(self):
        out = self.run_script(os.path.join(ROOT, "scratch_validate_refs.py"), "--cache-dir", FIXTURES)
        self.assertEqual(out.splitlines(), EXPECTED)

    def test_detail_list(self):
        out = self.run_script(os.path.join(ROOT, "mountain_weather_detail.py"), "--list")
        self.assertIn("唐松岳", out)


if __name__ == "__main__":
    unittest.main()
