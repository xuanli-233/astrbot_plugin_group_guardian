"""SQLite schema migration tests for scheduled unban retries."""

import importlib.util
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_storage():
    created = []
    if "astrbot" not in sys.modules:
        sys.modules["astrbot"] = types.ModuleType("astrbot")
        created.append("astrbot")
    if "astrbot.api" not in sys.modules:
        sys.modules["astrbot.api"] = types.ModuleType("astrbot.api")
        created.append("astrbot.api")
    astrbot = sys.modules["astrbot"]
    api = sys.modules["astrbot.api"]
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
        )
    astrbot.api = api
    spec = importlib.util.spec_from_file_location(
        "group_guardian_storage_migration_tests", ROOT / "storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in reversed(created):
        sys.modules.pop(name, None)
    return module


storage_module = _load_storage()


class StorageMigrationTests(unittest.TestCase):
    @staticmethod
    def _new_store(temp_dir):
        store = storage_module.SQLiteStorage(Path(temp_dir), str(ROOT))
        with store._connect() as conn:
            storage_module.SQLiteStorage._create_tables(conn)
        return store

    def test_old_scheduled_unbans_table_is_migrated_and_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = storage_module.SQLiteStorage(Path(temp_dir), str(ROOT))
            with sqlite3.connect(store.db_path) as conn:
                conn.execute(
                    "CREATE TABLE scheduled_unbans ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "group_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                    "unban_at INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                    "UNIQUE(group_id, user_id))"
                )
                conn.execute(
                    "INSERT INTO scheduled_unbans(group_id, user_id, unban_at, created_at) "
                    "VALUES('123', '456', 900, 800)"
                )

            with store._connect() as conn:
                storage_module.SQLiteStorage._create_tables(conn)
                columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(scheduled_unbans)"
                    ).fetchall()
                }

            self.assertTrue(
                {"retry_count", "next_retry_at", "last_error"}.issubset(columns)
            )
            due = store.list_due_unbans(1000)
            self.assertEqual(1, len(due))
            self.assertEqual(0, due[0]["retry_count"])
            self.assertEqual(0, due[0]["next_retry_at"])

            self.assertTrue(
                store.mark_scheduled_unban_retry(due[0]["id"], 1050, "temporary")
            )
            self.assertEqual([], store.list_due_unbans(1049))
            retried = store.list_due_unbans(1050)
            self.assertEqual(1, retried[0]["retry_count"])
            self.assertEqual("temporary", retried[0]["last_error"])

if __name__ == "__main__":
    unittest.main()
