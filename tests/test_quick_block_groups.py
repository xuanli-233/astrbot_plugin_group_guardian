"""快捷拉黑选规则组（小程序 / 码）+ 转发群按规则组订阅。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_rule_groups_miniapp as RG  # noqa: E402  （先装好 astrbot 桩）
import backup_routes  # noqa: E402
import code_board  # noqa: E402
import test_backup_routes as BR  # noqa: E402


class _Q(code_board.CodeBoardMixin, RG._H):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.backups = []

    async def _backup_message(self, *a, **k):
        self.backups.append(k)
        return True


class QuickBlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = _Q()
        self.g, err, _ = self.h._rg_save({"name": "广告", "scope": ["2"], "action": {"mute": True, "mute_minutes": 5}})
        self.assertEqual(err, "")

    def test_app_goes_to_chosen_group_and_moves(self):
        h = self.h
        self.assertEqual(h._rg_add_app("微店买买", group_id=self.g["id"]), "")
        self.assertEqual(h._rg_get(self.g["id"])["apps"], ["微店买买"])
        item = {"appid": "wx1", "name": "微店买买"}
        self.assertEqual(h._rg_match_app(item, "2")[0]["name"], "广告")
        self.assertIsNone(h._rg_match_app(item, "1")[0])               # 只在组的生效范围里拦
        self.assertEqual(h._rg_add_app("微店买买"), "")                 # 再拉黑到默认组：从「广告」移过去
        self.assertEqual(h._rg_get(self.g["id"])["apps"], [])
        self.assertEqual(h._rg_default()["apps"], ["微店买买"])
        self.assertIn("不存在", h._rg_add_app("x", group_id=9999))

    def test_code_goes_to_chosen_group(self):
        h = self.h
        self.assertEqual(h._code_set_in_list("block", "CODE1", True, rule_group=self.g["id"]), "")
        self.assertEqual(h._code_block_list(), [])                      # 不进全局码黑名单
        self.assertIn("CODE1", h._code_block_set())                     # 但状态标成已拉黑
        rule, action = h._hg_code("CODE1", "2")
        self.assertEqual((rule, action["group"], action["group_id"]), ("已拉黑的码", "广告", self.g["id"]))
        self.assertEqual(h._hg_code("CODE1", "1"), (None, None))        # 不在范围的群放行
        self.assertEqual(h._code_rule_hit("CODE1"), "已拉黑的码")         # 看板 / 测试里照样算命中
        ent = h._code_block_entries()
        self.assertEqual([(e["text"], e["group"]) for e in ent], [("CODE1", "广告")])

        # 改拉黑到默认组：进码黑名单，从组里移走
        self.assertEqual(h._code_set_in_list("block", "CODE1", True), "")
        self.assertEqual(h._code_block_list(), ["CODE1"])
        self.assertEqual(h._rg_get(self.g["id"])["codes"], [])
        self.assertEqual(h._hg_code("CODE1", "1"), ("已拉黑的码", None))

        # 再拉黑回组：从码黑名单移走；加白名单 / 解除拉黑都从组里移走
        h._code_set_in_list("block", "CODE1", True, rule_group=self.g["id"])
        self.assertEqual(h._code_block_list(), [])
        h._code_set_in_list("allow", "CODE1", True)
        self.assertNotIn("CODE1", h._code_block_set())
        h._code_set_in_list("block", "CODE2", True, rule_group=self.g["id"])
        h._code_set_in_list("block", "CODE2", False)
        self.assertEqual(h._code_block_entries(), [])

    def test_rule_group_save_keeps_codes(self):
        h = self.h
        h._code_set_in_list("block", "CODE1", True, rule_group=self.g["id"])
        data = h._rg_public(h._rg_get(self.g["id"]))
        data["keywords"] = ["加微信"]
        _saved, err, _ = h._rg_save(data)
        self.assertEqual(err, "")
        self.assertEqual(h._rg_get(self.g["id"])["codes"], ["CODE1"])  # 编辑规则组不会丢掉拉黑的码

    async def test_hit_passes_rule_group_to_backup(self):
        h = self.h
        rule, action = h._hg_code("CODE1", "2")
        h._code_set_in_list("block", "CODE1", True, rule_group=self.g["id"])
        rule, action = h._hg_code("CODE1", "2")
        await h._execute_hit_action(RG._Event(), "2", "9", "张三", "x", rule, kind="wxcode", action=action)
        self.assertEqual(h.backups[-1].get("rule_group"), self.g["id"])
        await h._execute_hit_action(RG._Event(), "2", "9", "张三", "x", "码发送超限", kind="code_limit")
        self.assertNotIn("rule_group", h.backups[-1])                    # 码超限不属于规则组

    def test_choices(self):
        names = [c["name"] for c in self.h._rg_choices()]
        self.assertEqual(names, ["广告", "默认组"])


class RouteSubscribeTests(unittest.TestCase):
    def test_parse_format_and_filter(self):
        r = backup_routes.parse_route("500:keyword;rg=3,1,x")
        self.assertEqual(r, {"group_id": "500", "kinds": ["keyword"], "rule_groups": [1, 3]})
        self.assertEqual(backup_routes.format_route(r), "500:keyword;rg=1,3")
        self.assertEqual(backup_routes.parse_route({"group_id": 6, "kinds": ["wxcode"], "rule_groups": ["2"]})["rule_groups"], [2])
        h = BR._H(backup_routes=["500:keyword;rg=1,3", "600:keyword"])
        self.assertEqual(h._br_targets("keyword"), ["500", "600"])       # 不属于规则组：不筛选
        self.assertEqual(h._br_targets("keyword", 3), ["500", "600"])
        self.assertEqual(h._br_targets("keyword", 2), ["600"])           # 500 没订阅规则组 2
        self.assertEqual(h._backup_targets("keyword", "100", 2), [600])

    def test_merge_and_save(self):
        h = BR._H(backup_routes=["500:keyword;rg=1", "500:wxcode;rg=2"])
        self.assertEqual(h._br_routes()[0]["rule_groups"], [1, 2])
        h = BR._H(backup_routes=["500:keyword;rg=1", "500:wxcode"])
        self.assertEqual(h._br_routes()[0]["rule_groups"], [])           # 一行不筛选 = 收全部
        routes, err = h._br_save([{"group_id": "500", "kinds": ["keyword"], "rule_groups": [4]}])
        self.assertEqual(err, "")
        self.assertEqual(h.config["backup_routes"], ["500:keyword;rg=4"])
        self.assertIn("规则组", h.ops[-1][1]["detail"])


if __name__ == "__main__":
    unittest.main()
