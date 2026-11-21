"""群主 @成员 全群拉黑（owner_blacklist.py）：权限、理由必填、按群开关、@机器人 帮助。"""
import importlib.util
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_minimal_web as T  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{T.PKG}.{name}", os.path.join(ROOT, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


owner_blacklist = _load("owner_blacklist")
command_center = _load("command_center")
BOT = "2854000001"


class _H(owner_blacklist.OwnerBlacklistMixin, command_center.CommandCenterMixin, T._Harness):
    def __init__(self, client):
        super().__init__(client)
        self._bot_uin_cache = int(BOT)
        self.ov = {}            # {(群, 配置项): 值}
        self.ops = []
        self._admin_role_cache, self._admin_role_cache_ttl = {}, 30.0
        st = self._storage
        st.get_group_configs = lambda gid: {k: v for (g, k), v in self.ov.items() if g == str(gid)}
        st.set_group_config = lambda gid, k, v: self.ov.__setitem__((str(gid), k), v)
        st.delete_group_config = lambda gid, k: self.ov.pop((str(gid), k), None) is not None
        st.is_group_super_admin = lambda gid, uid: False

    @staticmethod
    def _get_raw_event(event):          # Main 里来自 membership.py
        return getattr(event, "raw_event", None)

    def _get_group_override(self, group_id, key):
        return self.ov.get((str(group_id), key))

    def _invalidate_group_cfg_cache(self, group_id=""):
        pass

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    def _bq_create_job(self, title, kind, items, **kw):
        self.job = {"id": 1, "title": title, "items": len(items), "mode": "manual", "eta_seconds": 0, "raw": items, **kw}
        return self.job


def _msg(group_id, user_id, segs, role="member"):
    raw = {"post_type": "message", "message_type": "group", "group_id": int(group_id), "user_id": int(user_id),
           "self_id": int(BOT), "sender": {"user_id": int(user_id), "role": role}, "message": segs}
    return types.SimpleNamespace(raw_event=raw, group_id=str(group_id), get_sender_id=lambda: str(user_id),
                                 message_str="".join(s["data"].get("text", "") for s in segs if s["type"] == "text"))


def at(qq):
    return {"type": "at", "data": {"qq": str(qq)}}


def tx(text):
    return {"type": "text", "data": {"text": text}}


class OwnerBlacklistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = T._Client(T._groups())
        self.h = _H(self.client)
        await self.h._dir_groups(self.client, force=True)
        for g in ("10001", "10002"):
            await self.h._dir_members(self.client, g, force=True)

    async def run_msg(self, segs, uid="20001", role="owner", gid="10001"):
        return await self.h._ob_handle(_msg(gid, uid, segs, role))

    async def test_owner_blacklists_across_groups(self):
        reply = await self.run_msg([at(30001), tx(" 全群拉黑 发广告")])
        self.assertIn("已将 30001 加入黑名单，理由：发广告", reply)
        self.assertIn("30001", self.h._user_black_set)
        groups = {i["group_id"] for i in self.h.job["raw"]}
        self.assertTrue({"10001", "10002"} <= groups)                 # 所有管理的群，不只是发指令的群
        self.assertTrue(all(i["params"]["block"] for i in self.h.job["raw"]))
        self.assertEqual(self.h.job["meta"]["notify_group"], "10001")
        self.assertIn("发广告", self.h.job["raw"][0]["params"]["reason"])

    async def test_reason_required_and_variants(self):
        reply = await self.run_msg([at(30001), tx(" 全群拉黑 ")])
        self.assertIn("理由", reply)
        self.assertNotIn("30001", self.h._user_black_set)
        reply = await self.run_msg([tx("全群拉黑 理由：刷屏 "), at(30001)])            # 先写指令再 @，带「理由：」
        self.assertIn("理由：刷屏", reply)

    async def test_member_or_admin_denied(self):
        reply = await self.run_msg([at(30001), tx(" 全群拉黑 发广告")], uid="20002", role="admin")
        self.assertIn("仅群主和机器人管理员", reply)
        self.assertNotIn("30001", self.h._user_black_set)
        again = await self.run_msg([at(30001), tx(" 全群拉黑 发广告")], uid="20002", role="admin")
        self.assertEqual(again, "")                                   # 30 秒内不重复提示
        self.assertIsNone(await self.run_msg([tx("全群拉黑是什么意思")], uid="20003", role="member"))  # 聊天，不是指令

    async def test_plugin_admin_allowed(self):
        reply = await self.run_msg([at(30001), tx("全群拉黑 发广告")], uid="10000", role="member")
        self.assertIn("加入黑名单", reply)

    async def test_per_group_switch(self):
        self.h._ob_set_groups(["10001"], "off")
        reply = await self.run_msg([at(30001), tx("全群拉黑 发广告")])
        self.assertIn("没有开放", reply)
        self.h.config["owner_at_blacklist_enabled"] = False                    # 总开关关，单独开放的群照样能用
        self.h._ob_set_groups(["10002"], "on")
        self.assertIn("加入黑名单", await self.run_msg([at(30001), tx("全群拉黑 发广告")], gid="10002"))
        st = {g["group_id"]: g for g in self.h._ob_group_states()["groups"]}
        self.assertEqual((st["10001"]["state"], st["10001"]["effective"]), ("off", False))
        self.assertEqual((st["10002"]["state"], st["10002"]["effective"]), ("on", True))
        self.h._ob_set_groups(["10001", "10002"], "inherit")
        self.assertFalse(any(g["effective"] for g in self.h._ob_group_states()["groups"]))
        self.assertEqual(self.h.ops[-1][0], "修改群主@拉黑")

    async def test_protected_targets_skipped(self):
        reply = await self.run_msg([at(10000), at(BOT), tx("全群拉黑 发广告")])
        self.assertNotIn("10000", self.h._user_black_set)
        self.assertNotIn(BOT, self.h._user_black_set)
        self.assertIn("已跳过", reply)

    async def test_help_for_owner_only(self):
        reply = await self.run_msg([at(BOT)])
        self.assertIn("全群拉黑", reply)
        self.assertIn("/指令列表", reply)
        self.assertIsNotNone(await self.run_msg([at(BOT), tx(" 帮助")]))
        self.assertIsNone(await self.run_msg([at(BOT)], uid="20003", role="member"))     # 普通成员 @机器人：不管
        self.assertIsNone(await self.run_msg([at(BOT), tx(" 今天天气怎么样")]))           # 群主和机器人聊天：不管

    async def test_unrelated_messages_ignored(self):
        self.assertIsNone(await self.run_msg([tx("大家好")]))
        self.assertIsNone(await self.run_msg([at(30001), tx(" 你好")]))


if __name__ == "__main__":
    unittest.main()
