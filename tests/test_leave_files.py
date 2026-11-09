"""退群删文件（leave_files.py）；撤回前备份按 ID 引用失败时用消息内容补（message_backup.py）。"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import group_files  # noqa: E402
import leave_files  # noqa: E402
import leave_recall  # noqa: E402
import message_backup  # noqa: E402

GID, UID, OTHER, BOT = "100", "555", "666", "999"


def _decrease(sub_type="leave", uid=UID, operator=""):
    return {"post_type": "notice", "notice_type": "group_decrease", "sub_type": sub_type,
            "group_id": GID, "user_id": uid, "self_id": BOT, "operator_id": operator or uid}


class _FilesH(leave_files.LeaveFilesMixin):
    LEAVE_FILES_INTERVAL = 0

    def __init__(self, cfg=None, files=None, scan_error="", fail_ids=()):
        self.cfg = {"leave_files_enabled": True}
        self.cfg.update(cfg or {})
        self.files = files if files is not None else []
        self.scan_error = scan_error
        self.fail_ids = set(fail_ids)
        self.scans, self.deleted, self.ops = 0, [], []
        self.forgotten = []

    def _get_raw_event(self, event):
        return event

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _is_backup_group(self, gid):
        return False

    def _blacklist_feature_ready(self, gid):
        return True

    async def _get_client(self, event=None):
        return object()

    async def _gf_scan_group(self, client, gid):
        self.scans += 1
        if self.scan_error:
            return [], self.scan_error, 0
        return [group_files.file_item(gid, f) for f in self.files], "", 0

    def _gf_forget(self, gid, file_id):
        self.forgotten.append(file_id)

    async def _call_group_api(self, client, action, name="", **kw):
        assert action == "delete_group_file"
        if kw["file_id"] in self.fail_ids:
            return False, "没有权限"
        self.deleted.append((kw["file_id"], kw["busid"]))
        return True, ""

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def drain(self):
        for task in list(getattr(self, "_lf_tasks", set()) or []):
            await task


FILES = [
    {"file_id": "/a", "file_name": "广告.apk", "busid": 102, "uploader": int(UID)},
    {"file_id": "/b", "file_name": "资料.pdf", "busid": 102, "uploader": int(OTHER)},
    {"file_id": "/c", "file_name": "广告2.zip", "busid": 104, "uploader": UID},
]


class LeaveFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_only_that_users_files(self):
        h = _FilesH(files=FILES)
        self.assertTrue(await h._handle_leave_files(_decrease("kick", operator=BOT)))
        await h.drain()
        self.assertEqual([("/a", 102), ("/c", 104)], h.deleted)
        self.assertEqual(["/a", "/c"], h.forgotten)
        action, op = h.ops[0]
        self.assertEqual("退群删文件", action)
        self.assertEqual("被踢出群", op["reason"])
        self.assertEqual("机器人", op["operator"])
        self.assertEqual("成功", op["result"])
        self.assertIn("2/2", op["detail"])
        self.assertIn("广告.apk", op["detail"])

    async def test_group_listing_is_cached_between_users(self):
        h = _FilesH(files=FILES)
        await h._handle_leave_files(_decrease("leave"))
        await h._handle_leave_files(_decrease("leave", uid=OTHER))
        await h.drain()
        self.assertEqual(1, h.scans)
        self.assertEqual(["/a", "/c", "/b"], [f for f, _b in h.deleted])
        # 删掉的文件从缓存里去掉，同一个人再触发不会重复删
        await h._handle_leave_files(_decrease("leave"))
        await h.drain()
        self.assertEqual(3, len(h.deleted))
        # 缓存过期后重新扫描
        h._lf_cache[GID] = (time.time() - h.LEAVE_FILES_CACHE_SECONDS - 1, [])
        await h._handle_leave_files(_decrease("leave"))
        await h.drain()
        self.assertEqual(2, h.scans)

    async def test_no_files_no_log(self):
        h = _FilesH(files=FILES[1:2])
        await h._handle_leave_files(_decrease("leave"))
        await h.drain()
        self.assertEqual([], h.deleted)
        self.assertEqual([], h.ops)

    async def test_failures_and_scan_error_recorded(self):
        h = _FilesH(files=FILES, fail_ids={"/c"})
        await h._handle_leave_files(_decrease("leave"))
        await h.drain()
        op = h.ops[0][1]
        self.assertEqual("部分成功", op["result"])
        self.assertIn("没有权限", op["detail"])
        self.assertEqual("", op["operator"])  # 主动退群：操作人就是自己，不重复写
        h = _FilesH(scan_error="获取群文件失败")
        await h._handle_leave_files(_decrease("leave"))
        await h.drain()
        self.assertEqual("失败", h.ops[0][1]["result"])
        self.assertNotIn(GID, getattr(h, "_lf_cache", {}))  # 失败不缓存

    async def test_switches(self):
        self.assertFalse(await _FilesH({"leave_files_enabled": False})._handle_leave_files(_decrease("leave")))
        h = _FilesH({"leave_files_on_leave": False})
        self.assertFalse(await h._handle_leave_files(_decrease("leave")))
        self.assertTrue(await h._handle_leave_files(_decrease("kick")))
        await h.drain()
        self.assertFalse(await _FilesH({"leave_files_on_kick": False})._handle_leave_files(_decrease("kick")))
        h = _FilesH()
        self.assertFalse(await h._handle_leave_files(_decrease("kick_me", uid=BOT)))
        self.assertFalse(await h._handle_leave_files({"post_type": "message"}))


# ----------------------------------------------------------------------
# 撤回前备份：按 ID 引用失败 → get_msg 取内容
# ----------------------------------------------------------------------
class _BackupH(message_backup.MessageBackupMixin):
    def __init__(self, targets, id_fail_groups=(), get_msg=None):
        self.targets = targets
        self.id_fail_groups = set(id_fail_groups)
        self.get_msg = get_msg or {}
        self.sent, self.get_calls = [], []

    def _cfg(self, key, default=True, group_id=None):
        return default

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _br_targets(self, kind):
        return self.targets

    async def _get_bot_uin(self, client):
        return 999

    async def _call_group_api(self, client, action, label, **kw):
        nodes = kw["messages"]
        by_id = any("id" in n["data"] for n in nodes)
        self.sent.append((kw["group_id"], "id" if by_id else "content", nodes))
        if by_id and kw["group_id"] in self.id_fail_groups:
            return False, "消息不存在"
        return True, ""

    async def _call_group_api_result(self, client, action, label, **kw):
        assert action == "get_msg"
        self.get_calls.append(kw["message_id"])
        data = self.get_msg.get(kw["message_id"])
        return (True, data, "") if data else (False, {}, "消息不存在")


def run(coro):
    return asyncio.run(coro)


class BackupFillTests(unittest.TestCase):
    MSG = {"message_id": -1046347266, "sender": {"user_id": 555, "card": "张三"},
           "message": [{"type": "reply", "data": {"id": "1"}}, {"type": "text", "data": {"text": "加我"}}]}

    def test_second_group_uses_fetched_content(self):
        h = _BackupH(["500", "600", "700"], id_fail_groups={600}, get_msg={-1046347266: self.MSG})
        ok = run(h._backup_msg_ids(object(), "100", ["-1046347266"], "退群撤回", kind="leave_recall"))
        self.assertTrue(ok)
        self.assertEqual([(500, "id"), (600, "id"), (600, "content"), (700, "content")],
                         [(g, k) for g, k, _n in h.sent])
        self.assertEqual([-1046347266], h.get_calls)  # 只取一次，后面的群共用
        node = h.sent[2][2][1]["data"]
        self.assertEqual("张三", node["name"])
        self.assertEqual("555", node["uin"])
        self.assertEqual([{"type": "text", "data": {"text": "加我"}}], node["content"])  # 去掉引用回复

    def test_known_segments_skip_get_msg(self):
        h = _BackupH(["500"], id_fail_groups={500})
        items = [{"message_id": "7", "user_id": "5", "segments": [{"type": "text", "data": {"text": "hi"}}]}]
        self.assertTrue(run(h._backup_msg_ids(object(), "100", items, "r", kind="leave_recall")))
        self.assertEqual([], h.get_calls)

    def test_unknown_content_text(self):
        h = _BackupH(["500"], id_fail_groups={500})
        run(h._backup_msg_ids(object(), "100", ["42"], "r"))
        text = h.sent[-1][2][1]["data"]["content"][0]["data"]["text"]
        self.assertIn("没取到这条消息的内容", text)
        self.assertIn("42", text)


class LeaveRecallBackupContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_content_passed_to_backup(self):
        class H(leave_recall.LeaveRecallMixin):
            LEAVE_RECALL_INTERVAL = 0
            _history_backup_item = staticmethod(message_backup.MessageBackupMixin._history_backup_item)

            def __init__(self):
                self.items = None

            def _cfg(self, key, default=True, group_id=None):
                return default

            def _cfg_int(self, key, default=0, group_id=None):
                return default

            async def _call_group_api_result(self, client, action, name="", **kw):
                return True, {"messages": [{"message_id": 11, "time": time.time() - 10,
                                            "sender": {"user_id": int(UID), "nickname": "小王"},
                                            "message": [{"type": "text", "data": {"text": "广告"}}]}]}, ""

            async def _call_group_api(self, client, action, name="", **kw):
                return True, ""

            @staticmethod
            def _safe_int(v, d=0):
                return int(v)

            async def _backup_msg_ids(self, client, gid, items, reason, **kw):
                self.items = items
                return True

            def _record_op(self, *a, **k):
                pass

        h = H()
        await h._lr_recall_user(object(), GID, UID, "被踢出群")
        self.assertEqual("11", h.items[0]["message_id"])
        self.assertEqual("小王", h.items[0]["user_name"])
        self.assertEqual("广告", h.items[0]["segments"][0]["data"]["text"])


if __name__ == "__main__":
    unittest.main()
