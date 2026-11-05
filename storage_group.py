# -*- coding: utf-8 -*-
"""群级配置、名片审计与保护名单的 SQLite repository mixin。"""

from typing import Dict, List, Optional


class GroupStorageMixin:
    """依赖宿主提供 ``_connect()``，保持 ``SQLiteStorage`` 公共 API 不变。"""

    def get_group_config(self, group_id: str, key: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM group_configs WHERE group_id=? AND key=?",
                (str(group_id), str(key)),
            ).fetchone()
        return row["value"] if row else None

    def get_group_configs(self, group_id: str) -> Dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM group_configs WHERE group_id=?",
                (str(group_id),),
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_group_config(self, group_id: str, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO group_configs(group_id, key, value) "
                "VALUES(?, ?, ?)",
                (str(group_id), str(key), str(value)),
            )
            conn.commit()

    def delete_group_config(self, group_id: str, key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM group_configs WHERE group_id=? AND key=?",
                (str(group_id), str(key)),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def clear_group_configs(self, group_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM group_configs WHERE group_id=?", (str(group_id),)
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def list_configured_groups(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT group_id FROM group_configs ORDER BY group_id"
            ).fetchall()
        return [row["group_id"] for row in rows]
