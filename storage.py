# -*- coding: utf-8 -*-
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from astrbot.api import logger

try:
    from .storage_group import GroupStorageMixin
except ImportError:  # 允许直接运行 storage.py 的离线迁移/测试脚本
    from storage_group import GroupStorageMixin


class SQLiteStorage(GroupStorageMixin):
    # 持久化层统一使用 SQLite。_connect() 是 contextmanager，进入时创建连接并开启 WAL，退出时自动关闭。
    # 审核日志按 message_id + group_id + user_id + time 组合键去重。
    def __init__(self, data_dir: Path, plugin_dir: str):
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.db_path = self.data_dir / "group_guardian.db"
        self.legacy_logs_path = self.data_dir / "moderation_logs.json"

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._create_tables(conn)
        # 旧版本留在数据库里的词库 / 规则 / 申诉 / 名片 / 学习表不再创建也不读取，原样保留。

    @staticmethod
    def _positive_ints(values: Iterable[int]) -> List[int]:
        ids: List[int] = []
        for value in values or []:
            try:
                item = int(value)
            except (TypeError, ValueError):
                continue
            if item > 0:
                ids.append(item)
        return ids

    @staticmethod
    def _non_empty_strings(values: Iterable[object]) -> List[str]:
        items: List[str] = []
        seen = set()
        for value in values or []:
            item = str(value).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
        return items

    @contextmanager
    def _connect(self):
        # 使用 contextmanager 确保连接在退出 with 块时总是通过 finally 关闭，防止泄漏。
        # timeout=5：数据库被外部锁定时最多等 5 秒抛异常，避免永久阻塞事件循环（扫描#38 S5）
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # synchronous 是连接级设置、不随 WAL 持久化到库文件；_create_tables 里那次设置
        # 只对建表连接生效，之后每个 _connect() 都会回落到默认 FULL。这里每连接显式设为
        # NORMAL，配合已持久化的 WAL 模式，减少高频审核日志写入的 fsync 开销。
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
        # 表名/列名只允许字母数字下划线，防止将来动态传入时被注入
        import re as _re
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
            raise ValueError(f"非法表名或列名: {table}.{column}")
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _create_tables(conn) -> None:
        # WAL 模式提升并发读性能，NORMAL 同步策略在 crash 后仍可恢复。
        # meta（键值对）、moderation_logs（处理日志）以及入群/解禁/权限/名单/群配置等表。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moderation_logs ("
            "id INTEGER PRIMARY KEY, "
            "time TEXT, "
            "ts INTEGER, "
            "group_id TEXT, "
            "user_id TEXT, "
            "user_name TEXT, "
            "msg_text TEXT, "
            "msg_preview TEXT, "
            "action TEXT, "
            "reason TEXT, "
            "image_urls TEXT"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON moderation_logs(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_group ON moderation_logs(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON moderation_logs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_action ON moderation_logs(action)")
        # ===== v2.4.0 新增表 =====
        # F1 入群审核规则（按群，group_id='default' 为全局兜底）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS join_audit_rules ("
            "group_id TEXT PRIMARY KEY, "
            "accept_keywords TEXT, "
            "reject_keywords TEXT, "
            "default_action TEXT, "
            "reject_reason TEXT, "
            "enabled INTEGER NOT NULL DEFAULT 1"
            ")"
        )
        # F3 定时解禁计划
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_unbans ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "unban_at INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "retry_count INTEGER NOT NULL DEFAULT 0, "
            "next_retry_at INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT DEFAULT '', "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        SQLiteStorage._ensure_column(
            conn, "scheduled_unbans", "retry_count", "INTEGER NOT NULL DEFAULT 0"
        )
        SQLiteStorage._ensure_column(
            conn, "scheduled_unbans", "next_retry_at", "INTEGER NOT NULL DEFAULT 0"
        )
        SQLiteStorage._ensure_column(
            conn, "scheduled_unbans", "last_error", "TEXT DEFAULT ''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unban_at ON scheduled_unbans(unban_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_unban_retry ON scheduled_unbans(next_retry_at, unban_at)"
        )
        # F5 群管理员动态授权（按群）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_admin_grant ("
            "group_id TEXT PRIMARY KEY, "
            "grant_owner INTEGER NOT NULL DEFAULT 1, "
            "grant_admin INTEGER NOT NULL DEFAULT 1, "
            "enabled INTEGER NOT NULL DEFAULT 1"
            ")"
        )
        # 配置迁移：单群管理类名单（群白/群黑/用户黑/用户白/管理员）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS managed_lists ("
            "list_type TEXT NOT NULL, "
            "value TEXT NOT NULL, "
            "UNIQUE(list_type, value)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_managed_lists_type ON managed_lists(list_type)")
        # F5 增强：群超管（某群专属的插件管理员，仅在该群生效，WebUI 单独设置）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_super_admins ("
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_super_admins_group ON group_super_admins(group_id)")
        # F5 增强：群级 bot 权限黑名单（群主可移除本群某群管的 bot 管理权限，优先级最高）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_admin_block ("
            "group_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "UNIQUE(group_id, user_id)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_block_group ON group_admin_block(group_id)")
        # 多群独立配置：每个群对任意配置项的覆盖值（value 存字符串，读取时按类型解析）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_configs ("
            "group_id TEXT NOT NULL, "
            "key TEXT NOT NULL, "
            "value TEXT, "
            "UNIQUE(group_id, key)"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_group_configs_group ON group_configs(group_id)")
        conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        # 从 meta 表读取键值对，不存在则返回 default。
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        # 向 meta 表写入键值对，已存在则覆盖（INSERT OR REPLACE）。
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
            conn.commit()

    def count_logs(self) -> int:
        # 返回审核日志表总条数。
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM moderation_logs").fetchone()
        return int(row["c"] or 0)

    def count_legacy_logs(self) -> int:
        # 统计旧的 moderation_logs.json 中的日志条数（迁移前）。
        if not self.legacy_logs_path.exists():
            return 0
        try:
            with open(self.legacy_logs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 0
        except Exception:
            return 0

    def migration_status(self) -> dict:
        # 返回完整的迁移状态信息，供 WebUI 迁移面板展示。
        return {
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
            "db_log_count": self.count_logs(),
            "legacy_logs_path": str(self.legacy_logs_path),
            "legacy_logs_exists": self.legacy_logs_path.exists(),
            "legacy_log_count": self.count_legacy_logs(),
        }

    @staticmethod
    def _log_to_row(log: dict) -> tuple:
        return (
            int(log.get("id", 0)),
            log.get("time", ""),
            int(log.get("ts", 0) or 0),
            str(log.get("group_id", "")),
            str(log.get("user_id", "")),
            str(log.get("user_name", "")),
            str(log.get("msg_text", "")),
            str(log.get("msg_preview", "")),
            str(log.get("action", "")),
            str(log.get("reason", "")),
            json.dumps(log.get("image_urls", []) or [], ensure_ascii=False),
        )

    @staticmethod
    def _row_to_log(row) -> dict:
        try:
            image_urls = json.loads(row["image_urls"] or "[]")
            if not isinstance(image_urls, list):
                image_urls = []
        except Exception:
            image_urls = []
        return {
            "id": row["id"],
            "time": row["time"] or "",
            "ts": row["ts"] or 0,
            "group_id": row["group_id"] or "",
            "user_id": row["user_id"] or "",
            "user_name": row["user_name"] or "",
            "msg_text": row["msg_text"] or "",
            "msg_preview": row["msg_preview"] or "",
            "action": row["action"] or "",
            "reason": row["reason"] or "",
            "image_urls": image_urls,
        }

    def add_log(self, log: dict) -> None:
        # INSERT OR REPLACE 按 id 主键持久化一条审核日志到 SQLite。
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO moderation_logs("
                "id, time, ts, group_id, user_id, user_name, msg_text, msg_preview, action, reason, image_urls"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._log_to_row(log),
            )
            conn.commit()

    def import_logs(self, logs: Iterable[dict]) -> int:
        # 批量导入 dict 格式的日志到 SQLite（INSERT OR IGNORE 按 id 去重）。
        imported = 0
        with self._connect() as conn:
            for log in logs:
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO moderation_logs("
                        "id, time, ts, group_id, user_id, user_name, msg_text, msg_preview, action, reason, image_urls"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._log_to_row(log),
                    )
                    imported += cur.rowcount if cur.rowcount else 0
                except Exception:
                    logger.debug("[GroupMgr] 跳过一条无法导入的旧日志", exc_info=True)
            conn.commit()
        return imported

    def import_legacy_logs(self, delete_file: bool = False) -> int:
        # 读取旧的 moderation_logs.json，批量 INSERT 到 SQLite，然后备份并删除原文件。
        if not self.legacy_logs_path.exists():
            return 0
        with open(self.legacy_logs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return 0
        imported = self.import_logs(data)
        self.set_meta("logs_migrated_at", str(int(time.time())))
        if delete_file:
            backup = self.legacy_logs_path.with_suffix(self.legacy_logs_path.suffix + ".bak")
            try:
                shutil.copy2(self.legacy_logs_path, backup)
            except Exception:
                logger.warning("[GroupMgr] 旧日志备份失败，将继续删除原文件", exc_info=True)
            os.remove(self.legacy_logs_path)
        return imported

    def list_logs(self, limit: int = 200, offset: int = 0,
                  group_id: str = "", user_id: str = "", action: str = "") -> List[dict]:
        sql = "SELECT * FROM moderation_logs WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if action:
            sql += " AND action LIKE ?"
            params.append(f"%{action}%")
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_log(r) for r in rows]

    def count_logs_filtered(self, group_id: str = "", user_id: str = "", action: str = "") -> int:
        sql = "SELECT COUNT(*) AS c FROM moderation_logs WHERE 1=1"
        params: List[object] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if action:
            sql += " AND action LIKE ?"
            params.append(f"%{action}%")
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def list_logs_asc(self, limit: int = 500) -> List[dict]:
        # 按 id 降序查询后反转返回（即实际升序），用于内存缓存按时间顺序回放。
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_log(r) for r in reversed(rows)]

    def get_log(self, log_id: int) -> Optional[dict]:
        # 根据 id 查询单条审核日志。
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM moderation_logs WHERE id=?", (log_id,)).fetchone()
        return self._row_to_log(row) if row else None

    def delete_logs(self, ids: Iterable[int]) -> int:
        # 按 id 列表批量删除审核日志，返回实际删除条数。
        ids = self._positive_ints(ids)
        if not ids:
            return 0
        total = 0
        with self._connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(f"DELETE FROM moderation_logs WHERE id IN ({placeholders})", chunk)
                total += int(cur.rowcount or 0)
            conn.commit()
        return total

    def delete_logs_by_users(self, user_ids: Iterable[object]) -> int:
        # 按用户 ID 批量删除审核日志，避免 WebUI 为了拿日志 id 拉取全量导出。
        users = self._non_empty_strings(user_ids)
        if not users:
            return 0
        total = 0
        with self._connect() as conn:
            for start in range(0, len(users), 500):
                chunk = users[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(f"DELETE FROM moderation_logs WHERE user_id IN ({placeholders})", chunk)
                total += int(cur.rowcount or 0)
            conn.commit()
        return total

    def delete_all_logs(self) -> int:
        # 清空审核日志表，返回删除的总条数。
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM moderation_logs").fetchone()
            count = int(row["c"] or 0)
            conn.execute("DELETE FROM moderation_logs")
            conn.commit()
        return count

    def max_log_id(self) -> int:
        # 查询当前最大 id，用于计算下一个自增 id。
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) AS m FROM moderation_logs").fetchone()
        return int(row["m"] or -1)

    def migrate_legacy(self, delete_logs: bool = False) -> dict:
        # 将旧的 JSON 格式日志导入 SQLite，返回导入数和最终状态。
        imported_logs = self.import_legacy_logs(delete_file=delete_logs)
        return {
            "imported_logs": imported_logs,
            "deleted_legacy_logs": delete_logs and not self.legacy_logs_path.exists(),
            "status": self.migration_status(),
        }

    def get_daily_trend(self, days: int = 30) -> List[dict]:
        # 按天聚合审核日志，返回最近 days 天每日的拦截/放行/总审核数。
        # 结果按日期升序排列，日期键为 YYYY-MM-DD 格式字符串。
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT DATE(time) as day, "
                "SUM(CASE WHEN action LIKE '%撤回%' THEN 1 ELSE 0 END) as blocked, "
                "SUM(CASE WHEN action LIKE '%放行%' THEN 1 ELSE 0 END) as passed, "
                "COUNT(*) as total "
                "FROM moderation_logs WHERE ts >= ? "
                "GROUP BY DATE(time) ORDER BY day ASC",
                (since,),
            ).fetchall()
        return [{"date": r["day"], "blocked": r["blocked"] or 0, "passed": r["passed"] or 0, "total": r["total"] or 0} for r in rows]

    def get_violation_distribution(self, days: int = 30) -> List[dict]:
        # 按违规原因分组统计最近 days 天的分布情况，返回各类型及其出现次数。
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT reason, COUNT(*) as count "
                "FROM moderation_logs WHERE ts >= ? AND action LIKE '%撤回%' AND reason != '' "
                "GROUP BY reason ORDER BY count DESC",
                (since,),
            ).fetchall()
        return [{"reason": r["reason"], "count": r["count"] or 0} for r in rows]

    def get_group_activity_ranking(self, days: int = 30, top_n: int = 10) -> List[dict]:
        # 按群号聚合最近 days 天的拦截量并排序，返回 Top N 群拦截排行。
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT group_id, COUNT(*) as count "
                "FROM moderation_logs WHERE ts >= ? AND action LIKE '%撤回%' AND group_id != '' "
                "GROUP BY group_id ORDER BY count DESC LIMIT ?",
                (since, top_n),
            ).fetchall()
        return [{"group_id": r["group_id"], "count": r["count"] or 0} for r in rows]

    def get_hourly_distribution(self, days: int = 7) -> List[dict]:
        # 按小时聚合最近 days 天的拦截量，返回 0-23 各时段分布，用于分析活跃高峰。
        with self._connect() as conn:
            since = int(time.time()) - days * 86400
            rows = conn.execute(
                "SELECT CAST(STRFTIME('%H', time) AS INTEGER) as hour, COUNT(*) as count "
                "FROM moderation_logs WHERE ts >= ? AND action LIKE '%撤回%' "
                "GROUP BY hour ORDER BY hour ASC",
                (since,),
            ).fetchall()
        return [{"hour": r["hour"], "count": r["count"] or 0} for r in rows]

    # ============================================================
    # v2.4.0 新增：F1 入群审核规则
    # ============================================================
    def get_join_audit_rule(self, group_id: str) -> Optional[dict]:
        # 读取某个群的入群审核规则；group_id 传 'default' 取全局兜底规则。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id, accept_keywords, reject_keywords, default_action, reject_reason, enabled "
                "FROM join_audit_rules WHERE group_id=?",
                (str(group_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "group_id": row["group_id"],
            "accept_keywords": self._loads_list(row["accept_keywords"]),
            "reject_keywords": self._loads_list(row["reject_keywords"]),
            "default_action": row["default_action"] or "manual",
            "reject_reason": row["reject_reason"] or "",
            "enabled": bool(row["enabled"]),
        }

    def list_join_audit_rules(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT group_id, accept_keywords, reject_keywords, default_action, reject_reason, enabled "
                "FROM join_audit_rules ORDER BY group_id"
            ).fetchall()
        return [
            {
                "group_id": r["group_id"],
                "accept_keywords": self._loads_list(r["accept_keywords"]),
                "reject_keywords": self._loads_list(r["reject_keywords"]),
                "default_action": r["default_action"] or "manual",
                "reject_reason": r["reject_reason"] or "",
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        ]

    def save_join_audit_rule(self, group_id: str, accept_keywords: List[str], reject_keywords: List[str],
                             default_action: str = "manual", reject_reason: str = "", enabled: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO join_audit_rules("
                "group_id, accept_keywords, reject_keywords, default_action, reject_reason, enabled"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (
                    str(group_id),
                    json.dumps(accept_keywords or [], ensure_ascii=False),
                    json.dumps(reject_keywords or [], ensure_ascii=False),
                    default_action or "manual",
                    reject_reason or "",
                    1 if enabled else 0,
                ),
            )
            conn.commit()

    def delete_join_audit_rule(self, group_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM join_audit_rules WHERE group_id=?", (str(group_id),))
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：F3 定时解禁
    # ============================================================
    def add_scheduled_unban(self, group_id: str, user_id: str, unban_at: int, created_at: int) -> None:
        # 登记/更新一条定时解禁计划（同群同人唯一，新计划覆盖旧的）。
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scheduled_unbans("
                "group_id, user_id, unban_at, created_at, retry_count, next_retry_at, last_error"
                ") VALUES(?, ?, ?, ?, 0, ?, '')",
                (
                    str(group_id), str(user_id), int(unban_at), int(created_at),
                    int(unban_at),
                ),
            )
            conn.commit()

    def list_due_unbans(self, now_ts: int) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, group_id, user_id, unban_at, retry_count, next_retry_at, last_error "
                "FROM scheduled_unbans "
                "WHERE CASE WHEN next_retry_at > 0 THEN next_retry_at ELSE unban_at END <= ? "
                "ORDER BY CASE WHEN next_retry_at > 0 THEN next_retry_at ELSE unban_at END",
                (int(now_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all_scheduled_unbans(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, group_id, user_id, unban_at, retry_count, next_retry_at, last_error "
                "FROM scheduled_unbans ORDER BY unban_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_scheduled_unban_retry(
        self, unban_id: int, next_retry_at: int, last_error: str = ""
    ) -> bool:
        """记录一次解禁失败并安排下次重试，不丢失原任务。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE scheduled_unbans SET retry_count=retry_count+1, "
                "next_retry_at=?, last_error=? WHERE id=?",
                (int(next_retry_at), str(last_error or "")[:500], int(unban_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def delete_scheduled_unban(self, unban_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM scheduled_unbans WHERE id=?", (int(unban_id),))
            conn.commit()
        return bool(cur.rowcount)

    def delete_scheduled_unban_by_target(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM scheduled_unbans WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：F5 群管理员动态授权
    # ============================================================
    def get_group_admin_grant(self, group_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id, grant_owner, grant_admin, enabled FROM group_admin_grant WHERE group_id=?",
                (str(group_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "group_id": row["group_id"],
            "grant_owner": bool(row["grant_owner"]),
            "grant_admin": bool(row["grant_admin"]),
            "enabled": bool(row["enabled"]),
        }

    def list_group_admin_grants(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT group_id, grant_owner, grant_admin, enabled FROM group_admin_grant ORDER BY group_id"
            ).fetchall()
        return [
            {
                "group_id": r["group_id"],
                "grant_owner": bool(r["grant_owner"]),
                "grant_admin": bool(r["grant_admin"]),
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        ]

    def save_group_admin_grant(self, group_id: str, grant_owner: bool, grant_admin: bool, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO group_admin_grant(group_id, grant_owner, grant_admin, enabled) "
                "VALUES(?, ?, ?, ?)",
                (str(group_id), 1 if grant_owner else 0, 1 if grant_admin else 0, 1 if enabled else 0),
            )
            conn.commit()

    def delete_group_admin_grant(self, group_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM group_admin_grant WHERE group_id=?", (str(group_id),))
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：单群管理类名单（managed_lists）
    # ============================================================
    _MANAGED_LIST_TYPES = ("group_white", "group_black", "user_black", "user_white", "admin")

    def load_managed_list(self, list_type: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT value FROM managed_lists WHERE list_type=? ORDER BY value",
                (str(list_type),),
            ).fetchall()
        return [r["value"] for r in rows]

    def count_managed_list(self, list_type: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM managed_lists WHERE list_type=?",
                (str(list_type),),
            ).fetchone()
        return int(row["c"] or 0)

    def add_managed_list_value(self, list_type: str, value: str) -> bool:
        value = str(value).strip()
        if not value:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO managed_lists(list_type, value) VALUES(?, ?)",
                (str(list_type), value),
            )
            conn.commit()
        return bool(cur.rowcount)

    def remove_managed_list_value(self, list_type: str, value: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM managed_lists WHERE list_type=? AND value=?",
                (str(list_type), str(value).strip()),
            )
            conn.commit()
        return bool(cur.rowcount)

    def seed_managed_list(self, list_type: str, values: Iterable[str]) -> int:
        # 一次性迁移：把旧 config 名单导入 DB（INSERT OR IGNORE 去重）。
        items = [(str(list_type), str(v).strip()) for v in (values or []) if str(v).strip()]
        if not items:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO managed_lists(list_type, value) VALUES(?, ?)",
                items,
            )
            conn.commit()
        return int(cur.rowcount or 0)

    @staticmethod
    def _loads_list(raw) -> List[str]:
        # 把 DB 里存的 JSON 数组字符串还原成 list[str]，异常时返回空列表。
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [str(x) for x in data] if isinstance(data, list) else []
        except Exception:
            return []

    # ============================================================
    # v2.4.0 新增：群超管 group_super_admins
    # ============================================================
    def list_group_super_admins(self, group_id: str = "") -> List[dict]:
        # 列出群超管：传 group_id 则只列该群，否则列全部。
        with self._connect() as conn:
            if group_id:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_super_admins WHERE group_id=? ORDER BY user_id",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_super_admins ORDER BY group_id, user_id"
                ).fetchall()
        return [{"group_id": r["group_id"], "user_id": r["user_id"]} for r in rows]

    def is_group_super_admin(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM group_super_admins WHERE group_id=? AND user_id=? LIMIT 1",
                (str(group_id), str(user_id)),
            ).fetchone()
        return row is not None

    def add_group_super_admin(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO group_super_admins(group_id, user_id) VALUES(?, ?)",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def remove_group_super_admin(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM group_super_admins WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    # ============================================================
    # v2.4.0 新增：群级 bot 权限黑名单 group_admin_block
    # ============================================================
    def list_group_admin_blocks(self, group_id: str = "") -> List[dict]:
        with self._connect() as conn:
            if group_id:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_admin_block WHERE group_id=? ORDER BY user_id",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT group_id, user_id FROM group_admin_block ORDER BY group_id, user_id"
                ).fetchall()
        return [{"group_id": r["group_id"], "user_id": r["user_id"]} for r in rows]

    def is_group_admin_blocked(self, group_id: str, user_id: str) -> bool:
        # 该用户在该群是否被剥夺了 bot 管理权限（群主可设，优先级最高）。
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM group_admin_block WHERE group_id=? AND user_id=? LIMIT 1",
                (str(group_id), str(user_id)),
            ).fetchone()
        return row is not None

    def add_group_admin_block(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO group_admin_block(group_id, user_id) VALUES(?, ?)",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def remove_group_admin_block(self, group_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM group_admin_block WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            conn.commit()
        return bool(cur.rowcount)
