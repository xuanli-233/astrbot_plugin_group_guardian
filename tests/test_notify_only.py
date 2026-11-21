"""规则组的「撤回」开关：关掉后命中只抄送到转发群提醒，不撤回。"""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keyword_guard  # noqa: E402
import message_backup  # noqa: E402
import rule_groups  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

GID, UID = "100", "555"


def run(coro):
    return asyncio.run(coro)


class _Config(dict):
    def save_config(self):
        pass


class _Storage(SqliteConnectMixin):
    pass


class _Event:
    def __init__(self):
        self.stopped = False

    def stop_event(self):
        self.stopped = True


class _H(rule_groups.RuleGroupMixin, keyword_guard.KeywordGuardMixin):
    def __init__(self, backup_ok=True):
        self.config = _Config({})
        self._storage = _Storage()
        self.backup_ok = backup_ok
        self.ops, self.logs, self.mutes, self.recalls, self.backups = [], [], [], [], []

    def _cfg(self, key, default=True, group_id=None):
        return default

    def _cfg_int(self, key, default=0, group_id=None):
        return default

    def _cfg_str(self, key, default="", group_id=None):
        return default

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    def _log_moderation(self, group_id, user_id, user_name, text, action, reason, images):
        self.logs.append(action)

    async def _backup_message(self, event, group_id, user_id, user_name, reason, extra_text="", kind="keyword",
                              notify_only=False):
        self.backups.append((kind, notify_only))
        return self.backup_ok

    @staticmethod
    def _event_message_id(event):
        return "m1"

    async def _recall_msg(self, event, msg_id):
        self.recalls.append(msg_id)
        return True

    async def _mute_member(self, event, duration):
        self.mutes.append(duration)
        return True

    def add_group(self, keywords, **action):
        saved, err, _ = self._rg_save({"name": "提醒组", "keywords": keywords, "action": action})
        assert not err, err
        return saved


class RecallSwitchStorageTests(unittest.TestCase):
    def test_old_rows_without_field_still_recall(self):
        h = _H()
        h._rg_init_db()
        with h._storage._connect() as conn:   # 升级前保存的组：action 里没有 recall
            conn.execute("UPDATE rule_groups SET action=? WHERE is_default=1",
                         (json.dumps({"mute": False, "mute_minutes": 30}),))
            conn.commit()
        h._rg_invalidate()
        self.assertTrue(h._rg_default()["action"]["recall"])

    def test_migrated_default_group_recalls(self):
        h = _H()
        self.assertTrue(h._rg_default()["action"]["recall"])

    def test_save_round_trip(self):
        h = _H()
        saved = h.add_group(["广告"], recall=False)
        self.assertFalse(saved["action"]["recall"])
        again = h._rg_list()[0]
        self.assertFalse(again["action"]["recall"])
        self.assertIn("不撤回", h.ops[-1][1]["detail"])
        # 不传 recall = 撤回
        saved2, _err, _ = h._rg_save({"name": "普通组", "keywords": ["x"], "action": {}})
        self.assertTrue(saved2["action"]["recall"])


class NotifyOnlyHitTests(unittest.TestCase):
    def _hit(self, h, **kw):
        ev = _Event()
        handled, notice = run(h._run_hit_guards(ev, GID, UID, "张三", "这里有广告", [], **kw))
        return ev, handled, notice

    def test_notify_only_does_not_recall(self):
        h = _H()
        h.add_group(["广告"], recall=False, notice=True)
        ev, handled, notice = self._hit(h)
        self.assertTrue(handled)
        self.assertTrue(ev.stopped)
        self.assertEqual(h.recalls, [])
        self.assertEqual(h.backups, [("keyword", True)])
        self.assertIsNone(notice)                     # 提示写的是「已撤回」，不撤回时不发
        self.assertTrue(h._last_hit_notify)
        self.assertEqual(h.ops[-1][0], "关键词提醒")
        result = h.ops[-1][1]["result"]
        self.assertIn("仅提醒", result)
        self.assertIn("已抄送", result)
        self.assertNotIn("撤回", h.logs[-1])           # 审核统计按「撤回」二字算拦截次数

    def test_notify_only_reports_failed_copy(self):
        h = _H(backup_ok=False)
        h.add_group(["广告"], recall=False)
        self._hit(h)
        self.assertIn("没有抄送成功", h.ops[-1][1]["result"])
        self.assertNotIn("撤回", h.logs[-1])

    def test_notify_only_still_mutes(self):
        h = _H()
        h.add_group(["广告"], recall=False, mute=True, mute_minutes=5)
        self._hit(h)
        self.assertEqual(h.mutes, [300])
        self.assertIn("禁言", h.ops[-1][1]["result"])

    def test_recall_on_unchanged(self):
        h = _H()
        h.add_group(["广告"], notice=True)
        ev, handled, notice = self._hit(h)
        self.assertEqual(h.recalls, ["m1"])
        self.assertEqual(h.backups, [("keyword", False)])
        self.assertTrue(notice)
        self.assertFalse(h._last_hit_notify)
        self.assertEqual(h.ops[-1][0], "关键词撤回")

    def test_miniapp_notify_only(self):
        h = _H()
        saved, err, _ = h._rg_save({"name": "小程序提醒", "apps": ["微店"], "action": {"recall": False}})
        self.assertFalse(err)
        ev, handled, _n = self._hit(h, mp_items=[{"name": "微店买买", "form": "card"}])
        self.assertTrue(handled)
        self.assertEqual(h.recalls, [])
        self.assertEqual(h.ops[-1][0], "小程序提醒")

    def test_default_fallback_always_recalls(self):
        """码黑名单、码超限、已拉黑的链接不传 action，用默认组处理方式，但默认组关掉撤回也照样撤回。"""
        h = _H()
        d = h._rg_public(h._rg_default())
        d["action"]["recall"] = False
        _saved, err, _ = h._rg_save(d)
        self.assertFalse(err)
        self.assertFalse(h._rg_default()["action"]["recall"])
        run(h._execute_hit_action(_Event(), GID, UID, "张三", "x", "已拉黑的链接", kind="link"))
        self.assertEqual(h.recalls, ["m1"])
        self.assertFalse(h._last_hit_notify)
        # 默认组自己的关键词命中：不撤回
        d["keywords"] = ["广告"]
        h._rg_save(d)
        self._hit(h)
        self.assertEqual(h.recalls, ["m1"])
        self.assertTrue(h._last_hit_notify)


class _B(message_backup.MessageBackupMixin):
    BACKUP_HEADER_MAX = 1000

    def __init__(self):
        self.sent = []

    def _cfg(self, key, default=True, group_id=None):
        return default

    def _backup_targets(self, kind, group_id):
        return [999]

    async def _get_client(self, event=None):
        return object()

    @staticmethod
    def _event_message_id(event):
        return "m1"

    def _event_original_segments(self, event):
        return []

    async def _call_group_api(self, client, action, name="", **kw):
        self.sent.append(kw)
        return True, ""


class BackupLabelTests(unittest.TestCase):
    def _header(self, **kw):
        b = _B()
        self.assertTrue(run(b._backup_message(_Event(), GID, UID, "张三", "关键词: 广告", **kw)))
        return b.sent[0]["messages"][0]["data"]["content"][0]["data"]["text"]

    def test_notify_label(self):
        self.assertIn("违规消息命中提醒（未撤回）", self._header(notify_only=True))
        self.assertIn("葵花码命中提醒（未撤回）", self._header(kind="wxcode", notify_only=True))

    def test_default_label(self):
        self.assertIn("[违规消息备份]", self._header())


if __name__ == "__main__":
    unittest.main()
