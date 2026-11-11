"""引流广告自动处理：群名片卡片识别、规则组的群名片规则、命中后踢出并拉黑（只踢新进群的人）。"""
import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import card_parse  # noqa: E402
import keyword_guard  # noqa: E402
import rule_groups  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

GID, UID, ADMIN = "100", "555", "900"
# 真实的群名片卡片（分享群 646536731）
GROUP_CARD = json.dumps({
    "app": "com.tencent.contact.lua", "desc": "", "view": "contact", "bizsrc": "qun.share", "ver": "0.0.0.1",
    "prompt": "群名片: 为他庆生| 今年还剩51",
    "meta": {"contact": {
        "avatar": "https://p.qlogo.cn/gh/646536731/646536731/100", "contact": "在这里，发现更多~",
        "jumpUrl": "mqqapi://card/show_pslcard?authSig=4jdP2AOO8%2Bcr&card_type=group&src_type=internal&uin=646536731&version=1",
        "nickname": "为他庆生| 今年还剩51", "tag": "群名片",
        "pcJumpUrl": "tencent://groupwpa/?jump_from=&param=7b22&subcmd=all"}},
}, ensure_ascii=False)
USER_CARD = json.dumps({
    "app": "com.tencent.contact.lua", "view": "contact", "prompt": "推荐联系人: 小明",
    "meta": {"contact": {"avatar": "https://q.qlogo.cn/g?b=qq&nk=12345678&s=100", "contact": "帐号:12345678",
                         "jumpUrl": "mqqapi://card/show_pslcard?src_type=internal&source=sharecard&version=1&uin=12345678",
                         "nickname": "小明", "tag": "推荐联系人"}},
}, ensure_ascii=False)


def _gcard(uin="646536731"):
    return card_parse.parse_card(GROUP_CARD.replace("646536731", uin))


class _Config(dict):
    def save_config(self):
        pass


class _Storage(SqliteConnectMixin):
    pass


class _Event:
    def stop_event(self):
        pass


class _H(rule_groups.RuleGroupMixin, keyword_guard.KeywordGuardMixin):
    def __init__(self, own=("100", "200200"), join_days=1.0, api_join=None, exempt=(), kick_fail=""):
        self.config = _Config({})
        self._storage = _Storage()
        self.own = set(own)
        self.members = {} if join_days is None else {(GID, UID): {"join_time": int(time.time() - join_days * 86400)}}
        self.api_join, self.exempt, self.kick_fail = api_join, set(exempt), kick_fail
        self.ops, self.mutes, self.kicks, self.cross, self.blacklist, self.backups, self.api = [], [], [], [], [], [], []

    def _cfg(self, key, default=True, group_id=None):
        return default

    def _cfg_int(self, key, default=0, group_id=None):
        return default

    def _cfg_str(self, key, default="", group_id=None):
        return default

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    def _log_moderation(self, *a):
        pass

    def _dir(self):
        return {"groups": {g: {} for g in self.own}}

    def _dir_cached_member(self, gid, uid):
        return self.members.get((str(gid), str(uid)))

    async def _call_group_api_result(self, client, action, name="", **kw):
        self.api.append(action)
        if self.api_join is None:
            return False, None, "查不到"
        return True, {"join_time": self.api_join}, ""

    async def _get_client(self, event=None):
        return object()

    async def _backup_message(self, event, group_id, user_id, user_name, reason, extra_text="", kind="keyword"):
        self.backups.append(kind)
        return True

    @staticmethod
    def _event_message_id(event):
        return "m1"

    async def _recall_msg(self, event, msg_id):
        return True

    async def _mute_member(self, event, duration):
        self.mutes.append(duration)
        return True

    def _is_blacklist_exempt(self, uid):
        return str(uid) in self.exempt

    def _add_user_blacklist(self, uid, reason="", source="自动", operator="", group_id=""):
        self.blacklist.append((uid, reason, source))
        return True

    async def _kick_targets_in_group(self, client, group_id, targets, block, assume_present=False, log=None):
        self.kicks.append((str(group_id), list(targets), block, assume_present, dict(log or {})))
        if self.kick_fail:
            return {"kicked": [], "failed": {targets[0]: self.kick_fail}, "absent": 0, "skipped": ""}
        return {"kicked": list(targets), "failed": {}, "absent": 0, "skipped": ""}

    async def _list_managed_groups(self, client):
        return sorted(self.own), ""

    async def _kick_users_across_groups(self, client, targets, block, groups=None, reason="", source="", operator=""):
        self.cross.append((list(targets), block, list(groups or []), source))
        return {}

    async def drain(self):
        for task in list(getattr(self, "_hg_tasks", set()) or []):
            await task

    def group(self, **over):
        data = {"name": "引流广告", "keywords": ["勤工俭学"], "cards": {"group_card": "others"},
                "action": {"mute": True, "mute_minutes": 60, "kick": "block", "kick_days": 7}}
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k] = dict(data[k], **v)
            else:
                data[k] = v
        saved, err, _errs = self._rg_save(data)
        assert not err, err
        return saved


class CardParseTests(unittest.TestCase):
    def test_group_card_is_recognised(self):
        c = _gcard()
        self.assertEqual((c["kind"], c["share_type"], c["share_uin"], c["title"]),
                         ("群名片", "group", "646536731", "为他庆生| 今年还剩51"))
        self.assertIn("群号:646536731", card_parse.card_text(c))
        self.assertIn("分享的群 646536731", card_parse.CardParseMixin._card_live_lines([c])[0])

    def test_group_card_without_uin_in_jump_url(self):
        raw = json.loads(GROUP_CARD)
        raw["meta"]["contact"]["jumpUrl"] = "mqqapi://card/show_pslcard?card_type=group"
        c = card_parse.parse_card(raw)
        self.assertEqual((c["share_type"], c["share_uin"]), ("group", "646536731"))   # 从头像地址里取群号

    def test_personal_card_is_not_a_group_card(self):
        c = card_parse.parse_card(USER_CARD)
        self.assertEqual((c["share_type"], c["share_uin"], c["kind"]), ("user", "12345678", "推荐名片"))


class CardRuleTests(unittest.TestCase):
    def test_others_mode_ignores_own_groups_and_allow_list(self):
        h = _H()
        h.group(cards={"group_card": "others", "allow": ["777777"]})
        self.assertEqual(h._rg_match_card(_gcard(), GID)[0]["name"], "引流广告")
        self.assertIn("646536731", h._rg_match_card(_gcard(), GID)[1])
        self.assertEqual(h._rg_match_card(_gcard("200200"), GID), (None, None))        # 机器人也在的群
        self.assertEqual(h._rg_match_card(_gcard("777777"), GID), (None, None))     # 允许分享的群
        self.assertEqual(h._rg_match_card(card_parse.parse_card(USER_CARD), GID), (None, None))
        self.assertEqual(h._rg_test(card_group="646536731")["card"]["group"], "引流广告")
        self.assertIsNone(h._rg_test(card_group="200200")["card"])

    def test_others_mode_is_skipped_when_own_groups_unknown(self):
        h = _H(own=())
        h.group()
        self.assertEqual(h._rg_match_card(_gcard(), GID), (None, None))

    def test_all_mode_and_off(self):
        h = _H()
        g = h.group(cards={"group_card": "all"})
        self.assertIsNotNone(h._rg_match_card(_gcard("200200"), GID)[0])
        h._rg_save(dict(g, cards={"group_card": ""}))
        self.assertEqual(h._rg_match_card(_gcard(), GID), (None, None))

    def test_validation_and_persistence(self):
        h = _H()
        g = h.group(cards={"group_card": "others", "allow": "111111, 222222\n111111"})
        self.assertEqual(g["cards"], {"group_card": "others", "allow": ["111111", "222222"]})
        self.assertEqual((g["action"]["kick"], g["action"]["kick_days"]), ("block", 7))
        h._rg_db_ready = False
        h._rg_invalidate()
        self.assertEqual(h._rg_list()[0]["cards"]["allow"], ["111111", "222222"])   # 重新读库后还在
        base = {"name": "x", "keywords": ["a"]}
        self.assertIn("群名片", h._rg_save(dict(base, cards={"group_card": "bad"}))[1])
        self.assertIn("数字", h._rg_save(dict(base, cards={"allow": ["abc"]}))[1])
        self.assertIn("踢人方式", h._rg_save(dict(base, action={"kick": "ban"}))[1])
        self.assertIn("入群天数", h._rg_save(dict(base, action={"kick": "kick", "kick_days": -1}))[1])
        self.assertIn("踢出并拉黑（入群不满 7 天）", h.ops[0][1]["detail"])


class AutoKickTests(unittest.IsolatedAsyncioTestCase):
    async def _hit(self, h, text="来勤工俭学群"):
        handled, _notice = await h._run_hit_guards(_Event(), GID, UID, "荒", text, [])
        await h.drain()
        return handled

    async def test_new_member_is_kicked_blocked_and_removed_from_other_groups(self):
        h = _H(join_days=1)
        h.group()
        self.assertTrue(await self._hit(h))
        gid, targets, block, assume, log = h.kicks[0]
        self.assertEqual((gid, targets, block, assume, log["source"]), (GID, [UID], True, True, "自动"))
        self.assertIn("关键词: 勤工俭学", log["reason"])
        self.assertEqual(h.blacklist[0][0], UID)
        self.assertEqual(h.cross, [([UID], True, ["200200"], "自动")])     # 其它群在后台踢，不含本群
        self.assertEqual(h.mutes, [])                                   # 踢了就不再禁言
        action, op = h.ops[-1]
        self.assertEqual(action, "关键词撤回")
        self.assertIn("踢出并拉黑", op["result"])

    async def test_old_member_is_only_muted(self):
        h = _H(join_days=40)
        h.group()
        self.assertTrue(await self._hit(h))
        self.assertEqual((h.kicks, h.blacklist, h.mutes), ([], [], [3600]))
        self.assertIn("入群已 40 天", h.ops[-1][1]["reason"])

    async def test_unknown_join_time_is_not_kicked_but_api_is_tried(self):
        h = _H(join_days=None)
        h.group()
        await self._hit(h)
        self.assertEqual((h.kicks, h.mutes, h.api), ([], [3600], ["get_group_member_info"]))
        self.assertIn("不知道入群时间", h.ops[-1][1]["reason"])
        h2 = _H(join_days=None, api_join=int(time.time()) - 3600)       # 缓存里没有，协议端查到了刚进群
        h2.group()
        await self._hit(h2)
        self.assertEqual(len(h2.kicks), 1)

    async def test_kick_days_zero_ignores_join_time(self):
        h = _H(join_days=400)
        h.group(action={"kick": "block", "kick_days": 0})
        await self._hit(h)
        self.assertEqual(len(h.kicks), 1)

    async def test_exempt_users_and_kick_only_mode(self):
        h = _H(exempt={UID})
        h.group()
        await self._hit(h)
        self.assertEqual((h.kicks, h.blacklist, h.mutes), ([], [], [3600]))
        h2 = _H()
        h2.group(action={"kick": "kick"})
        await self._hit(h2)
        self.assertEqual((h2.kicks[0][2], h2.blacklist, h2.cross), (False, [], []))   # 只踢本群，不拉黑
        self.assertIn("+踢出", h2.ops[-1][1]["result"])

    async def test_kick_failure_falls_back_to_mute(self):
        h = _H(kick_fail="目标是管理员")
        h.group()
        await self._hit(h)
        self.assertEqual(h.mutes, [3600])
        self.assertEqual(h.cross, [])
        self.assertIn("踢出并拉黑失败：目标是管理员", h.ops[-1][1]["reason"])

    async def test_group_without_kick_behaves_as_before(self):
        h = _H()
        h.group(action={"kick": ""})
        await self._hit(h)
        self.assertEqual((h.kicks, h.mutes), ([], [3600]))
        self.assertNotIn("踢", h.ops[-1][1]["reason"])

    async def test_group_card_hit_goes_through_same_action(self):
        h = _H()
        g = h.group(keywords=[])
        group, rule = h._rg_match_card(_gcard(), GID)
        await h._execute_hit_action(_Event(), GID, UID, "荒", "[群名片]", rule, kind="groupcard",
                                    action=h._rg_action(group))
        await h.drain()
        self.assertEqual(g["id"], group["id"])
        self.assertEqual((len(h.kicks), h.backups), (1, ["keyword"]))   # 备份归到「关键词命中」类别
        self.assertEqual(h.ops[-1][0], "群名片撤回")


if __name__ == "__main__":
    unittest.main()
