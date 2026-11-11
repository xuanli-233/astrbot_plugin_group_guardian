"""踢人后暂停加群（join_lock.py）。"""
import os
import random
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import join_lock  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

GID, UID, OTHER, BOT, ADMIN = "100", "555", "666", "999", "777"
NIGHT = (23 * 60, 8 * 60)


def _ts(hour, minute=0, day=10):
    return datetime(2026, 11, day, hour, minute).timestamp()


def _hm(ts):
    return datetime.fromtimestamp(ts).strftime("%d %H:%M")


def _decrease(sub_type="kick", uid=UID, operator=ADMIN):
    return {"post_type": "notice", "notice_type": "group_decrease", "sub_type": sub_type,
            "group_id": GID, "user_id": uid, "self_id": BOT, "operator_id": operator}


class _Storage(SqliteConnectMixin):
    pass


class _H(join_lock.JoinLockMixin):
    def __init__(self, cfg=None, fail=()):
        self.cfg = {"kick_join_lock_enabled": True}
        self.cfg.update(cfg or {})
        self._storage = _Storage()
        self.fail = set(fail)        # 这些 add_type 调用失败
        self.calls, self.ops = [], []

    def _get_raw_event(self, event):
        return event

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg.get(key, default))

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

    async def _call_group_api(self, client, action, name="", **kw):
        assert action == "set_group_add_option"
        self.calls.append((kw["group_id"], kw["add_type"]))
        if kw["add_type"] in self.fail:
            return False, "没有权限"
        return True, ""

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def drain(self):
        for task in list(getattr(self, "_jl_tasks", set()) or []):
            await task


class PickUnlockTests(unittest.TestCase):
    def test_parse_and_night(self):
        self.assertEqual(23 * 60, join_lock.parse_hhmm("23:00", "01:00"))
        self.assertEqual(60, join_lock.parse_hhmm("乱填", "01:00"))
        self.assertTrue(join_lock.in_night(_ts(23, 0), *NIGHT))
        self.assertTrue(join_lock.in_night(_ts(7, 59), *NIGHT))
        self.assertFalse(join_lock.in_night(_ts(8, 0), *NIGHT))
        self.assertFalse(join_lock.in_night(_ts(22, 59), *NIGHT))
        self.assertTrue(join_lock.in_night(_ts(13), 12 * 60, 14 * 60))   # 不跨天的时间段
        self.assertFalse(join_lock.in_night(_ts(3), 60, 60))             # 起止相同 = 不限制

    def test_daytime_stays_within_range(self):
        now = _ts(10)
        for seed in range(200):
            t = join_lock.pick_unlock_at(now, 3, 6, *NIGHT, rng=random.Random(seed))
            self.assertTrue(now + 3 * 3600 <= t <= now + 6 * 3600)
            self.assertFalse(join_lock.in_night(t, *NIGHT))

    def test_partly_night_picks_only_daytime(self):
        now = _ts(18)   # 3–6 小时后 = 21:00–24:00，只有 21:00–22:59 不在夜间
        seen = set()
        for seed in range(300):
            t = join_lock.pick_unlock_at(now, 3, 6, *NIGHT, rng=random.Random(seed))
            self.assertTrue(_ts(21) <= t <= _ts(22, 59), _hm(t))
            seen.add(t)
        self.assertGreater(len(seen), 30)   # 确实是随机的

    def test_all_night_postponed_to_night_end(self):
        now = _ts(22)   # 3–6 小时后 = 次日 01:00–04:00，全在夜间
        t = join_lock.pick_unlock_at(now, 3, 6, *NIGHT, rng=random.Random(1))
        self.assertEqual("11 08:00", _hm(t))

    def test_never_shorter_than_minimum(self):
        now = _ts(4, 30) + 20   # 带秒：3 小时后是 07:30:20，夜间到 08:00
        for seed in range(100):
            t = join_lock.pick_unlock_at(now, 3, 6, *NIGHT, rng=random.Random(seed))
            self.assertTrue(_ts(8) <= t <= now + 6 * 3600)

    def test_swapped_hours(self):
        now = _ts(10)
        t = join_lock.pick_unlock_at(now, 6, 3, *NIGHT, rng=random.Random(1))
        self.assertTrue(now + 3 * 3600 <= t <= now + 6 * 3600)


class JoinLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_kick_locks_and_restores_when_due(self):
        h = _H()
        self.assertTrue(await h._handle_kick_join_lock(_decrease("kick", operator=BOT)))
        await h.drain()
        self.assertEqual([(100, 2)], h.calls)
        row = h._jl_get(GID)
        self.assertEqual(1, row["kick_count"])
        action, op = h.ops[0]
        self.assertEqual("踢人后暂停加群", action)
        self.assertEqual("成功", op["result"])
        self.assertIn("机器人", op["reason"])
        self.assertIn("需要验证", op["detail"])
        # 没到点不恢复
        self.assertEqual(0, await h._jl_tick(now=row["unlock_at"] - 1))
        self.assertEqual([(100, 2)], h.calls)
        # 到点恢复为「需要验证」
        self.assertEqual(1, await h._jl_tick(now=row["unlock_at"]))
        self.assertEqual([(100, 2), (100, 1)], h.calls)
        self.assertIsNone(h._jl_get(GID))
        self.assertEqual(("恢复加群方式", "成功"), (h.ops[-1][0], h.ops[-1][1]["result"]))
        self.assertEqual([], h._jl_status())

    async def test_restore_type_allow_any(self):
        h = _H({"kick_join_lock_restore_type": 0})
        await h._jl_lock_group(object(), GID, UID, now=_ts(10))
        await h._jl_tick(now=_ts(20))
        self.assertEqual([(100, 2), (100, 0)], h.calls)

    async def test_leave_and_disabled_do_not_trigger(self):
        h = _H()
        self.assertFalse(await h._handle_kick_join_lock(_decrease("leave", operator=UID)))
        self.assertFalse(await h._handle_kick_join_lock(_decrease("kick_me", uid=BOT)))
        self.assertFalse(await h._handle_kick_join_lock({"post_type": "message"}))
        self.assertFalse(await _H({"kick_join_lock_enabled": False})._handle_kick_join_lock(_decrease()))
        # sub_type=leave 但操作人是别人：按被踢处理
        self.assertTrue(await h._handle_kick_join_lock(_decrease("leave", operator=ADMIN)))
        await h.drain()
        self.assertEqual([(100, 2)], h.calls)

    async def test_second_kick_only_postpones(self):
        h = _H()
        first = await h._jl_lock_group(object(), GID, UID, now=_ts(9))
        second = await h._jl_lock_group(object(), GID, OTHER, now=_ts(9, 5))
        self.assertTrue(second["extended"])
        self.assertGreaterEqual(second["unlock_at"], first["unlock_at"])
        self.assertEqual([(100, 2)], h.calls)          # 不重复调用接口
        self.assertEqual(1, len(h.ops))                # 10 分钟内不重复写操作记录
        third = await h._jl_lock_group(object(), GID, OTHER, now=_ts(14))
        self.assertGreaterEqual(third["unlock_at"], _ts(17))   # 至少是最后一次踢人后 3 小时
        self.assertEqual(2, len(h.ops))
        row = h._jl_get(GID)
        self.assertEqual((3, OTHER), (row["kick_count"], row["last_user"]))

    async def test_lock_failure_is_recorded_and_not_stored(self):
        h = _H(fail={2})
        res = await h._jl_lock_group(object(), GID, UID, now=_ts(10))
        self.assertFalse(res["ok"])
        self.assertIsNone(h._jl_get(GID))
        self.assertEqual("失败", h.ops[0][1]["result"])
        self.assertIn("没有权限", h.ops[0][1]["detail"])

    async def test_restore_failure_retries_with_backoff(self):
        h = _H(fail={1})
        await h._jl_lock_group(object(), GID, UID, now=_ts(10))
        due = h._jl_get(GID)["unlock_at"]
        self.assertEqual(0, await h._jl_tick(now=due))
        row = h._jl_get(GID)
        self.assertEqual((1, due + 60, "没有权限"), (row["retry_count"], row["next_retry_at"], row["last_error"]))
        self.assertEqual(0, await h._jl_tick(now=due + 30))      # 还没到重试时间
        self.assertEqual(2, len(h.calls))
        self.assertEqual(0, await h._jl_tick(now=due + 60))
        self.assertEqual(due + 60 + 120, h._jl_get(GID)["next_retry_at"])
        self.assertEqual(1, [a for a, _k in h.ops].count("恢复加群方式"))   # 自动重试只记第一次失败
        self.assertEqual(2, h._jl_status([{"group_id": GID, "group_name": "测试群"}])[0]["retry_count"])
        h.fail.clear()
        self.assertEqual(1, await h._jl_tick(now=due + 600))
        self.assertIsNone(h._jl_get(GID))

    async def test_manual_restore_and_still_restores_when_disabled(self):
        h = _H()
        await h._jl_lock_group(object(), GID, UID, now=_ts(10))
        h.cfg["kick_join_lock_enabled"] = False
        ok, _err = await h._jl_restore_group(object(), GID, source="WebUI", operator="张三")
        self.assertTrue(ok)
        self.assertEqual(("WebUI", "张三", "手动恢复"),
                         (h.ops[-1][1]["source"], h.ops[-1][1]["operator"], h.ops[-1][1]["reason"]))
        ok, err = await h._jl_restore_group(object(), GID, source="WebUI")
        self.assertFalse(ok)
        self.assertIn("没有在暂停", err)


if __name__ == "__main__":
    unittest.main()
