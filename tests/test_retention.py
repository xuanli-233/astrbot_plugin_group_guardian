"""保留时长改按小时（retention.py）：旧的天数换算一次、范围收紧、各模块按小时清理。"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import link_guard  # noqa: E402
import retention  # noqa: E402
import storage_admin  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402


class _Storage(SqliteConnectMixin):
    def __init__(self):
        self.meta = {}

    def get_meta(self, k, d=""):
        return self.meta.get(k, d)

    def set_meta(self, k, v):
        self.meta[k] = v


class _H(retention.RetentionMixin, link_guard.LinkGuardMixin, storage_admin.StorageAdminMixin):
    def __init__(self, config):
        self.config = dict(config)
        self._storage = _Storage()
        self.saved = 0

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.config.get(key, default))

    def _cfg(self, key, default=True, group_id=None):
        return self.config.get(key, default)

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.config.get(key, default))

    def _save_config_safe(self):
        self.saved += 1
        return True


class RetentionTests(unittest.TestCase):
    def test_migrate_days_to_hours_once(self):
        h = _H({"link_store_days": 30, "miniapp_store_days": 90, "log_image_days": 7, "leave_log_image_days": 1,
                "code_stats_keep_days": 0, "link_store_hours": 2160})
        self.assertTrue(h._ret_migrate())
        self.assertEqual((h.config["link_store_hours"], h.config["miniapp_store_hours"], h.config["log_image_hours"],
                          h.config["leave_log_image_hours"], h.config["code_stats_keep_hours"]), (720, 2160, 168, 24, 0))
        self.assertEqual(h.saved, 1)
        h.config["link_store_hours"] = 36          # 之后在页面改成 36 小时：不会再被旧天数覆盖
        self.assertFalse(h._ret_migrate())
        self.assertEqual(h._ret_hours("link_store_hours"), 36)

    def test_clamp_and_defaults(self):
        h = _H({"link_store_hours": 0, "code_stats_keep_hours": 0, "miniapp_store_hours": 10 ** 9})
        self.assertEqual(h._ret_hours("link_store_hours"), 1)         # 最少 1 小时
        self.assertEqual(h._ret_hours("code_stats_keep_hours"), 0)    # 0 = 永久
        self.assertEqual(h._ret_hours("miniapp_store_hours"), 87600)
        self.assertEqual(h._ret_hours("log_image_hours"), 720)        # 没配置：默认 30 天

    def test_link_cleanup_uses_hours(self):
        h = _H({"link_store_hours": 36})
        self.assertEqual((h._lk_store_hours(), h._lk_store_days()), (36, 2))   # 不足一天按一天给统计范围用
        now = int(time.time())
        item = link_guard.normalize_url("https://a.com/x")
        item.update(url_hash=link_guard._h(item["key"]), state="")
        h._lk_record("1", "9", "", [dict(item)], now=now - 40 * 3600)
        h._lk_record("1", "9", "", [dict(item)], now=now - 30 * 3600)
        h._lk_clean(now)
        with h._storage._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lk_sends").fetchone()[0], 1)
        self.assertEqual(h._sa_retention("links"), "自动保留 36 小时")
        h.config["link_store_hours"] = 48
        self.assertEqual(h._sa_retention("links"), "自动保留 2 天")


if __name__ == "__main__":
    unittest.main()
