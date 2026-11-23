"""重复成员页：发过链接 / 小程序 / 码的人（duplicate_members._dup_activity），筛选、排序、发送记录接口。"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_minimal_web as T  # noqa: E402


class _H(T._Harness):
    def __init__(self, client):
        super().__init__(client)
        self.queries = []

    def _lk_init_db(self):
        with self._storage._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS lk_sends (ts INTEGER, user_id TEXT, outcome TEXT)")

    def _mp_init_db(self):
        with self._storage._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mp_sends (ts INTEGER, user_id TEXT, outcome TEXT)")

    def _cl_init_db(self):
        with self._storage._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS code_user_stats (user_id TEXT, total INTEGER, ruled INTEGER, "
                         "limited INTEGER, last_ts INTEGER)")

    def _dup_act_query(self, uids=None):
        self.queries.append(None if uids is None else sorted(uids))
        return super()._dup_act_query(uids)

    # 浮窗接口用到的三个查询
    def _lk_user(self, uid, limit=30):
        return {"store_days": 30, "domains": [], "recent": [{"uid": uid}]}

    def _mp_user(self, uid, limit=30):
        raise RuntimeError("坏了")

    def _cu_user_detail(self, uid, limit=500):
        return {"codes": [], "code_count": 0, "total": 0}


def _groups():
    g = T._groups()
    for gid in ("10001", "10002"):
        g[gid]["members"].update({"30002": "member", "30003": "member"})
    return g


class DupActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = T._Client(_groups())
        self.h = _H(self.client)
        await self.h._dir_groups(self.client, force=True)
        for gid in ("10001", "10002"):
            await self.h._dir_members(self.client, gid, force=True)
        now = int(time.time())
        for fn in (self.h._lk_init_db, self.h._mp_init_db, self.h._cl_init_db):
            fn()
        with self.h._storage._connect() as conn:
            conn.executemany("INSERT INTO lk_sends VALUES(?,?,?)", [(now, "30001", "pass"), (now, "30001", "rule"),
                                                                     (now, "99999", "pass")])
            conn.execute("INSERT INTO mp_sends VALUES(?,?,?)", (now - 5, "30002", "pass"))
            conn.execute("INSERT INTO code_user_stats VALUES(?,?,?,?,?)", ("30002", 3, 0, 0, now - 9))
            conn.commit()

    def test_page_only_queries_shown_people(self):
        h = self.h
        d = h._dup_page()
        act = {it["user_id"]: it["activity"] for it in d["items"]}
        self.assertEqual((act["30001"]["link"], act["30001"]["link_bad"], act["30001"]["bad"]), (2, 1, 1))
        self.assertEqual((act["30002"]["miniapp"], act["30002"]["code"], act["30002"]["total"]), (1, 3, 4))
        self.assertIsNone(act["30003"])
        self.assertEqual(h.queries, [["30001", "30002", "30003"]])       # 只查本页的人，不整表汇总
        self.assertIsNone(d["sent_people"])
        self.assertNotIn("activity", h._dup_all()[0])                    # 缓存里的条目不被改

    def test_filter_sort_and_cache(self):
        h = self.h
        d = h._dup_page(sent="any")
        self.assertEqual({it["user_id"] for it in d["items"]}, {"30001", "30002"})
        self.assertEqual(d["sent_people"], 2)
        self.assertEqual([it["user_id"] for it in h._dup_page(sent="bad")["items"]], ["30001"])
        self.assertEqual([it["user_id"] for it in h._dup_page(sent="code")["items"]], ["30002"])
        self.assertEqual([it["user_id"] for it in h._dup_page(sort="sends")["items"]], ["30001", "30002", "30003"])
        self.assertEqual(h.queries, [None])                              # 整表汇总一次，60 秒内复用
        h._dup_page()
        self.assertEqual(h.queries, [None])                              # 翻页也用缓存
        self.assertEqual(h._dup_matching_ids(sent="bad"), ["30001"])     # 按条件批量处理跟着筛选

    async def test_sends_endpoint(self):
        T.REQUEST.args = {"user_id": "30001"}
        out = (await T.call(self.h._web_minimal_member_sends))["data"]
        self.assertEqual(out["links"]["recent"], [{"uid": "30001"}])
        self.assertIsNone(out["miniapps"])                               # 一张表出错不影响其它
        self.assertEqual(out["codes"]["code_count"], 0)
        T.REQUEST.args = {"user_id": "abc"}
        self.assertEqual((await T.call(self.h._web_minimal_member_sends))["status"], "error")


if __name__ == "__main__":
    unittest.main()
