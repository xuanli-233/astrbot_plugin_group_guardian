"""群成员查找（member_lookup.py）：按 QQ 号 / 昵称 / 群名片搜人，汇总一个人的档案。"""
import asyncio
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_minimal_web as T  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(f"{T.PKG}.member_lookup", os.path.join(ROOT, "member_lookup.py"))
member_lookup = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = member_lookup
_spec.loader.exec_module(member_lookup)


class _H(member_lookup.MemberLookupMixin, T._Harness):
    pass


GROUPS = {
    "700000001": {"name": "一群", "members": {"20001": "member", "20002": "admin", "30001": "member"}},
    "700000002": {"name": "二群", "members": {"20001": "owner", "30002": "member"}},
}


def _harness():
    T.REQUEST.args = {}
    c = T._Client({g: dict(v) for g, v in GROUPS.items()})
    h = _H(c)

    async def fill():
        await h._dir_groups(c, force=True)
        for g in GROUPS:
            await h._dir_members(c, g, force=True)
    asyncio.run(fill())
    members = h._dir()["members"]
    members["700000001"]["20001"]["card"] = "张三"
    members["700000002"]["20001"]["nickname"] = "老张"
    members["700000001"]["30001"]["card"] = "广告号A"
    return h


class SearchTests(unittest.TestCase):
    def test_search_by_uid_prefix_merges_groups(self):
        h = _harness()
        r = h._mbr_search("2000")
        ids = [it["user_id"] for it in r["items"]]
        self.assertEqual(sorted(ids), ["20001", "20002"])
        zs = [it for it in r["items"] if it["user_id"] == "20001"][0]
        self.assertEqual(zs["groups"], 2)
        self.assertIn("owner", zs["roles"])

    def test_exact_uid_first(self):
        h = _harness()
        r = h._mbr_search("20001")
        self.assertEqual(r["items"][0]["user_id"], "20001")
        self.assertTrue(r["items"][0]["exact"])

    def test_search_by_card_or_nickname(self):
        h = _harness()
        self.assertEqual([it["user_id"] for it in h._mbr_search("广告")["items"]], ["30001"])
        self.assertEqual([it["user_id"] for it in h._mbr_search("老张")["items"]], ["20001"])
        self.assertEqual(h._mbr_search("没有这个人")["total"], 0)


class ProfileTests(unittest.TestCase):
    def test_profile_lists_groups(self):
        h = _harness()
        p = h._mbr_profile("20001")
        self.assertEqual(p["user_id"], "20001")
        self.assertEqual(sorted(g["group_id"] for g in p["groups"]), ["700000001", "700000002"])
        roles = {g["group_id"]: g["role"] for g in p["groups"]}
        self.assertEqual(roles["700000002"], "owner")

    def test_profile_unknown_member_is_empty_not_error(self):
        h = _harness()
        p = h._mbr_profile("99999")
        self.assertEqual(p["groups"], [])

    def test_web_routes(self):
        h = _harness()
        T.REQUEST.args = {"q": "1"}
        res = asyncio.run(h._web_minimal_member_search())
        self.assertEqual(res.obj["status"], "success")  # 单个数字按 QQ 号前缀查
        T.REQUEST.args = {"q": "张"}
        self.assertEqual(asyncio.run(h._web_minimal_member_search()).obj["status"], "error")
        T.REQUEST.args = {"user_id": "abc"}
        self.assertEqual(asyncio.run(h._web_minimal_member_profile()).obj["status"], "error")
        T.REQUEST.args = {"user_id": "20001"}
        self.assertEqual(asyncio.run(h._web_minimal_member_profile()).obj["status"], "success")


if __name__ == "__main__":
    unittest.main()
