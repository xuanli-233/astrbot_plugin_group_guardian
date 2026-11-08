"""规则组（rule_groups.py）与小程序（miniapp.py）：迁移、匹配顺序与范围、处理方式、小程序识别 / 归并 / 记录 / 统计。"""
import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import card_parse  # noqa: E402
import keyword_guard  # noqa: E402
import miniapp  # noqa: E402
import rule_groups  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

CARD = json.dumps({
    "app": "com.tencent.miniapp.lua", "view": "miniapp", "prompt": "[微信小程序]我正在[刺猬云印]打印文件,低至88折,快来打印吧！",
    "meta": {"miniapp": {
        "tag": "微信小程序", "title": "我正在[刺猬云印]打印文件,低至88折,快来打印吧！", "source": "刺猬云印丨打印5分 包邮到家",
        "sourcelogo": "https://miniapp.gtimg.cn/generated-icon/wx9580c9208762a7c5.png",
        "preview": "https://qq.ugcimg.cn/v1/abc/def",
        "jumpUrl": "https://m.q.qq.com/a/s/cfae5456842aa26ff8d16e8cf5eb42e7",
        "ark_reserved1": "{\"mini_app_id\":\"wx9580c9208762a7c5\",\"mini_app_type\":0,\"applet_source\":2}"}},
}, ensure_ascii=False)


WEIDIAN = json.dumps({
    "app": "com.tencent.miniapp.lua", "view": "miniapp",
    "prompt": "[微信小程序]【差1人】｜19.9元拼 27考研英语-万能作文模板 英一24+/英二20+ （持续更新…） ",
    "meta": {"miniapp": {
        "tag": "微信小程序", "title": "【差1人】｜19.9元拼 27考研英语-万能作文模板 英一24+/英二20+ （持续更新…） ",
        "source": "微店买买", "sourcelogo": "https://miniapp.gtimg.cn/generated-icon/wx4d1258677af59f5c.png",
        "preview": "https://qq.ugcimg.cn/v1/x/y", "jumpUrl": "https://m.q.qq.com/a/s/b225ee7ce08331bb6d23b18757d65576",
        "ark_reserved1": "{\"mini_app_id\":\"wx4d1258677af59f5c\",\"mini_app_type\":0,\"applet_source\":2}"}},
}, ensure_ascii=False)


class _Config(dict):
    def save_config(self):
        pass


class _Storage(SqliteConnectMixin):
    pass


class _H(rule_groups.RuleGroupMixin, miniapp.MiniAppMixin, keyword_guard.KeywordGuardMixin):
    def __init__(self, config=None, cfg=None):
        self.config = _Config(config or {})
        self.cfg = dict(cfg or {})
        self._storage = _Storage()
        self.hits, self.mutes, self.ops = [], [], []

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg.get(key, default))

    def _record_op(self, action, **kw):
        self.ops.append(action)

    # 命中处理用到的协议操作
    async def _backup_message(self, *a, **k):
        return True

    @staticmethod
    def _event_message_id(event):
        return "m1"

    async def _recall_msg(self, event, msg_id):
        return True

    async def _mute_member(self, event, duration):
        self.mutes.append(duration)
        return True

    def _log_moderation(self, *args):
        self.hits.append(args)

    async def _download_bytes(self, url, max_bytes=0, timeout=0):
        return None


class _Event:
    def __init__(self):
        self.stopped = False

    def stop_event(self):
        self.stopped = True


def _card():
    return card_parse.parse_card(CARD)


class RuleGroupTests(unittest.IsolatedAsyncioTestCase):
    def test_migrates_old_config_into_default_group(self):
        h = _H(config={"keyword_rules": ["加微信", "=1"], "wxcode_regex_rules": ["invite"], "hit_mute_enabled": True,
                       "moderation_ban_duration": 600, "hit_notice_enabled": True, "hit_notice_template": "撤了 {name}"})
        g = h._rg_default()
        self.assertEqual((g["name"], g["keywords"], g["code_rules"]), ("默认组", ["加微信", "=1"], ["invite"]))
        self.assertEqual(g["action"], {"mute": True, "mute_minutes": 10, "notice": True, "notice_template": "撤了 {name}"})
        # 只迁移一次：之后改配置不影响
        h.config["keyword_rules"] = ["别的"]
        h._rg_db_ready = False
        h._rg_invalidate()
        self.assertEqual(h._rg_default()["keywords"], ["加微信", "=1"])

    def test_order_scope_and_default_last(self):
        h = _H(config={"keyword_rules": ["广告"]})
        a, err, _ = h._rg_save({"name": "严重", "keywords": ["广告", "代刷"], "action": {"mute": True, "mute_minutes": 60}})
        self.assertEqual(err, "")
        b, err, _ = h._rg_save({"name": "只管一群", "keywords": ["代刷"], "scope": ["100"]})
        # 组「严重」先建，排在前面；默认组总在最后
        self.assertEqual([g["name"] for g in h._rg_list()], ["严重", "只管一群", "默认组"])
        self.assertEqual(h._rg_match_text("这是广告", "1")[0]["name"], "严重")
        h._rg_move(b["id"], -1)
        self.assertEqual(h._rg_match_text("代刷", "100")[0]["name"], "只管一群")
        self.assertEqual(h._rg_match_text("代刷", "200")[0]["name"], "严重")  # 不在范围的群跳过
        self.assertEqual(h._rg_move(h._rg_default()["id"], -1), "默认组总是最后检查，不能移动")

    def test_validation_and_default_protection(self):
        h = _H()
        _g, err, errors = h._rg_save({"name": "坏", "keywords": ["re:(a+)+", "re:[bad"], "code_rules": ["ok"]})
        self.assertIn("2 条规则无法使用", err)
        self.assertEqual(len(errors), 2)
        self.assertIn("组名", h._rg_save({"name": " "})[1])
        self.assertIn("禁言时长", h._rg_save({"name": "x", "action": {"mute_minutes": 0}})[1])
        d = h._rg_default()
        self.assertEqual(h._rg_delete(d["id"]), "默认组不能删除")
        data = h._rg_public(d)
        data["enabled"] = False
        self.assertIn("不能停用", h._rg_save(data)[1])

    def test_disabled_group_is_skipped(self):
        h = _H()
        g, _err, _ = h._rg_save({"name": "停用的", "enabled": False, "keywords": ["停"]})
        self.assertIsNone(h._rg_match_text("停", "1")[0])
        self.assertEqual(h._rg_delete(g["id"]), "")

    async def test_hit_uses_group_action(self):
        h = _H(config={"keyword_rules": ["普通"]})
        h._rg_save({"name": "严重", "keywords": ["代刷"], "action": {"mute": True, "mute_minutes": 5, "notice": True,
                                                                  "notice_template": "{name} 违规"}})
        handled, notice = await h._run_hit_guards(_Event(), "1", "555", "张三", "专业代刷", [])
        self.assertTrue(handled)
        self.assertEqual((h.mutes, notice), ([300], "张三 违规"))
        self.assertIn("（严重）", h.hits[-1][5])
        # 默认组：没开禁言 / 提示
        handled, notice = await h._run_hit_guards(_Event(), "1", "556", "李四", "普通", [])
        self.assertTrue(handled)
        self.assertEqual((h.mutes, notice), ([300], None))

    def test_code_rules_and_blacklist_use_default(self):
        h = _H(config={"wxcode_regex_rules": ["pages/.*share"]})
        h._rg_save({"name": "邀请码", "code_rules": ["invite"], "action": {"mute": True, "mute_minutes": 1}})
        rule, action = h._hg_code("https://x/invite?a=1", "1")
        self.assertEqual((rule, action["group"]), ("invite", "邀请码"))
        rule, action = h._hg_code("https://x/pages/a/share", "1")
        self.assertEqual((rule, action["group"]), ("pages/.*share", "默认组"))
        h._code_block_set = lambda: {"BAD"}
        self.assertEqual(h._hg_code("BAD", "1"), ("已拉黑的码", None))
        self.assertEqual(h._rg_test(code="BAD")["code"]["rule"], "已拉黑的码")


class MiniAppTests(unittest.IsolatedAsyncioTestCase):
    def test_links_and_cards_are_recognised_and_merged(self):
        self.assertEqual(miniapp.extract_links("看这个 #小程序://刺猬云印/N937Lf7Xrcnhc3r 好用"),
                         [{"name": "刺猬云印", "path": "N937Lf7Xrcnhc3r", "raw": "#小程序://刺猬云印/N937Lf7Xrcnhc3r"}])
        self.assertEqual(miniapp.extract_links("#小程序:/坏的 http://x"), [])
        h = _H()
        # 链接先出现：还不知道是哪个小程序
        first = h._mp_items([], "#小程序://刺猬云印/AAA")
        self.assertEqual(first[0]["app_key"], "name:刺猬云印")
        h._mp_record("1", "u1", "甲", first)
        items = h._mp_items([_card()], "")
        self.assertEqual((items[0]["app_key"], items[0]["name"], items[0]["form"]),
                         ("wx9580c9208762a7c5", "刺猬云印丨打印5分 包邮到家", "card"))
        self.assertEqual(items[0]["content"], "https://m.q.qq.com/a/s/cfae5456842aa26ff8d16e8cf5eb42e7")
        h._mp_record("1", "u2", "乙", items)
        # 有了卡片之后，同名链接归到卡片的小程序
        later = h._mp_items([], "#小程序://刺猬云印/BBB")
        self.assertEqual((later[0]["app_key"], later[0]["name"]), ("wx9580c9208762a7c5", "刺猬云印丨打印5分 包邮到家"))
        # 普通分享卡片不算小程序
        news = card_parse.parse_card({"app": "com.tencent.structmsg", "view": "news",
                                      "meta": {"news": {"title": "新闻", "jumpUrl": "https://n"}}})
        self.assertEqual(h._mp_items([news], ""), [])

    def test_record_stats_detail_feed_and_screen(self):
        h = _H()
        now = int(time.time())
        card_items = h._mp_items([_card()], "")
        h._mp_record("1", "u1", "甲", card_items, now=now - 100)
        h._mp_record("2", "u1", "甲", h._mp_items([_card()], ""), outcome="rule", reason="小程序卡片", now=now - 50)
        h._mp_record("1", "u2", "乙", h._mp_items([], "#小程序://刺猬云印/XYZ"), now=now)
        h._mp_record("1", "u3", "丙", h._mp_items([], "#小程序://别的程序/Q1"), now=now)
        items, total = h._mp_stats(days=1)
        self.assertEqual(total, 2)
        top = items[0]
        self.assertEqual((top["app_key"], top["total"], top["cards"], top["links"], top["ruled"], top["users"],
                          top["groups"], top["contents"]), ("wx9580c9208762a7c5", 3, 2, 1, 1, 2, 2, 2))
        by_user, total = h._mp_stats(days=1, by="user")
        self.assertEqual(total, 3)
        self.assertEqual(h._mp_stats(days=1, keyword="别的")[1], 1)
        self.assertEqual(h._mp_stats(days=1, group_id="2")[1], 1)
        d = h._mp_detail("wx9580c9208762a7c5")
        self.assertEqual([c["total"] for c in d["contents"]], [2, 1])
        self.assertEqual({g["group_id"] for g in d["groups"]}, {"1", "2"})
        self.assertEqual(d["sends"][0]["user_id"], "u2")
        self.assertIn("刺猬云印", d["aliases"])
        feed = h._mp_feed()
        self.assertEqual(len(feed["items"]), 4)
        self.assertEqual(h._mp_feed(since_id=feed["last_id"])["items"], [])
        s = h._mp_screen(hours=24, now=now)
        self.assertEqual((s["total"], s["apps"], s["ruled"], s["top"][0]["total"]), (4, 2, 1, 3))

    def test_whitelist_and_block_state(self):
        h = _H()
        items = h._mp_items([_card()], "")
        self.assertFalse(h._mp_is_allowed(items[0]))
        self.assertEqual(h._mp_set_allowed("wx9580c9208762a7c5", True), "")
        self.assertTrue(h._mp_is_allowed(items[0]))
        items[0]["_allowed"] = True
        h._mp_record("1", "u1", "", items)
        self.assertEqual(h._mp_stats()[0][0]["allowed"], 1)
        self.assertEqual(h._mp_stats()[0][0]["state"], "allow")
        h._mp_set_allowed("wx9580c9208762a7c5", False)
        self.assertEqual(h._rg_add_app("wx9580c9208762a7c5"), "")
        self.assertEqual(h._mp_stats()[0][0]["state"], "block")
        self.assertEqual(h._rg_remove_app("wx9580c9208762a7c5"), "")
        self.assertEqual(h._mp_stats()[0][0]["state"], "")

    async def test_rule_group_app_entries_recall(self):
        h = _H()
        h._rg_save({"name": "小程序广告", "apps": ["刺猬云印"], "action": {"mute": True, "mute_minutes": 2}})
        link_items = h._mp_items([], "#小程序://刺猬云印/AAA")
        handled, _ = await h._run_hit_guards(_Event(), "1", "u1", "甲", "#小程序://刺猬云印/AAA", [], mp_items=link_items)
        self.assertTrue(handled)
        self.assertEqual(h.mutes, [120])
        self.assertIn("小程序链接", h.hits[-1][5])
        card_items = h._mp_items([_card()], "")
        self.assertEqual(h._rg_match_app(card_items[0], "1")[1], "刺猬云印")  # 名字包含
        h._rg_save(dict(h._rg_public(h._rg_list()[0]), apps=["wx9580c9208762a7c5"]))
        self.assertEqual(h._rg_match_app(card_items[0], "1")[1], "wx9580c9208762a7c5")  # appid
        h._mp_record("1", "u1", "甲", card_items)
        self.assertEqual(h._rg_test(app="wx9580c9208762a7c5")["app"]["rule"], "wx9580c9208762a7c5")
        h._rg_save(dict(h._rg_public(h._rg_list()[0]), apps=["刺猬云印"]))
        self.assertEqual(h._rg_test(app="wx9580c9208762a7c5")["app"]["rule"], "刺猬云印")  # 填 appid 也能对上名字
        card_items[0]["_allowed"] = True  # 白名单不按小程序条目撤回
        handled, _ = await h._run_hit_guards(_Event(), "1", "u1", "甲", "", [], mp_items=card_items)
        self.assertFalse(handled)

    async def test_title_conditions(self):
        """平台类小程序（微店买买）：同一个小程序里只拦部分标题。"""
        h = _H()
        weidian = card_parse.parse_card(WEIDIAN)
        other = dict(weidian, title="家用小风扇 包邮", prompt="[微信小程序]家用小风扇 包邮")
        h._rg_save({"name": "考研资料", "apps": ["微店买买 | 考研", r"* | re:差\d+人"],
                    "action": {"mute": True, "mute_minutes": 3}})
        hit_item = h._mp_items([weidian], "")[0]
        self.assertEqual(hit_item["app_key"], "wx4d1258677af59f5c")
        group, rule = h._rg_match_app(hit_item, "1")
        self.assertEqual((group["name"], rule), ("考研资料", "微店买买 | 考研"))
        self.assertEqual(h._rg_match_app(h._mp_items([other], "")[0], "1"), (None, None))  # 同一个小程序的别的商品放行
        # 任何小程序：标题含「差N人」
        print_card = _card()
        print_card = dict(print_card, title="【差2人】拼团打印")
        self.assertEqual(h._rg_match_app(h._mp_items([print_card], "")[0], "1")[1], r"* | re:差\d+人")
        # 状态：只拦部分标题 = partial；整个拉黑 = block
        self.assertEqual(h._rg_app_state(hit_item), "partial")
        self.assertEqual(h._mp_state("wx4d1258677af59f5c", "wx4d1258677af59f5c", "微店买买"), "partial")
        self.assertEqual(h._rg_app_state(h._mp_items([_card()], "")[0]), "")  # * 条目不算针对某个小程序
        h._rg_add_app("wx4d1258677af59f5c")
        self.assertEqual(h._rg_app_state(hit_item), "block")
        rules = h._mp_rules({"app_key": "wx4d1258677af59f5c", "appid": "wx4d1258677af59f5c", "name": "微店买买"})
        self.assertEqual([(r["entry"], r["title"]) for r in rules],
                         [("微店买买 | 考研", "考研"), ("wx4d1258677af59f5c", "")])
        # 撤回
        handled, _ = await h._run_hit_guards(_Event(), "1", "u1", "甲", "", [], mp_items=[hit_item])
        self.assertTrue(handled)
        # 测试框：只填标题 / 填小程序 + 标题
        self.assertEqual(h._rg_test(title="【差1人】考研")["app"]["rule"], r"* | re:差\d+人")
        self.assertEqual(h._rg_test(app="微店买买", title="27考研英语")["app"]["rule"], "微店买买 | 考研")
        # 规则写 appid、测试填名字：已记录过的小程序按名字对上
        h._mp_record("1", "u1", "甲", [hit_item])
        h._rg_save(dict(h._rg_public(h._rg_list()[0]), apps=["wx4d1258677af59f5c | 考研"]))
        self.assertEqual(h._rg_test(app="微店买买", title="27考研英语")["app"]["rule"], "wx4d1258677af59f5c | 考研")

    def test_title_condition_validation(self):
        h = _H()
        self.assertIn("竖线后面要写标题条件", h._rg_save({"name": "x", "apps": ["微店买买 |"]})[1])
        self.assertIn("小程序为空", h._rg_save({"name": "x", "apps": ["*"]})[1])
        self.assertIn("嵌套量词", h._rg_save({"name": "x", "apps": ["微店买买 | re:(a+)+"]})[1])
        g, err, _ = h._rg_save({"name": "x", "apps": ["微店买买 ｜ 考研"]})  # 全角竖线也行
        self.assertEqual(err, "")
        self.assertEqual(h._rg_app_entries({"name": "微店买买"})[0]["title"], "考研")

    def test_clean_old_sends(self):
        h = _H(cfg={"miniapp_store_days": 1})
        h._mp_record("1", "u", "", h._mp_items([], "#小程序://a/1"), now=int(time.time()) - 3 * 86400)
        self.assertEqual(h._mp_clean(), 1)
        h._mp_record("1", "u", "", h._mp_items([], "#小程序://a/2"))
        self.assertEqual(len(h._mp_feed()["items"]), 1)
        self.assertEqual(h._mp_stats()[0][0]["name"], "a")  # 小程序本身保留


if __name__ == "__main__":
    unittest.main()
