"""群文件跨群管理、批量发言、批量传群文件（group_files.py）。"""
import asyncio
import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import group_files  # noqa: E402

NOW = int(time.time())


def _f(fid, name, size, uploader, ago=0):
    return {"file_id": fid, "file_name": name, "busid": 102, "file_size": size, "uploader": uploader,
            "uploader_name": f"u{uploader}", "upload_time": NOW - ago}


class _Client:
    def __init__(self):
        self.files = {
            1: {"root": [_f("/a", "ad.apk", 5000, 30001, 100), _f("/b", "ad.apk", 5000, 30001, 50),
                         _f("/c", "rules.pdf", 100, 1, 9999)],
                "folders": {"/资料": [_f("/d", "notes.doc", 10, 20001, 10)]}},
            2: {"root": [_f("/e", "ad.apk", 5000, 30001, 30)], "folders": {}},
        }
        self.calls = []
        self.folder_param = "folder_id"   # 模拟只认某一种参数名的实现

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        g = self.files.get(kw.get("group_id"))
        if action == "get_group_root_files":
            if g is None:
                raise RuntimeError("群不存在")
            return {"status": "ok", "retcode": 0, "data": {
                "files": g["root"], "folders": [{"folder_id": k, "folder_name": k.strip("/")} for k in g["folders"]]}}
        if action == "get_group_files_by_folder":
            if self.folder_param not in kw:
                return {"status": "failed", "retcode": 100, "message": "缺少参数"}
            return {"status": "ok", "retcode": 0, "data": {"files": g["folders"][kw[self.folder_param]], "folders": []}}
        if action == "delete_group_file":
            g["root"] = [f for f in g["root"] if f["file_id"] != kw["file_id"]]
            return {"status": "ok", "retcode": 0, "data": None}
        if action in ("send_group_msg", "upload_group_file"):
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
        return {"status": "failed", "retcode": 1, "message": "unknown"}


class _H(group_files.GroupFilesMixin):
    def __init__(self):
        self.client = _Client()
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        self.ops, self.jobs = [], []

    def _cfg_int(self, key, default=0, group_id=None):
        return 0 if key == "directory_min_interval_ms" else default

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    async def _get_client(self, event=None):
        return self.client

    def _check_api_result(self, result, name="操作"):
        if isinstance(result, dict) and result.get("retcode", 0) != 0:
            return False, result.get("message", "失败")
        return True, ""

    async def _call_group_api_result(self, client, action, name="", **kw):
        try:
            r = await client.call_action(action, **kw)
        except Exception as e:
            return False, None, str(e)
        ok, err = self._check_api_result(r, name)
        return (True, r.get("data"), "") if ok else (False, None, err)

    async def _call_group_api(self, client, action, name="", **kw):
        ok, _d, err = await self._call_group_api_result(client, action, name, **kw)
        return ok, err

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    def _bq_create_job(self, title, kind, items, **kw):
        self.jobs.append((title, kind, items))
        return {"id": len(self.jobs), "title": title, "items": len(items)}


async def _scan(h, groups):
    assert h._gf_start_scan(groups) == ""
    await asyncio.gather(*list(h._gf_tasks))
    return h._gf_snapshot()


class FilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_folders_errors_and_duplicates(self):
        h = _H()
        snap = await _scan(h, ["1", "2", "9"])
        self.assertEqual((snap["done"], snap["total"], snap["file_count"], snap["folders"]), (3, 3, 5, 1))
        self.assertIn("9", snap["errors"])
        notes = next(f for f in snap["files"] if f["name"] == "notes.doc")
        self.assertEqual((notes["folder_name"], notes["uploader"]), ("资料", "20001"))
        # 同一个群里的 ad.apk 两份，保留早的（/a），二群那份不算重复
        self.assertEqual(snap["duplicates"], ["1:/b"])
        self.assertEqual(sorted(group_files.duplicate_ids(snap["files"], cross_group=True)), ["1:/b", "2:/e"])
        self.assertFalse(h._gf_state()["running"])
        h._gf_state()["running"] = True
        self.assertIn("正在扫描", h._gf_start_scan(["1"]))  # 同一时间只扫一次

    async def test_folder_param_fallback(self):
        h = _H()
        h.client.folder_param = "folder"
        snap = await _scan(h, ["1"])
        self.assertEqual(snap["file_count"], 4)
        self.assertEqual(snap["errors"], {})

    async def test_delete_job_and_executor(self):
        h = _H()
        await _scan(h, ["1", "2"])
        job = h._gf_delete_job([{"group_id": "1", "file_id": "/a"}, {"group_id": "2", "file_id": "/e"},
                                {"group_id": "1", "file_id": "/a"}, {"group_id": "1", "file_id": "/nope"}],
                               reason="广告文件")
        self.assertEqual(job["items"], 2)
        title, kind, items = h.jobs[-1]
        self.assertEqual(kind, "delfile")
        self.assertIn("上传者 30001", title)
        it = items[0]
        status, msg = await h._bq_exec_delfile(h.client, it["group_id"], it["target"], it["params"])
        self.assertEqual(status, "ok")
        self.assertEqual(h.ops[-1][0], "删除群文件")
        self.assertEqual(h.ops[-1][1]["reason"], "广告文件")
        self.assertNotIn("1:/a", [group_files.file_key(f) for f in h._gf_state()["files"]])
        self.assertEqual(h._gf_delete_job([{"group_id": "1", "file_id": "/zzz"}]), {})

    async def test_outbox_send_and_upload(self):
        h = _H()
        info, err = h._outbox_save('通知:1.txt', base64.b64encode(b"hello").decode())
        self.assertEqual(err, "")
        self.assertEqual(info["name"], "通知_1.txt")
        self.assertTrue(Path(info["path"]).is_file())
        self.assertIn("服务器上的文件路径", h._outbox_save("big", "A" * (group_files.OUTBOX_MAX_BYTES * 2))[1])
        local, err = h._outbox_local(info["path"])
        self.assertEqual((err, local["size"]), ("", 5))
        self.assertIn("完整路径", h._outbox_local("relative.txt")[1])
        self.assertIn("找不到", h._outbox_local(str(Path(h._data_dir) / "missing.txt"))[1])
        # 批量发言
        job, err = h._bulk_send_job(["1", "2"], "大家好", local)
        self.assertEqual((err, job["items"]), ("", 2))
        params = h.jobs[-1][2][0]["params"]
        status, _ = await h._bq_exec_sendmsg(h.client, "1", "", params)
        self.assertEqual(status, "ok")
        action, kw = h.client.calls[-1]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(kw["message"][0], {"type": "text", "data": {"text": "大家好"}})
        self.assertEqual(kw["message"][1]["data"]["file"], "base64://" + base64.b64encode(b"hello").decode())
        self.assertIn("内容", h._bulk_send_job(["1"], "  ")[1])
        # 批量传文件
        job, err = h._bulk_upload_job(["1"], local)
        self.assertEqual(err, "")
        params = h.jobs[-1][2][0]["params"]
        status, _ = await h._bq_exec_upfile(h.client, "1", "", params)
        self.assertEqual(status, "ok")
        self.assertEqual(h.client.calls[-1][1]["name"], "通知_1.txt")
        os.remove(local["path"])
        self.assertEqual((await h._bq_exec_upfile(h.client, "1", "", params))[0], "failed")

    async def test_send_with_multiple_images(self):
        h = _H()
        files = []
        for n, body in enumerate((b"one", b"two", b"three"), 1):
            info, err = h._outbox_save(f"图{n}.png", base64.b64encode(body).decode())
            self.assertEqual(err, "")
            files.append(info)
        job, err = h._bulk_send_job(["1", "2"], "看图", files)
        self.assertEqual((err, job["items"]), ("", 2))
        title, kind, items = h.jobs[-1]
        self.assertEqual(kind, "sendmsg")
        self.assertIn("[图片×3]", title)
        params = items[0]["params"]
        self.assertEqual(params["images"], [f["path"] for f in files])
        status, _ = await h._bq_exec_sendmsg(h.client, "1", "", params)
        self.assertEqual(status, "ok")
        message = h.client.calls[-1][1]["message"]
        # 一条消息：文字在前，图片按给的顺序
        self.assertEqual([seg["type"] for seg in message], ["text", "image", "image", "image"])
        self.assertEqual([seg["data"]["file"] for seg in message[1:]],
                         ["base64://" + base64.b64encode(b).decode() for b in (b"one", b"two", b"three")])
        self.assertIn("[图片×3]", h.ops[-1][1]["detail"])
        # 只有图片、没有文字
        job, err = h._bulk_send_job(["1"], "", files[:2])
        self.assertEqual(err, "")
        self.assertIn("[图片×2]", h.jobs[-1][0])
        status, _ = await h._bq_exec_sendmsg(h.client, "1", "", h.jobs[-1][2][0]["params"])
        self.assertEqual([seg["type"] for seg in h.client.calls[-1][1]["message"]], ["image", "image"])
        # 其中一张已被清理：整条不发，说明是第几张
        os.remove(files[1]["path"])
        calls = len(h.client.calls)
        status, msg = await h._bq_exec_sendmsg(h.client, "1", "", params)
        self.assertEqual(status, "failed")
        self.assertIn("第 2 张图片", msg)
        self.assertEqual(calls, len(h.client.calls))

    async def test_send_image_limits_and_old_job_params(self):
        h = _H()
        info, _ = h._outbox_save("a.png", base64.b64encode(b"img").decode())
        too_many = [info] * (group_files.SEND_IMAGES_MAX + 1)
        self.assertIn(f"最多带 {group_files.SEND_IMAGES_MAX} 张", h._bulk_send_job(["1"], "x", too_many)[1])
        big = [dict(info, size=group_files.SEND_IMAGES_TOTAL_MAX // 2 + 1)] * 2
        self.assertIn("合计超过", h._bulk_send_job(["1"], "x", big)[1])
        self.assertEqual(h.jobs, [])
        # 一张图仍可以直接传字典（旧调用方式）
        job, err = h._bulk_send_job(["1"], "x", info)
        self.assertEqual(err, "")
        self.assertEqual(h.jobs[-1][2][0]["params"]["images"], [info["path"]])
        # 升级前排队的任务只有 image 字段，照常发送
        status, _ = await h._bq_exec_sendmsg(h.client, "1", "", {"text": "旧任务", "image": info["path"]})
        self.assertEqual(status, "ok")
        self.assertEqual([seg["type"] for seg in h.client.calls[-1][1]["message"]], ["text", "image"])
        self.assertTrue(h.ops[-1][1]["detail"].endswith("[图片]"))

    def test_image_payload(self):
        h = _H()
        info, _ = h._outbox_save("海报.png", base64.b64encode(b"img").decode())
        uri = Path(info["path"]).as_uri()
        self.assertIn("%", uri)  # 中文文件名在 file:// 里被百分号编码
        self.assertEqual(group_files.image_payload(uri), ("base64://aW1n", ""))
        self.assertEqual(group_files.image_payload(info["path"]), ("base64://aW1n", ""))
        self.assertEqual(group_files.image_payload("https://x/a.png"), ("https://x/a.png", ""))
        os.remove(info["path"])
        self.assertIn("已不存在", group_files.image_payload(uri)[1])

    def test_outbox_clean(self):
        h = _H()
        info, _ = h._outbox_save("old.txt", base64.b64encode(b"x").decode())
        old = time.time() - group_files.OUTBOX_KEEP_SECONDS - 10
        os.utime(info["path"], (old, old))
        self.assertEqual(h._outbox_clean(), 1)


if __name__ == "__main__":
    unittest.main()
