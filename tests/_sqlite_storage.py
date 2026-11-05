"""测试用：给替身 storage 提供与 SQLiteStorage._connect 相同用法的临时 SQLite 连接（批量任务、操作记录建表用）。"""
import contextlib
import sqlite3
import tempfile
from pathlib import Path


class SqliteConnectMixin:
    def _connect(self):
        if not hasattr(self, "_sqlite_tmp"):
            self._sqlite_tmp = tempfile.TemporaryDirectory()
            self._sqlite_path = Path(self._sqlite_tmp.name) / "test.db"

        @contextlib.contextmanager
        def cm():
            conn = sqlite3.connect(str(self._sqlite_path))
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()
        return cm()
