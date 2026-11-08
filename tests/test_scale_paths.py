"""大群规模下的路径：重复成员缓存 / 分页 / 按条件处理、成员缓存只写变化的行、批量任务明细分页。"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_minimal_web as T  # noqa: E402


def _groups(n_groups=4, size=60, pool=150):
    import random
    random.seed(3)
    groups = {}
    for i in range(n_groups):
        gid = str(800000000 + i)
        members = {str(u): "member" for u in random.sample(range(1000, 1000 + pool), size)}
        members[str(T.BOT)] = "admin"
        groups[gid] = {"name": f"群{i}", "members": members}
    return groups


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        T.REQUEST.args = {}
        self.groups = _groups()
        self.client = T._Client(self.groups)
        self.h = T._Harness(self.client)
        await self.h._dir_groups(self.client, force=True)
        for gid in self.groups:
            await self.h._dir_members(self.client, gid, force=True)


class DuplicateTests(_Base):
    async def test_cache_reused_and_invalidated(self):
        calls = []
        real = self.h._dup_find_uncached

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        self.h._dup_find_uncached = counting
        first = self.h._dup_all()
        self.h._dup_all()
        self.h._dup_page(page=2, page_size=10)
        self.assertEqual(len(calls), 1)  # 翻页、再查都用缓存
        uid = first[0]["user_id"]
        gid = first[0]["groups"][0]["group_id"]
        self.h._dir_remove_member(gid, uid)  # 退群通知
        after = {it["user_id"]: it for it in self.h._dup_all()}
        self.assertEqual(len(calls), 2)
        self.assertNotIn(gid, [g["group_id"] for g in after.get(uid, {"groups": []})["groups"]])
        self.h._dup_set_ignored(first[1]["user_id"], True, reason="x")
        self.h._dup_all()
        self.assertEqual(len(calls), 3)

    async def test_page_and_by_count(self):
        everyone = self.h._dup_find()["items"]
        page = self.h._dup_page(page=2, page_size=10)
        self.assertEqual(page["total"], len(everyone))
        self.assertEqual([i["user_id"] for i in page["items"]], [i["user_id"] for i in everyone[10:20]])
        self.assertEqual(sum(b["people"] for b in page["by_count"]), len(everyone))
        three = self.h._dup_page(min_groups=3)
        self.assertTrue(all(i["count"] >= 3 for i in three["items"]))
        self.assertEqual(self.h._dup_page(page=999, page_size=10)["page"], page["pages"])
        by_uid = self.h._dup_page(sort="uid", page_size=200)["items"]
        self.assertEqual([i["user_id"] for i in by_uid], sorted((i["user_id"] for i in by_uid), key=int))

    async def test_endpoint_pages_and_handle_all_matching(self):
        T.REQUEST.args = {"page": "1", "page_size": "10"}
        out = await T.call(self.h._web_minimal_duplicates)
        self.assertEqual(len(out["data"]["items"]), 10)
        total = out["data"]["total"]
        T.REQUEST.args = {}
        ids = self.h._dup_matching_ids(min_groups=3)
        bad = await T.call(self.h._web_minimal_duplicates_handle, {
            "all_matching": True, "filter": {"min_groups": 3}, "expect_count": len(ids) + 1,
            "mode": "keep_one", "reason": "重复加群"})
        self.assertEqual(bad["status"], "error")
        self.assertIn("人数已变化", bad["message"])
        ok = await T.call(self.h._web_minimal_duplicates_handle, {
            "all_matching": True, "filter": {"min_groups": 3}, "expect_count": len(ids),
            "mode": "keep_one", "reason": "重复加群"})
        self.assertEqual(ok["status"], "success")
        self.assertEqual(ok["data"]["summary"]["people"], len(ids))
        self.assertGreater(ok["data"]["job"]["items"], 0)
        self.assertGreater(total, len(ids))  # 只处理 ≥3 个群的人


class DirectoryWriteTests(_Base):
    async def test_incremental_member_write_matches_full_rewrite(self):
        gid = next(iter(self.groups))
        members = self.groups[gid]["members"]
        gone = [u for u in members if u != str(T.BOT)][:5]
        for u in gone:
            members.pop(u)
        members["99999"] = "member"
        writes = []
        real = self.h._dir_save_members

        def spy(g, m, now, old=None):
            writes.append(old is not None)
            return real(g, m, now, old)
        self.h._dir_save_members = spy
        await self.h._dir_members(self.client, gid, force=True)
        self.assertEqual(writes, [True])  # 只写变化
        with self.h._storage._connect() as conn:
            rows = {r["user_id"] for r in conn.execute("SELECT user_id FROM dir_members WHERE group_id=?", (gid,))}
        self.assertEqual(rows, set(members))
        # 重启后从库里载入，与内存一致
        before = {u: dict(m) for u, m in self.h._dir()["members"][gid].items()}
        self.h._dir_state = None
        self.assertEqual({u: dict(m) for u, m in self.h._dir()["members"][gid].items()}, before)


class BatchPageTests(_Base):
    async def test_job_page_follows_current_item_and_filters(self):
        items = [{"group_id": "800000000", "action": "kick", "target": str(1000 + i), "label": "踢出"} for i in range(250)]
        job = self.h._bq_create_job("测试", "kick", items, mode="manual")
        with self.h._storage._connect() as conn:
            conn.execute("UPDATE batch_items SET status='ok' WHERE job_id=? AND seq<=130", (job["id"],))
            conn.execute("UPDATE batch_items SET status='failed' WHERE job_id=? AND seq IN (5, 7)", (job["id"],))
            conn.commit()
        page = self.h._bq_job_page(job["id"], page_size=100)
        self.assertEqual((page["page"], page["pages"], len(page["items"])), (2, 3, 100))
        self.assertEqual(page["current_id"], next(i["id"] for i in page["items"] if i["seq"] == 131))
        self.assertNotIn("params", page["items"][0])
        failed = self.h._bq_job_page(job["id"], status="failed")
        self.assertEqual(([i["seq"] for i in failed["items"]], failed["filtered_total"]), ([5, 7], 2))
        self.assertEqual(page["counts"], {"ok": 128, "failed": 2, "pending": 120})
        T.REQUEST.args = {"id": str(job["id"]), "page": "3", "page_size": "100"}
        out = await T.call(self.h._web_minimal_batch_job)
        self.assertEqual([i["seq"] for i in out["data"]["job"]["items"]][:1], [201])


if __name__ == "__main__":
    unittest.main()
