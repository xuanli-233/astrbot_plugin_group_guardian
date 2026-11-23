# -*- coding: utf-8 -*-
"""存储管理（二次开发新增）。WebUI「系统 → 存储管理」。

看占用：数据库文件（含 -wal / -shm）、可回收的空闲页、每类记录的条数 / 占用 / 最早时间、各图片目录的文件数和大小、
所在磁盘剩余空间。按类别归组（链接记录、小程序记录、码发送记录与图片、码按人统计、违规记录、退群记录、操作记录、
批量任务、群成员缓存、其它）。每张表的占用优先用 SQLite 的 dbstat 统计；多数 Python 自带的 SQLite 没有编进 dbstat，
这时每张表抽最多 200 行算平均行大小 × 行数，再按比例缩放到数据库实际已用大小（页面上标「约」）。

按需处理（每一步都写操作记录，同时只能跑一个）：
  purge      删掉某类里 N 天以前的记录（0 = 全部），带图片的一并删图片文件
  orphans    删掉图片目录里数据库已经不认识的文件（孤儿文件）
  recompress 把超过 N KB 的图片重新压成 JPEG（同 _compress_image，逐步降质量 / 缩小）；数据库里的路径、大小同步更新
  vacuum     整理数据库：先 wal_checkpoint(TRUNCATE)，再 VACUUM，把删掉记录留下的空闲页还给磁盘
统计和处理都在线程里做，不卡消息处理。群成员缓存、配置 / 规则 / 名单这些不提供删除。
"""
import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

try:
    from .code_limit import _compress_image
except ImportError:
    try:
        from code_limit import _compress_image
    except ImportError:
        def _compress_image(data, max_bytes):
            return data, "bin", (0, 0)

# 图片目录：(目录名, 表, 时间列)
SA_IMAGE_DIRS = {
    "code_images": ("code_images", "last_used"),
    "log_images": ("log_images", "created_at"),
    "leave_images": ("leave_images", "created_at"),
}
# 类别：key → (名称, 说明, [(表, 时间列 or None)], 图片目录 or None, 能否按天删除, 自动清理的配置说明)
SA_CATEGORIES: List[Tuple[str, str, str, List[Tuple[str, Optional[str]]], Optional[str], bool]] = [
    ("links", "链接记录", "每次发链接的记录、链接本身和读到的标题", [("lk_sends", "ts"), ("lk_urls", None)], None, True),
    ("miniapps", "小程序记录", "每次发小程序的记录、小程序和标题内容",
     [("mp_sends", "ts"), ("mp_apps", None), ("mp_contents", None), ("mp_aliases", None)], None, True),
    ("codes", "码发送记录与图片", "每次发码的记录、原图和码的裁剪图", [("code_sends", "ts"), ("code_images", None)],
     "code_images", True),
    ("code_stats", "码按人统计", "谁发过哪个码、几次，码缩略图", [("code_user_stats", "last_ts"), ("code_thumbs", None)],
     None, True),
    ("violations", "违规记录", "审核日志和保存的图片", [("moderation_logs", "ts"), ("log_images", None)], "log_images", True),
    ("leaves", "退群记录", "退群 / 被踢记录、他最近的消息和图片", [("leave_logs", "ts"), ("leave_images", None)],
     "leave_images", True),
    ("oplog", "操作记录", "踢人、拉黑、撤回、改配置等操作", [("op_logs", "ts")], None, True),
    ("batch", "批量任务", "已完成 / 已取消的批量任务和每一项的结果", [("batch_jobs", "finished_at"), ("batch_items", None)],
     None, True),
    ("directory", "群成员缓存", "群列表和群成员（重复成员、找人用，可在「群成员缓存」页刷新）",
     [("dir_groups", None), ("dir_members", None)], None, False),
]
SA_CAT_KEYS = {c[0] for c in SA_CATEGORIES}
SA_RECOMPRESS_MIN_KB = 30


def _dir_usage(path: Path) -> Tuple[int, int]:
    """(文件数, 字节数)。不跟随符号链接。"""
    files = size = 0
    if not path.is_dir():
        return 0, 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            files += 1
                            size += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, size


class StorageAdminMixin:
    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _sa_db_path(self) -> Optional[Path]:
        p = getattr(getattr(self, "_storage", None), "db_path", None)
        return Path(p) if p else None

    def _sa_data_dir(self) -> Optional[Path]:
        base = getattr(self, "_data_dir", None)
        return Path(base) if base else None

    @staticmethod
    def _sa_tables(conn) -> List[str]:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

    @staticmethod
    async def _sa_offload(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def _sa_report(self) -> dict:
        """（线程里执行）占用情况。"""
        db = self._sa_db_path()
        out = {"db": {}, "categories": [], "dirs": [], "disk": None, "dbstat": False,
               "data_dir": str(self._sa_data_dir() or ""), "generated_at": int(time.time())}
        if db:
            sizes = {}
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db) + suffix)
                sizes[suffix or "main"] = p.stat().st_size if p.exists() else 0
            out["db"] = {"path": str(db), "file": sizes["main"], "wal": sizes["-wal"], "shm": sizes["-shm"],
                         "total": sum(sizes.values())}
        table_bytes: Dict[str, int] = {}
        rows: Dict[str, int] = {}
        oldest: Dict[str, int] = {}
        with self._storage._connect() as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            out["db"].update({"page_size": page_size, "pages": page_count, "free_bytes": free * page_size,
                              "used_bytes": (page_count - free) * page_size})
            tables = self._sa_tables(conn)
            # dbstat：每张表 + 它的索引实际占的页
            try:
                owner = {r[0]: r[1] for r in conn.execute(
                    "SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
                for name, size in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                    t = owner.get(name, name)
                    table_bytes[t] = table_bytes.get(t, 0) + int(size or 0)
                out["dbstat"] = True
            except Exception:
                table_bytes = {}
            for t in tables:
                try:
                    rows[t] = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                except Exception:
                    rows[t] = 0
            if not out["dbstat"]:
                table_bytes = self._sa_estimate(conn, rows, out["db"]["used_bytes"])
                out["estimated"] = True
            for _k, _n, _d, tlist, _dir, _p in SA_CATEGORIES:
                for t, col in tlist:
                    if col and t in rows and rows[t]:
                        try:
                            v = conn.execute(f'SELECT MIN("{col}") FROM "{t}" WHERE "{col}" > 0').fetchone()[0]
                            oldest[t] = int(v or 0)
                        except Exception:
                            pass
        used = set()
        data_dir = self._sa_data_dir()
        for key, name, desc, tlist, img_dir, can_purge in SA_CATEGORIES:
            present = [t for t, _c in tlist if t in rows]
            used.update(present)
            files = fbytes = 0
            if img_dir and data_dir:
                files, fbytes = _dir_usage(data_dir / img_dir)
            main_table = tlist[0][0]
            out["categories"].append({
                "key": key, "name": name, "desc": desc, "can_purge": can_purge,
                "rows": rows.get(main_table, 0), "rows_all": sum(rows.get(t, 0) for t in present),
                "db_bytes": sum(table_bytes.get(t, 0) for t in present),
                "files": files, "file_bytes": fbytes, "oldest": oldest.get(main_table, 0),
                "tables": [{"name": t, "rows": rows.get(t, 0), "bytes": table_bytes.get(t, 0)} for t in present],
                "retention": self._sa_retention(key),
            })
        others = [t for t in rows if t not in used]
        out["categories"].append({
            "key": "other", "name": "其它", "desc": "配置、规则组、黑白名单、权限、定时任务等（不提供删除）",
            "can_purge": False, "rows": sum(rows[t] for t in others), "rows_all": sum(rows[t] for t in others),
            "db_bytes": sum(table_bytes.get(t, 0) for t in others),
            "files": 0, "file_bytes": 0, "oldest": 0,
            "tables": sorted([{"name": t, "rows": rows[t], "bytes": table_bytes.get(t, 0)} for t in others],
                             key=lambda x: -(x["bytes"] or x["rows"])),
            "retention": "",
        })
        if data_dir:
            seen_dirs = set(SA_IMAGE_DIRS)
            for d in sorted(SA_IMAGE_DIRS) + ["outbox"]:
                files, size = _dir_usage(data_dir / d)
                out["dirs"].append({"name": d, "files": files, "bytes": size, "orphans": None})
            try:
                for e in os.scandir(data_dir):
                    if e.is_dir(follow_symlinks=False) and e.name not in seen_dirs and e.name != "outbox":
                        files, size = _dir_usage(Path(e.path))
                        out["dirs"].append({"name": e.name, "files": files, "bytes": size, "orphans": None})
            except OSError:
                pass
            try:
                orphans = self._sa_orphans(False)["dirs"]
                for d in out["dirs"]:
                    if d["name"] in orphans:
                        d["orphans"] = orphans[d["name"]]
            except Exception as e:
                logger.debug(f"[GroupMgr] 统计孤儿文件失败: {e}")
            try:
                du = shutil.disk_usage(str(data_dir))
                out["disk"] = {"total": du.total, "free": du.free}
            except OSError:
                pass
        return out

    @staticmethod
    def _sa_estimate(conn, rows: Dict[str, int], used_bytes: int) -> Dict[str, int]:
        """没有 dbstat 时估算每张表的占用：抽样平均行大小 × 行数，按比例缩放到数据库已用大小（含索引、页内空隙）。"""
        raw: Dict[str, float] = {}
        for t, n in rows.items():
            if not n:
                continue
            try:
                sample = conn.execute(f'SELECT * FROM "{t}" LIMIT 200').fetchall()
            except Exception:
                continue
            if not sample:
                continue
            total = 0
            for r in sample:
                for v in tuple(r):
                    if v is None:
                        total += 1
                    elif isinstance(v, (bytes, bytearray)):
                        total += len(v)
                    elif isinstance(v, str):
                        total += len(v.encode("utf-8", "replace"))
                    else:
                        total += 8
                total += 8   # 行头
            raw[t] = total / len(sample) * n
        whole = sum(raw.values())
        if not whole or not used_bytes:
            return {t: int(v) for t, v in raw.items()}
        scale = used_bytes / whole
        return {t: int(v * scale) for t, v in raw.items()}

    def _sa_retention(self, key: str) -> str:
        """自动清理规则（给页面看）。"""
        try:
            if key == "links":
                return f"自动保留 {self._lk_store_days()} 天" if callable(getattr(self, "_lk_store_days", None)) else ""
            if key == "miniapps":
                return f"自动保留 {self._mp_store_days()} 天" if callable(getattr(self, "_mp_store_days", None)) else ""
            if key == "codes":
                h = self._cs_ttl_hours()
                return f"自动保留 {h // 24} 天" if h % 24 == 0 else f"自动保留 {h} 小时"
            if key == "code_stats":
                d = self._cs_stats_keep_days()
                return f"{d} 天没再发过的自动删除" if d else "永久保留"
            if key == "violations":
                return f"图片自动保留 {self._ms_days()} 天，记录不自动删除" if callable(getattr(self, "_ms_days", None)) \
                    else "不自动删除"
            if key == "leaves":
                return f"图片自动保留 {self._ll_image_days()} 天，记录不自动删除" \
                    if callable(getattr(self, "_ll_image_days", None)) else "不自动删除"
            if key == "batch":
                return f"只保留最近 {getattr(self, 'BQ_KEEP_JOBS', 200)} 个已结束的任务"
            if key == "oplog":
                return "不自动删除"
        except Exception:
            return ""
        return ""

    # ------------------------------------------------------------------
    # 删除旧记录
    # ------------------------------------------------------------------
    def _sa_purge(self, key: str, days: int) -> dict:
        """（线程里执行）删掉 key 这类里 days 天以前的记录（0 = 全部）。返回 {rows, files, bytes}。"""
        cutoff = int(time.time()) + 1 if days <= 0 else int(time.time()) - days * 86400
        removed_rows = removed_files = removed_bytes = 0

        def drop_files(conn, table, col):
            nonlocal removed_files, removed_bytes
            extra = ""
            if table == "code_images":   # 还被保留下来的发送记录引用的图不删
                extra = " AND hash NOT IN (SELECT img_hash FROM code_sends UNION SELECT crop_hash FROM code_sends)"
            got = conn.execute(f'SELECT hash, path FROM "{table}" WHERE "{col}" < ?{extra}', (cutoff,)).fetchall()
            for r in got:
                try:
                    size = os.path.getsize(r["path"])
                    os.remove(r["path"])
                    removed_files += 1
                    removed_bytes += size
                except OSError:
                    pass
            conn.executemany(f'DELETE FROM "{table}" WHERE hash=?', [(r["hash"],) for r in got])

        with self._storage._connect() as conn:
            tables = set(self._sa_tables(conn))

            def delete(sql, args=()):
                nonlocal removed_rows
                removed_rows += int(conn.execute(sql, args).rowcount or 0)
            if key == "links" and "lk_sends" in tables:
                delete("DELETE FROM lk_sends WHERE ts < ?", (cutoff,))
                if "lk_urls" in tables:
                    delete("DELETE FROM lk_urls WHERE url_hash NOT IN (SELECT DISTINCT url_hash FROM lk_sends)")
            elif key == "miniapps" and "mp_sends" in tables:
                delete("DELETE FROM mp_sends WHERE ts < ?", (cutoff,))
                if "mp_contents" in tables:
                    delete("DELETE FROM mp_contents WHERE content_hash NOT IN (SELECT DISTINCT content_hash FROM mp_sends)")
            elif key == "codes" and "code_sends" in tables:
                delete("DELETE FROM code_sends WHERE ts < ?", (cutoff,))
                if "code_images" in tables:
                    drop_files(conn, "code_images", "last_used")
            elif key == "code_stats" and "code_user_stats" in tables:
                delete("DELETE FROM code_user_stats WHERE last_ts < ?", (cutoff,))
                if "code_thumbs" in tables:
                    sends = " UNION SELECT code_hash FROM code_sends" if "code_sends" in tables else ""
                    delete(f"DELETE FROM code_thumbs WHERE code_hash NOT IN (SELECT code_hash FROM code_user_stats{sends})")
            elif key == "violations" and "moderation_logs" in tables:
                delete("DELETE FROM moderation_logs WHERE ts < ?", (cutoff,))
                if "log_images" in tables:
                    drop_files(conn, "log_images", "created_at")
            elif key == "leaves" and "leave_logs" in tables:
                delete("DELETE FROM leave_logs WHERE ts < ?", (cutoff,))
                if "leave_images" in tables:
                    drop_files(conn, "leave_images", "created_at")
            elif key == "oplog" and "op_logs" in tables:
                delete("DELETE FROM op_logs WHERE ts < ?", (cutoff,))
            elif key == "batch" and "batch_jobs" in tables:
                # 只删已经结束的任务（进行中 / 暂停的不动）
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM batch_jobs WHERE finished_at IS NOT NULL AND finished_at < ? "
                    "AND state IN ('done', 'cancelled')", (cutoff,))]
                for i in range(0, len(ids), 500):
                    part = ids[i:i + 500]
                    marks = ",".join("?" * len(part))
                    if "batch_items" in tables:
                        delete(f"DELETE FROM batch_items WHERE job_id IN ({marks})", part)
                    delete(f"DELETE FROM batch_jobs WHERE id IN ({marks})", part)
            conn.commit()
        return {"rows": removed_rows, "files": removed_files, "bytes": removed_bytes}

    # ------------------------------------------------------------------
    # 孤儿文件 / 重新压缩 / 整理数据库
    # ------------------------------------------------------------------
    def _sa_known_paths(self, conn, table: str) -> set:
        try:
            return {os.path.normcase(os.path.abspath(r[0])) for r in conn.execute(f'SELECT path FROM "{table}"')}
        except Exception:
            return set()

    def _sa_orphans(self, delete: bool = False) -> dict:
        """（线程里执行）图片目录里数据库不认识的文件。delete=True 时删掉。"""
        data_dir = self._sa_data_dir()
        out = {"files": 0, "bytes": 0, "dirs": {}}
        if not data_dir:
            return out
        with self._storage._connect() as conn:
            tables = set(self._sa_tables(conn))
            known = {d: self._sa_known_paths(conn, t) for d, (t, _c) in SA_IMAGE_DIRS.items() if t in tables}
        for d in SA_IMAGE_DIRS:
            folder = data_dir / d
            if d not in known or not folder.is_dir():
                continue
            n = size = 0
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                if not e.is_file(follow_symlinks=False):
                    continue
                if os.path.normcase(os.path.abspath(e.path)) in known[d]:
                    continue
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                    if delete:
                        os.remove(e.path)
                    n += 1
                    size += sz
                except OSError:
                    continue
            out["dirs"][d] = {"files": n, "bytes": size}
            out["files"] += n
            out["bytes"] += size
        return out

    def _sa_recompress(self, max_kb: int, limit: int = 5000) -> dict:
        """（线程里执行）超过 max_kb 的图片重新压缩。返回 {files, before, after, failed}。"""
        max_bytes = max(SA_RECOMPRESS_MIN_KB, int(max_kb)) * 1024
        done = before = after = failed = 0
        with self._storage._connect() as conn:
            tables = set(self._sa_tables(conn))
            for _d, (table, _col) in SA_IMAGE_DIRS.items():
                if table not in tables:
                    continue
                rows = conn.execute(f'SELECT hash, path FROM "{table}" WHERE bytes > ? ORDER BY bytes DESC LIMIT ?',
                                    (max_bytes, limit)).fetchall()
                for r in rows:
                    path = Path(r["path"])
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    if len(data) <= max_bytes:
                        conn.execute(f'UPDATE "{table}" SET bytes=? WHERE hash=?', (len(data), r["hash"]))
                        continue
                    out, ext, _size = _compress_image(data, max_bytes)
                    if ext == "bin" or len(out) >= len(data):
                        failed += 1
                        continue
                    new_path = path.with_suffix("." + ext)
                    try:
                        tmp = new_path.with_name(new_path.name + ".tmp")
                        tmp.write_bytes(out)
                        os.replace(tmp, new_path)
                        if new_path != path:
                            try:
                                path.unlink()
                            except OSError:
                                pass
                    except OSError:
                        failed += 1
                        continue
                    conn.execute(f'UPDATE "{table}" SET path=?, bytes=? WHERE hash=?', (str(new_path), len(out), r["hash"]))
                    done += 1
                    before += len(data)
                    after += len(out)
                conn.commit()
        return {"files": done, "before": before, "after": after, "failed": failed}

    def _sa_vacuum(self) -> dict:
        """（线程里执行）整理数据库。返回前后大小。"""
        db = self._sa_db_path()

        def total():
            return sum(Path(str(db) + s).stat().st_size for s in ("", "-wal", "-shm")
                       if db and Path(str(db) + s).exists())
        before = total()
        with self._storage._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None   # VACUUM 不能在事务里
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"before": before, "after": total()}

    # ------------------------------------------------------------------
    # 入口（WebUI）
    # ------------------------------------------------------------------
    async def _sa_run(self, action: str, category: str = "", days: int = 0, max_kb: int = 300,
                      source: str = "WebUI", operator: str = "") -> Tuple[Optional[dict], str]:
        if getattr(self, "_sa_busy", False):
            return None, "上一个存储操作还没做完，请稍等"
        if action == "purge":
            if category not in SA_CAT_KEYS or not dict((c[0], c[5]) for c in SA_CATEGORIES)[category]:
                return None, "这一类不能删除"
            if days < 0 or days > 36500:
                return None, "天数填 0–36500（0 = 全部删除）"
        elif action == "recompress":
            if not SA_RECOMPRESS_MIN_KB <= max_kb <= 5000:
                return None, f"压缩目标填 {SA_RECOMPRESS_MIN_KB}–5000 KB"
        elif action not in ("orphans", "vacuum"):
            return None, "未知操作"
        self._sa_busy = True
        started = time.time()
        try:
            if action == "purge":
                result = await self._sa_offload(self._sa_purge, category, int(days))
                name = next(c[1] for c in SA_CATEGORIES if c[0] == category)
                detail = f"{name}：{'全部' if days <= 0 else f'{days} 天以前的'}，删除 {result['rows']} 条" + \
                    (f"、图片 {result['files']} 个（{result['bytes'] // 1024} KB）" if result["files"] else "")
                op = "清理存储"
            elif action == "orphans":
                result = await self._sa_offload(self._sa_orphans, True)
                detail = f"删除孤儿图片 {result['files']} 个（{result['bytes'] // 1024} KB）"
                op = "清理孤儿文件"
            elif action == "recompress":
                result = await self._sa_offload(self._sa_recompress, int(max_kb))
                detail = (f"超过 {max_kb} KB 的图片重新压缩 {result['files']} 个："
                          f"{result['before'] // 1024} KB → {result['after'] // 1024} KB"
                          + (f"，{result['failed']} 个没压下来" if result["failed"] else ""))
                op = "压缩图片"
            else:
                result = await self._sa_offload(self._sa_vacuum)
                detail = f"整理数据库：{result['before'] // 1024} KB → {result['after'] // 1024} KB"
                op = "整理数据库"
        except Exception as e:
            logger.warning(f"[GroupMgr] 存储操作 {action} 失败: {e}")
            return None, f"操作失败：{e}"
        finally:
            self._sa_busy = False
        result["seconds"] = round(time.time() - started, 1)
        record = getattr(self, "_record_op", None)
        if callable(record):
            record(op, source=source, operator=operator, detail=detail)
        logger.info(f"[GroupMgr] {detail}（{result['seconds']} 秒）")
        return result, ""
