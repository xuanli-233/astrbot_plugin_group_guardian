"""违规记录图片落盘（media_store.py）：保存、改写地址、详情描述、读取、清理。"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import media_store  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402


def _png(color="red"):
    try:
        from PIL import Image
    except Exception:
        return b"\x89PNG fake " + color.encode()
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="PNG")
    return buf.getvalue()


class _Storage(SqliteConnectMixin):
    pass


class _H(media_store.MediaStoreMixin):
    def __init__(self, cfg=None):
        self.cfg = {"log_image_save_enabled": True, "log_image_days": 30, **(cfg or {})}
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        self._storage = _Storage()
        with self._storage._connect() as conn:
            conn.execute("CREATE TABLE moderation_logs (id INTEGER PRIMARY KEY, image_urls TEXT)")
            conn.commit()
        self.files = {}

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    async def _download_bytes(self, url, max_bytes=0, timeout=0):
        return self.files.get(url)

    def add_log(self, log_id, urls):
        with self._storage._connect() as conn:
            conn.execute("INSERT INTO moderation_logs(id, image_urls) VALUES(?, ?)", (log_id, json.dumps(urls)))
            conn.commit()
        return {"id": log_id, "image_urls": list(urls)}

    def stored_urls(self, log_id):
        with self._storage._connect() as conn:
            return json.loads(conn.execute("SELECT image_urls FROM moderation_logs WHERE id=?", (log_id,))
                              .fetchone()["image_urls"])


class MediaStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_paths_and_links_are_saved_and_rewritten(self):
        h = _H()
        local = r"D:\bot\astrbot\data\temp\media_1.jpg"
        h.files = {local: _png("red"), "https://q/a.jpg": _png("blue")}
        entry = h.add_log(1, [local, "https://q/a.jpg", r"D:\bot\astrbot\data\temp\gone.jpg", "https://q/dead.jpg"])
        out = await h._ml_save_log_images(entry)
        self.assertTrue(out[0].startswith("saved:") and out[1].startswith("saved:"))
        self.assertEqual(out[2], "failed:本地图片")  # 本机路径读不到：不再保留路径
        self.assertEqual(out[3], "https://q/dead.jpg")  # 链接读不到：保留链接
        self.assertEqual(entry["image_urls"], out)  # 内存里的那份也改了
        self.assertEqual(h.stored_urls(1), out)
        self.assertNotIn("temp", json.dumps(out))
        info = h._ml_describe_images(out)
        self.assertEqual(len(info["images"]), 2)
        self.assertTrue(all(i["thumb"] and not i["expired"] for i in info["images"]))
        self.assertEqual((info["links"], info["failed"]), (["https://q/dead.jpg"], 1))
        self.assertTrue(h._ms_read(info["images"][0]["hash"]).startswith("data:image/"))

    async def test_same_image_stored_once(self):
        h = _H()
        h.files = {"a": _png(), "b": _png()}
        out = await h._ml_save_log_images(h.add_log(1, ["a", "b"]))
        self.assertEqual(out[0], out[1])
        with h._storage._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM log_images").fetchone()["c"], 1)

    async def test_schedule_respects_switch_and_skips_processed(self):
        h = _H({"log_image_save_enabled": False})
        h.files = {"a": _png()}
        entry = h.add_log(1, ["a"])
        h._ml_schedule_images(entry)
        self.assertFalse(getattr(h, "_ms_tasks", None))
        h.cfg["log_image_save_enabled"] = True
        h._ml_schedule_images(entry)
        await asyncio.gather(*list(h._ms_tasks))
        self.assertTrue(entry["image_urls"][0].startswith("saved:"))
        h._ml_schedule_images(entry)  # 已处理过的不再处理
        self.assertFalse(h._ms_tasks)

    async def test_clean_and_bad_hash(self):
        h = _H({"log_image_days": 1})
        h.files = {"a": _png()}
        out = await h._ml_save_log_images(h.add_log(1, ["a"]))
        hsh = out[0][len("saved:"):]
        self.assertEqual(h._ms_clean(int(time.time()) + 2 * 86400), 1)
        self.assertEqual(h._ms_read(hsh), "")
        self.assertTrue(h._ml_describe_images(out)["images"][0]["expired"])
        self.assertEqual(h._ms_read("../../etc/passwd"), "")


if __name__ == "__main__":
    unittest.main()
