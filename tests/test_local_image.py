"""AstrBot 已存到本地的图片（data/temp/...）直接读取；数据目录之外的路径拒绝。"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
try:
    import image_audit  # noqa: E402
except Exception as e:  # pragma: no cover - 依赖缺失
    image_audit = None
    IMPORT_ERROR = e


class _H(image_audit.ImageAuditMixin if image_audit else object):
    def __init__(self, data_root: Path):
        # 与 StarTools.get_data_dir() 相同的结构：data/plugin_data/<插件名>
        self._data_dir = data_root / "plugin_data" / "astrbot_plugin_group_guardian"
        self._data_dir.mkdir(parents=True, exist_ok=True)


@unittest.skipIf(image_audit is None, "image_audit 无法导入")
class LocalImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "astrbot" / "data"
        (self.data / "temp").mkdir(parents=True)
        self.image = self.data / "temp" / "media_123.jpg"
        self.image.write_bytes(b"\x89PNG-fake")
        self.outside = base / "secret.txt"
        self.outside.write_bytes(b"secret")
        self.h = _H(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_inside_data_dir(self):
        self.assertEqual(self.h._resolve_local_image(str(self.image)), self.image.resolve())
        uri = "file://" + str(self.image)
        self.assertEqual(self.h._resolve_local_image(uri), self.image.resolve())

    def test_not_a_local_path(self):
        self.assertIsNone(self.h._resolve_local_image("https://example.com/a.png"))
        self.assertIsNone(self.h._resolve_local_image("1A2B.jpg"))

    def test_outside_data_dir_rejected(self):
        self.assertIs(self.h._resolve_local_image(str(self.outside)), False)
        self.assertIn("不在 AstrBot 数据目录内", image_audit.last_download_error())
        # ../ 穿越也算外部
        sneaky = str(self.data / "temp" / ".." / ".." / "secret.txt")
        self.assertIs(self.h._resolve_local_image(sneaky), False)

    def test_read_local_image(self):
        data, err = image_audit.ImageAuditMixin._read_local_image(self.image, 1024)
        self.assertEqual((data, err), (b"\x89PNG-fake", ""))
        data, err = image_audit.ImageAuditMixin._read_local_image(self.data / "temp" / "gone.jpg", 1024)
        self.assertIsNone(data)
        self.assertIn("不存在", err)
        data, err = image_audit.ImageAuditMixin._read_local_image(self.image, 3)
        self.assertIsNone(data)
        self.assertIn("超过", err)

    async def test_download_bytes_reads_local_file(self):
        loop = asyncio.get_running_loop()

        async def inline(_executor, fn, *args):
            return fn(*args)
        original = loop.run_in_executor
        loop.run_in_executor = inline  # Pyodide 没有线程池，直接在当前线程执行
        try:
            data = await self.h._download_bytes(str(self.image))
            self.assertEqual(data, b"\x89PNG-fake")
            missing = await self.h._download_bytes(str(self.data / "temp" / "gone.jpg"))
            self.assertIsNone(missing)
            self.assertIn("不存在", image_audit.last_download_error())
            blocked = await self.h._download_bytes(str(self.outside))
            self.assertIsNone(blocked)
            self.assertIn("不在 AstrBot 数据目录内", image_audit.last_download_error())
        finally:
            loop.run_in_executor = original


if __name__ == "__main__":
    unittest.main()
