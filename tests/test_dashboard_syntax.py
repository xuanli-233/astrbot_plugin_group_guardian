"""管理页面的脚本没有语法错误（有 node 时用 node --check 检查；一处语法错误会让整个页面失效）。"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "pages" / "dashboard" / "index.html", ROOT / "devtools" / "mock_webui" / "sim.html"]


@unittest.skipUnless(shutil.which("node"), "没有 node，跳过脚本语法检查")
class DashboardSyntaxTests(unittest.TestCase):
    def test_inline_scripts_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            for page in PAGES:
                scripts = re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S)
                for i, code in enumerate(scripts):
                    path = Path(tmp) / f"{page.stem}_{i}.js"
                    path.write_text(code, encoding="utf-8")
                    with self.subTest(page=page.name, script=i):
                        result = subprocess.run(["node", "--check", str(path)], capture_output=True,
                                                text=True, encoding="utf-8", errors="replace")
                        self.assertEqual(0, result.returncode, result.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
