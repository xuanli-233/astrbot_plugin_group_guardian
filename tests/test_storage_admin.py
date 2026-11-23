"""存储管理（storage_admin.py）：占用统计、删除旧记录（连图片）、孤儿文件、重新压缩、整理数据库。"""
import asyncio
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import storage_admin  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

DAY = 86400


class _Storage(SqliteConnectMixin):
    @property
    def db_path(self):
        with self._connect():
            pass
        return self._sqlite_path


class _H(storage_admin.StorageAdminMixin):
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._storage = _Storage()
        self.ops = []

    def _record_op(self, action, **kw):
        self.ops.append((action, kw.get("detail", "")))


def _png(size=600):
    try:
        from PIL import Image
    except Exception:
        return None
    import random
    img = Image.new("RGB", (size, size))
    rnd = random.Random(1)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size * size)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class StorageAdminTests(unittest.TestCase):
    def setUp(self):
        self.h = _H()
        now = int(time.time())
        self.now = now
        img_dir = self.h._data_dir / "log_images"
        img_dir.mkdir()
        self.old_img = img_dir / "old.jpg"
        self.new_img = img_dir / "new.jpg"
        self.orphan = img_dir / "orphan.jpg"
        for p in (self.old_img, self.new_img, self.orphan):
            p.write_bytes(b"x" * 2048)
        with self.h._storage._connect() as conn:
            conn.execute("CREATE TABLE moderation_logs (id INTEGER PRIMARY KEY, ts INTEGER, msg_text TEXT)")
            conn.execute("CREATE TABLE log_images (hash TEXT PRIMARY KEY, path TEXT, bytes INTEGER, thumb TEXT, created_at INTEGER)")
            conn.execute("CREATE TABLE lk_sends (id INTEGER PRIMARY KEY, ts INTEGER, url_hash TEXT)")
            conn.execute("CREATE TABLE lk_urls (url_hash TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE batch_jobs (id INTEGER PRIMARY KEY, state TEXT, finished_at INTEGER)")
            conn.execute("CREATE TABLE batch_items (id INTEGER PRIMARY KEY, job_id INTEGER)")
            conn.executemany("INSERT INTO moderation_logs(ts, msg_text) VALUES(?, ?)",
                             [(now - 100 * DAY, "旧" * 500), (now - DAY, "新")])
            conn.executemany("INSERT INTO log_images VALUES(?,?,?,?,?)",
                             [("a", str(self.old_img), 2048, "", now - 100 * DAY), ("b", str(self.new_img), 2048, "", now - DAY)])
            conn.executemany("INSERT INTO lk_sends(ts, url_hash) VALUES(?,?)", [(now - 100 * DAY, "u1"), (now, "u2")])
            conn.executemany("INSERT INTO lk_urls VALUES(?)", [("u1",), ("u2",)])
            conn.executemany("INSERT INTO batch_jobs VALUES(?,?,?)", [(1, "done", now - 100 * DAY), (2, "active", None)])
            conn.executemany("INSERT INTO batch_items(job_id) VALUES(?)", [(1,), (1,), (2,)])
            conn.commit()

    def tearDown(self):
        self.h._tmp.cleanup()

    def test_report(self):
        r = self.h._sa_report()
        cats = {c["key"]: c for c in r["categories"]}
        self.assertEqual((cats["violations"]["rows"], cats["violations"]["files"]), (2, 3))
        self.assertEqual(cats["violations"]["oldest"], self.now - 100 * DAY)
        self.assertEqual(cats["links"]["rows_all"], 4)
        self.assertFalse(cats["other"]["can_purge"])
        self.assertIn("meta", [t["name"] for t in cats["other"]["tables"]])
        self.assertGreater(r["db"]["total"], 0)
        if not r["dbstat"]:   # 估算：各表加起来等于数据库已用大小，大表占得多
            self.assertTrue(r["estimated"])
            self.assertAlmostEqual(sum(c["db_bytes"] for c in r["categories"]), r["db"]["used_bytes"], delta=50)
            self.assertGreater(cats["violations"]["db_bytes"], cats["oplog"]["db_bytes"])
        dirs = {d["name"]: d for d in r["dirs"]}
        self.assertEqual(dirs["log_images"]["orphans"], {"files": 1, "bytes": 2048})
        self.assertIsNotNone(r["disk"])

    def test_purge_with_images(self):
        res, err = asyncio.run(self.h._sa_run("purge", "violations", days=30, operator="9"))
        self.assertEqual(err, "")
        self.assertEqual((res["rows"], res["files"]), (1, 1))
        self.assertFalse(self.old_img.exists())
        self.assertTrue(self.new_img.exists())
        self.assertEqual(self.h.ops[-1][0], "清理存储")
        res, _ = asyncio.run(self.h._sa_run("purge", "links", days=30))
        with self.h._storage._connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT url_hash FROM lk_urls")], ["u2"])   # 没人引用的链接一起删
        res, _ = asyncio.run(self.h._sa_run("purge", "batch", days=0))
        with self.h._storage._connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT id FROM batch_jobs")], [2])          # 进行中的任务不删
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0], 1)

    def test_guards(self):
        self.assertIn("不能删除", asyncio.run(self.h._sa_run("purge", "directory", days=1))[1])
        self.assertIn("不能删除", asyncio.run(self.h._sa_run("purge", "other", days=1))[1])
        self.assertIn("天数", asyncio.run(self.h._sa_run("purge", "links", days=-1))[1])
        self.assertIn("未知", asyncio.run(self.h._sa_run("rm", ""))[1])
        self.h._sa_busy = True
        self.assertIn("稍等", asyncio.run(self.h._sa_run("vacuum"))[1])

    def test_orphans_and_vacuum(self):
        res, _ = asyncio.run(self.h._sa_run("orphans"))
        self.assertEqual(res["files"], 1)
        self.assertFalse(self.orphan.exists())
        self.assertTrue(self.old_img.exists() and self.new_img.exists())
        asyncio.run(self.h._sa_run("purge", "violations", days=0))
        res, err = asyncio.run(self.h._sa_run("vacuum"))
        self.assertEqual(err, "")
        self.assertLessEqual(res["after"], res["before"])

    def test_recompress(self):
        data = _png()
        if data is None:
            self.skipTest("没有 Pillow")
        p = self.h._data_dir / "log_images" / "big.png"
        p.write_bytes(data)
        with self.h._storage._connect() as conn:
            conn.execute("INSERT INTO log_images VALUES(?,?,?,?,?)", ("big", str(p), len(data), "", self.now))
            conn.commit()
        res, err = asyncio.run(self.h._sa_run("recompress", max_kb=100))
        self.assertEqual(err, "")
        self.assertEqual(res["files"], 1)
        with self.h._storage._connect() as conn:
            row = conn.execute("SELECT path, bytes FROM log_images WHERE hash='big'").fetchone()
        self.assertTrue(row["path"].endswith(".jpg"))
        self.assertLessEqual(row["bytes"], 100 * 1024)
        self.assertEqual(os.path.getsize(row["path"]), row["bytes"])
        self.assertFalse(p.exists())
        self.assertIn("压缩", asyncio.run(self.h._sa_run("recompress", max_kb=5))[1])


if __name__ == "__main__":
    unittest.main()
