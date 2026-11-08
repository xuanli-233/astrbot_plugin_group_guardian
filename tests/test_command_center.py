"""指令权限中心（command_center.py）：默认值、谁能用、开关、拒绝回复节流、入口接入、/指令列表。"""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import command_center  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ADMIN, OWNER, ADMIN, MEMBER, SUPER, BLOCKED = "1", "2", "3", "4", "5", "6"


class _Storage(SqliteConnectMixin):
    def is_group_super_admin(self, gid, uid):
        return uid == SUPER


class _Event:
    def __init__(self, uid, gid="100"):
        self.uid, self.gid, self.stopped = uid, gid, False

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class _H(command_center.CommandCenterMixin):
    def __init__(self):
        self._storage = _Storage()
        self.ops = []
        self.cfg = {}

    def _try_get_sender_id(self, event):
        return event.uid

    def _get_group_id(self, event):
        return event.gid

    async def _is_plugin_admin(self, event):
        return event.uid == PLUGIN_ADMIN

    async def _get_member_role(self, event, gid, uid):
        return {OWNER: "owner", ADMIN: "admin"}.get(uid, "member")

    def _is_group_admin_blocked(self, gid, uid):
        return uid == BLOCKED

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _record_op(self, action, **kw):
        self.ops.append((action, kw.get("detail")))


async def _run(h, event, name, body=("ran",)):
    async def gen():
        for x in body:
            yield x
    return [x async for x in h._cmd_guarded(event, name, gen())]


class CommandCenterTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults(self):
        h = _H()
        s = h._cmd_settings()
        self.assertEqual(s["禁言"], {"enabled": True, "level": "owner"})
        self.assertEqual(s["群统计"]["level"], "owner")
        self.assertEqual(s["全群公告"]["level"], "plugin_admin")
        self.assertEqual(s["指令列表"]["level"], "all")
        listing = {c["name"]: c for c in h._cmd_list()}
        self.assertEqual([lv["value"] for lv in listing["禁言"]["levels"]], ["plugin_admin", "owner", "admin"])
        self.assertEqual([lv["value"] for lv in listing["全群公告"]["levels"]], ["plugin_admin"])
        self.assertEqual([lv["value"] for lv in listing["群统计"]["levels"]], ["plugin_admin", "owner", "admin", "all"])
        self.assertFalse(listing["禁言"]["feature_on"])  # ban_enabled 没开
        h.cfg["ban_enabled"] = True
        self.assertTrue({c["name"]: c for c in h._cmd_list()}["禁言"]["feature_on"])

    async def test_who_can_use(self):
        h = _H()
        self.assertEqual(await _run(h, _Event(MEMBER), "禁言"), ["「/禁言」仅群主和插件管理员可以使用"])
        self.assertEqual(await _run(h, _Event(MEMBER), "禁言"), [])  # 30 秒内不重复回复
        self.assertEqual(await _run(h, _Event(ADMIN), "禁言"), ["「/禁言」仅群主和插件管理员可以使用"])
        ev = _Event(OWNER)
        self.assertEqual(await _run(h, ev, "禁言"), ["ran"])
        self.assertTrue(getattr(ev, "_gg_cmd_granted", False))  # 业务里的 _is_admin 以面板为准
        self.assertEqual(await _run(h, _Event(SUPER), "禁言"), ["ran"])  # 群超管与群主同级
        ev = _Event(PLUGIN_ADMIN)
        self.assertEqual(await _run(h, ev, "禁言"), ["ran"])
        self.assertFalse(getattr(ev, "_gg_cmd_granted", False))
        blocked = _Event(BLOCKED)
        self.assertEqual(await _run(h, blocked, "群统计"), [])
        self.assertTrue(blocked.stopped)
        # 放宽到群管理员
        self.assertEqual(h._cmd_set("禁言", level="admin"), "")
        self.assertEqual(await _run(h, _Event(ADMIN), "禁言"), ["ran"])
        self.assertIn("最宽只能设为", h._cmd_set("禁言", level="all"))
        h._cmd_set("群统计", level="all")
        self.assertEqual(await _run(h, _Event(MEMBER), "群统计"), ["ran"])
        # 仅插件管理员的指令
        self.assertEqual(await _run(h, _Event(OWNER), "全群公告"), ["「/全群公告」仅插件管理员可以使用"])
        self.assertIn("最宽只能设为", h._cmd_set("全群公告", level="owner"))
        # 私聊：只有插件管理员和「所有人」的指令
        self.assertEqual(await _run(h, _Event(OWNER, gid=""), "禁言"), ["「/禁言」仅群主、群管理员和插件管理员可以使用"])

    async def test_disabled(self):
        h = _H()
        self.assertEqual(h._cmd_set("踢人", enabled=False), "")
        member = _Event(OWNER)
        self.assertEqual(await _run(h, member, "踢人"), [])  # 不回复
        self.assertTrue(member.stopped)
        self.assertEqual(await _run(h, _Event(PLUGIN_ADMIN), "踢人"), ["「/踢人」已在管理面板关闭"])
        self.assertIn("关闭", h.ops[-1][1])
        h._cmd_reset()
        self.assertEqual(await _run(h, _Event(OWNER), "踢人"), ["ran"])
        self.assertTrue(all(c["is_default"] for c in h._cmd_list()))

    async def test_denied_command_body_never_runs(self):
        h = _H()
        ran = []

        async def body():
            ran.append(1)
            yield "x"
        out = [x async for x in h._cmd_guarded(_Event(MEMBER), "禁言", body())]
        self.assertEqual((out, ran), (["「/禁言」仅群主和插件管理员可以使用"], []))

    async def test_command_list(self):
        h = _H()
        out = [x async for x in h.cmd_command_list(_Event(MEMBER))]
        self.assertIn("/指令列表", out[0])
        self.assertNotIn("/禁言", out[0])
        out = [x async for x in h.cmd_command_list(_Event(OWNER))]
        self.assertIn("/禁言", out[0])
        self.assertNotIn("/全群公告", out[0])
        h._cmd_set("踢人", enabled=False)
        self.assertNotIn("/踢人 ", [x async for x in h.cmd_command_list(_Event(OWNER))][0])
        out = [x async for x in h.cmd_command_list(_Event(PLUGIN_ADMIN))][0]
        self.assertIn("/踢人 移出群聊：/踢人 <QQ号或@某人>（已关闭）", out)
        self.assertIn("/全群公告", out)


class WiringTests(unittest.TestCase):
    def test_every_command_entry_is_guarded_and_registered(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        names = re.findall(r'@filter\.command\("([^"]+)"\)', main)
        self.assertEqual(set(names), set(command_center.COMMAND_SPECS))
        for name in names:
            with self.subTest(name=name):
                self.assertIn(f'self._cmd_guarded(event, "{name}", ', main)
        self.assertIn("CommandCenterMixin", main)

    def test_is_admin_honours_panel_grant(self):
        onebot = (ROOT / "onebot.py").read_text(encoding="utf-8")
        i = onebot.index("async def _is_admin(")
        body = onebot[i:onebot.index("async def _is_plugin_admin(", i)]
        # 被移除群管权限的人优先拒绝，其次才认面板授权
        self.assertLess(body.index("_is_group_admin_blocked"), body.index('_gg_cmd_granted'))


if __name__ == "__main__":
    unittest.main()
