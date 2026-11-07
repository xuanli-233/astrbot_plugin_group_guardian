# -*- coding: utf-8 -*-
"""码记录、同一个码的发送次数限制（二次开发新增）。WebUI「码管理」页。

码记录（code_store_enabled，默认开）
  每条解出了内容的码都记一行（SQLite code_sends）：时间、群、发送者、码内容、处理结果
    pass  放行（计入发送次数）   limit  超过次数被撤回   rule  命中码内容规则 / 已拉黑被撤回
    allow 在白名单里（放行，不计次）
  同时保存图片：原图和每个码的裁剪图存到插件数据目录 code_images/，超过 code_image_max_kb（默认 500KB）
  先压缩；同一张图只存一份。记录和图片保留 code_store_ttl_hours 小时后自动清理（0 = 滚动窗口 × 2；
  填了也不会短于滚动窗口，否则计次会漏算）。

发送次数限制（code_limit_enabled，默认关）
  没命中规则、不在白名单的码按内容计次：code_limit_window_hours 小时（滑动窗口）内超过 code_limit_max 次，
  超出的那条消息撤回（同关键词命中：先备份、可选禁言、写日志），可选在群里警告。
    code_limit_scope    global 所有群合计 / group 每个群单独计
    code_limit_count_by code   按码计（谁发都算）/ user 按人计（同一个人发同一个码才累加）
  只有解出了内容的码能计次；同一条消息里重复的码只算一次；被规则撤回、超限撤回、白名单的不计次。
"""
import asyncio
import base64
import hashlib
import io
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

try:
    from .live_log import live_detail
except ImportError:  # 单元测试直接加载本文件
    def live_detail(*_a, **_k):
        return None

CODE_LIMIT_WINDOW_MAX_HOURS = 168
# 码记录 / 图片最长保留（小时），一年
CODE_STORE_TTL_MAX_HOURS = 8760
CODE_LIMIT_TEXT_MAX = 300
CODE_IMAGE_THUMB = 96
DEFAULT_WARN = "[群管] {name}，同一个码 {hours} 小时内只能发 {max} 次，这条已撤回"
OUTCOME_LABEL = {"pass": "放行", "limit": "超限撤回", "rule": "规则撤回", "allow": "白名单放行"}


def _compress_image(data: bytes, max_bytes: int) -> Tuple[bytes, str, Tuple[int, int]]:
    """（线程池里执行）不超过 max_bytes 时原样保存；超过时转 JPEG，逐步降低质量 / 缩小直到不超过。
    返回 (字节, 扩展名, (宽, 高))。没有 Pillow 时原样返回。"""
    try:
        from PIL import Image, ImageOps
    except Exception:
        return data, "bin", (0, 0)
    try:
        opened = Image.open(io.BytesIO(data))
        fmt = (opened.format or "").lower()  # exif_transpose 返回的副本没有 format，要先取
        image = ImageOps.exif_transpose(opened)
        size = image.size
    except Exception:
        return data, "bin", (0, 0)
    if len(data) <= max_bytes:
        ext = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp"}.get(fmt, "bin")
        return data, ext, size
    image = image.convert("RGB")
    out = data
    for scale in (1.0, 0.8, 0.6, 0.45, 0.33, 0.25):
        resized = image if scale == 1.0 else image.resize(
            (max(1, int(size[0] * scale)), max(1, int(size[1] * scale))))
        for quality in (85, 75, 65, 50):
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if len(out) <= max_bytes:
                return out, "jpg", resized.size
    return out, "jpg", resized.size


def _crop_image(data: bytes, box) -> Optional[bytes]:
    try:
        from PIL import Image, ImageOps
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        w, h = image.size
        x1, y1, x2, y2 = [int(round(float(v))) for v in (box or [])[:4]]
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        buf = io.BytesIO()
        image.crop((x1, y1, x2, y2)).save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def _thumb(data: bytes, size: int = CODE_IMAGE_THUMB) -> str:
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image.thumbnail((size, size))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=75)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


class CodeLimitMixin:
    CODE_LIMIT_PRUNE_EVERY = 500
    CODE_STORE_CLEAN_INTERVAL = 600

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _cl_init_db(self) -> None:
        if getattr(self, "_cl_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS code_sends ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, group_id TEXT NOT NULL, "
                "user_id TEXT NOT NULL, code_hash TEXT NOT NULL, code_text TEXT NOT NULL DEFAULT '')"
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(code_sends)").fetchall()}
            for col, ddl in (("user_name", "TEXT NOT NULL DEFAULT ''"), ("outcome", "TEXT NOT NULL DEFAULT 'pass'"),
                             ("img_hash", "TEXT NOT NULL DEFAULT ''"), ("crop_hash", "TEXT NOT NULL DEFAULT ''")):
                if col not in cols:
                    conn.execute(f"ALTER TABLE code_sends ADD COLUMN {col} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_hash ON code_sends(code_hash, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_group ON code_sends(group_id, code_hash, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_user ON code_sends(user_id, code_hash, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_ts ON code_sends(ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS code_images ("
                "hash TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, "
                "width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0, thumb TEXT NOT NULL DEFAULT '', "
                "created_at INTEGER NOT NULL, last_used INTEGER NOT NULL)"
            )
            # 按人长期累计（不随 code_store_ttl_hours 清理）：每个 (人, 码, 群) 一行计数；
            # code_thumbs 每个码保留一张小缩略图，原图被清理后「按人统计」仍能看到码长什么样。
            fresh = not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_user_stats'").fetchone()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS code_user_stats ("
                "user_id TEXT NOT NULL, code_hash TEXT NOT NULL, group_id TEXT NOT NULL, "
                "code_text TEXT NOT NULL DEFAULT '', user_name TEXT NOT NULL DEFAULT '', "
                "total INTEGER NOT NULL DEFAULT 0, passed INTEGER NOT NULL DEFAULT 0, "
                "limited INTEGER NOT NULL DEFAULT 0, ruled INTEGER NOT NULL DEFAULT 0, "
                "allowed INTEGER NOT NULL DEFAULT 0, first_ts INTEGER NOT NULL DEFAULT 0, "
                "last_ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, code_hash, group_id))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_user_stats_last ON code_user_stats(last_ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_user_stats_code ON code_user_stats(code_hash)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS code_thumbs ("
                "code_hash TEXT PRIMARY KEY, thumb TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL DEFAULT 0)"
            )
            if fresh:  # 第一次建表：把还没清理的码记录补进累计
                conn.execute(
                    "INSERT OR IGNORE INTO code_user_stats(user_id, code_hash, group_id, code_text, user_name, total, "
                    "passed, limited, ruled, allowed, first_ts, last_ts) "
                    "SELECT user_id, code_hash, group_id, MAX(code_text), MAX(user_name), COUNT(*), "
                    "SUM(outcome='pass'), SUM(outcome='limit'), SUM(outcome='rule'), SUM(outcome='allow'), "
                    "MIN(ts), MAX(ts) FROM code_sends GROUP BY user_id, code_hash, group_id"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO code_thumbs(code_hash, thumb, ts) "
                    "SELECT s.code_hash, i.thumb, MAX(s.ts) FROM code_sends s JOIN code_images i ON i.hash = s.crop_hash "
                    "WHERE s.crop_hash != '' AND i.thumb != '' GROUP BY s.code_hash"
                )
            conn.commit()
        self._cl_db_ready = True

    @staticmethod
    def _cu_bump(conn, group_id: str, user_id: str, code_hash: str, text: str, user_name: str,
                 outcome: str, now: int) -> None:
        """按人长期累计 +1（与 code_sends 写在同一个事务里）。"""
        flags = [int(outcome == k) for k in ("pass", "limit", "rule", "allow")]
        conn.execute(
            "INSERT INTO code_user_stats(user_id, code_hash, group_id, code_text, user_name, total, passed, limited, "
            "ruled, allowed, first_ts, last_ts) VALUES(?,?,?,?,?,1,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, code_hash, group_id) DO UPDATE SET total=total+1, passed=passed+excluded.passed, "
            "limited=limited+excluded.limited, ruled=ruled+excluded.ruled, allowed=allowed+excluded.allowed, "
            "last_ts=MAX(last_ts, excluded.last_ts), "
            "user_name=CASE WHEN excluded.user_name != '' THEN excluded.user_name ELSE user_name END",
            (str(user_id), code_hash, str(group_id), str(text)[:CODE_LIMIT_TEXT_MAX], str(user_name or "")[:64],
             *flags, int(now), int(now)),
        )

    @staticmethod
    def _cl_hash(text: str) -> str:
        return hashlib.sha1(str(text).strip().encode("utf-8", "replace")).hexdigest()

    def _cl_settings(self, group_id: str) -> dict:
        scope = self._cfg_str("code_limit_scope", "global", group_id=group_id).strip().lower()
        by = self._cfg_str("code_limit_count_by", "code", group_id=group_id).strip().lower()
        return {
            "scope": "group" if scope == "group" else "global",
            "by": "user" if by == "user" else "code",
            "hours": min(max(self._cfg_int("code_limit_window_hours", 5, group_id=group_id), 1), CODE_LIMIT_WINDOW_MAX_HOURS),
            "max": min(max(self._cfg_int("code_limit_max", 3, group_id=group_id), 1), 1000),
        }

    def _cs_enabled(self, group_id: str = None) -> bool:
        return bool(self._cfg("code_store_enabled", True, group_id=group_id))

    def _cs_ttl_hours(self) -> int:
        """码记录 / 图片保留时长（小时）。code_store_ttl_hours = 0 时为滚动窗口 × 2；
        自定义值不短于滚动窗口（计次要读窗口内的记录），不超过 CODE_STORE_TTL_MAX_HOURS。"""
        window = min(max(self._cfg_int("code_limit_window_hours", 5), 1), CODE_LIMIT_WINDOW_MAX_HOURS)
        custom = self._cfg_int("code_store_ttl_hours", 0)
        if custom <= 0:
            return window * 2
        return min(max(custom, window), CODE_STORE_TTL_MAX_HOURS)

    def _cs_ttl(self) -> int:
        """码记录 / 图片保留时长（秒），见 _cs_ttl_hours。"""
        return self._cs_ttl_hours() * 3600

    def _cl_count(self, code_hash: str, since: int, group_id: str = "", user_id: str = "") -> int:
        """窗口内计入次数的发送（outcome = pass）。group_id / user_id 为空时不按它过滤。"""
        self._cl_init_db()
        where, args = ["code_hash=?", "ts>=?", "outcome='pass'"], [code_hash, int(since)]
        if group_id:
            where.append("group_id=?")
            args.append(str(group_id))
        if user_id:
            where.append("user_id=?")
            args.append(str(user_id))
        with self._storage._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM code_sends WHERE {' AND '.join(where)}", args).fetchone()
        return int(row["c"] or 0)

    def _cl_record(self, group_id: str, user_id: str, codes: List[Tuple[str, str]], now: int,
                   outcome: str = "pass", user_name: str = "") -> Dict[str, int]:
        """写入发送记录，返回 {码内容: 行 ID}。"""
        if not codes:
            return {}
        self._cl_init_db()
        rows = {}
        with self._storage._connect() as conn:
            for code_hash, text in codes:
                cur = conn.execute(
                    "INSERT INTO code_sends(ts, group_id, user_id, code_hash, code_text, user_name, outcome) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (int(now), str(group_id), str(user_id), code_hash, str(text)[:CODE_LIMIT_TEXT_MAX],
                     str(user_name or "")[:64], outcome),
                )
                rows[text] = int(cur.lastrowid or 0)
                self._cu_bump(conn, group_id, user_id, code_hash, text, user_name, outcome, now)
            conn.commit()
        self._cs_maybe_clean(now)
        return rows

    @staticmethod
    def _cl_unique(texts: List[str]) -> List[Tuple[str, str]]:
        seen, codes = set(), []
        for t in texts or []:
            t = str(t or "").strip()
            if not t:
                continue
            h = CodeLimitMixin._cl_hash(t)
            if h not in seen:
                seen.add(h)
                codes.append((h, t))
        return codes

    # ------------------------------------------------------------------
    # 计次
    # ------------------------------------------------------------------
    def _cl_process(self, group_id: str, user_id: str, texts: List[str], now: Optional[int] = None,
                    user_name: str = "") -> Tuple[Optional[dict], Dict[str, int]]:
        """检查一条消息里解出的码（已排除命中规则的情况）。返回 (超限信息或 None, {码内容: 记录行 ID})。

        白名单的码记为 allow、不计次；有一个码超限整条消息就算超限，这条消息的码都记为 limit（不计次）；
        否则记为 pass。码记录和次数限制都关闭时什么也不记。
        """
        limit_on = bool(self._cfg("code_limit_enabled", False, group_id=group_id))
        store_on = self._cs_enabled(group_id)
        if not limit_on and not store_on:
            return None, {}
        allowed = getattr(self, "_code_allowed", None)
        codes, allow_codes = [], []
        for h, t in self._cl_unique(texts):
            (allow_codes if callable(allowed) and allowed(t) else codes).append((h, t))
        now = int(now or time.time())
        rows = {}
        if allow_codes:
            live_detail([f"码在白名单里，放行：{t[:60]}" for _h, t in allow_codes])
            if store_on:
                rows.update(self._cl_record(group_id, user_id, allow_codes, now, "allow", user_name))
        if not codes:
            return None, rows
        if limit_on:
            opts = self._cl_settings(group_id)
            since = now - opts["hours"] * 3600
            scope_gid = str(group_id) if opts["scope"] == "group" else ""
            scope_uid = str(user_id) if opts["by"] == "user" else ""
            where = ("本群" if scope_gid else "所有群") + ("、这个人" if scope_uid else "")
            counts = []
            for h, t in codes:
                count = self._cl_count(h, since, scope_gid, scope_uid)
                counts.append((t, count))
                if count >= opts["max"]:
                    live_detail(f"码发送次数：{t[:60]} 在{where} {opts['hours']} 小时内"
                                f"已发 {count} 次，上限 {opts['max']} 次 ← 超限")
                    if store_on:
                        rows.update(self._cl_record(group_id, user_id, codes, now, "limit", user_name))
                    return {"text": t, "count": count, **opts}, rows
            live_detail([f"码发送次数：{t[:60]} 在{where}第 {c + 1} 次（{opts['hours']} 小时内上限 {opts['max']} 次）"
                         for t, c in counts])
        rows.update(self._cl_record(group_id, user_id, codes, now, "pass", user_name))
        return None, rows

    def _cl_check(self, group_id: str, user_id: str, texts: List[str], now: Optional[int] = None) -> Optional[dict]:
        """兼容旧接口：只返回超限信息。"""
        return self._cl_process(group_id, user_id, texts, now)[0]

    def _cs_log_rule(self, group_id: str, user_id: str, user_name: str, texts: List[str],
                     collected: Optional[list] = None) -> None:
        """命中码内容规则被撤回的消息：只做记录（不计次）。"""
        if not self._cs_enabled(group_id):
            return
        allowed = getattr(self, "_code_allowed", None)
        codes = [(h, t) for h, t in self._cl_unique(texts) if not (callable(allowed) and allowed(t))]
        try:
            rows = self._cl_record(group_id, user_id, codes, int(time.time()), "rule", user_name)
        except Exception as e:
            logger.debug(f"[GroupMgr] 记录码失败: {e}")
            return
        self._cs_schedule_images(rows, collected)

    def _cl_warning(self, group_id: str, user_id: str, user_name: str, over: dict) -> Optional[str]:
        if not self._cfg("code_limit_warn_enabled", True, group_id=group_id):
            return None
        throttled = getattr(self, "_hit_throttled", None)
        if callable(throttled) and throttled("code_limit_warn", group_id, user_id):
            return None  # 同一个人连续超限时 60 秒内只警告一次
        template = self._cfg_str("code_limit_warn_template", DEFAULT_WARN, group_id=group_id) or DEFAULT_WARN
        return (template.replace("{name}", user_name or user_id).replace("{uid}", str(user_id))
                .replace("{group}", str(group_id)).replace("{hours}", str(over["hours"]))
                .replace("{max}", str(over["max"])).replace("{count}", str(over["count"])))

    async def _cl_enforce(self, event, group_id: str, user_id: str, user_name: str, text: str,
                          code_texts: List[str], image_urls: list, summary: str = "",
                          collected: Optional[list] = None) -> Tuple[bool, Optional[str]]:
        """没命中规则时调用：记录码；超限则撤回（与关键词命中同样的处理）并返回警告。返回 (是否已处理, 群内提示)。"""
        try:
            over, rows = self._cl_process(group_id, user_id, code_texts, user_name=user_name)
        except Exception as e:
            logger.warning(f"[GroupMgr] 码发送次数检查出错: {e}")
            return False, None
        self._cs_schedule_images(rows, collected)
        if not over:
            return False, None
        where = ("本群" if over["scope"] == "group" else "所有群合计") + ("、按人计" if over.get("by") == "user" else "")
        reason = f"同一个码 {over['hours']} 小时内超过 {over['max']} 次（{where}已发 {over['count']} 次）"
        extra = f"码内容：{over['text'][:200]}" + (f"\n识别结果：\n{summary}" if summary else "")
        await self._execute_hit_action(
            event, group_id, user_id, user_name, text or "[图片]", reason,
            kind="code_limit", extra_text=extra, image_urls=image_urls,
        )
        return True, self._cl_warning(group_id, user_id, user_name, over)

    # ------------------------------------------------------------------
    # 图片（原图 + 裁剪图）
    # ------------------------------------------------------------------
    def _cs_image_dir(self) -> Optional[Path]:
        base = getattr(self, "_data_dir", None)
        if not base:
            return None
        path = Path(base) / "code_images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cs_schedule_images(self, rows: Dict[str, int], collected: Optional[list]) -> None:
        if not rows or not collected or not self._cs_enabled():
            return
        tasks = getattr(self, "_cs_tasks", None)
        if tasks is None:
            tasks = self._cs_tasks = set()

        async def run():
            try:
                await self._cs_save_images(rows, collected)
            except Exception as e:
                logger.debug(f"[GroupMgr] 保存码图片失败: {e}")

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @staticmethod
    async def _cs_offload(fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, fn, *args)
        except RuntimeError:
            return fn(*args)

    async def _cs_save_images(self, rows: Dict[str, int], collected: list) -> None:
        """把每个码所在的原图和它的裁剪图存盘，并关联到记录行。"""
        folder = self._cs_image_dir()
        if folder is None:
            return
        max_bytes = min(max(self._cfg_int("code_image_max_kb", 500), 50), 5000) * 1024
        text_of = getattr(self, "_code_item_text", None)
        for item in collected or []:
            data = item.get("data")
            codes = (item.get("scan") or {}).get("codes") or []
            if not data or not codes:
                continue
            img_hash = ""
            for c in codes:
                if not c.get("decoded"):
                    continue
                t = str(text_of(c) if callable(text_of) else "").strip()
                row_id = rows.get(t)
                if not row_id:
                    continue
                if not img_hash:
                    img_hash = await self._cs_store_bytes(folder, "orig", data, max_bytes)
                crop_hash = ""
                box = c.get("crop_box") or c.get("bbox")
                if box:
                    crop = await self._cs_offload(_crop_image, data, box)
                    if crop:
                        crop_hash = await self._cs_store_bytes(folder, "crop", crop, max_bytes)
                with self._storage._connect() as conn:
                    conn.execute("UPDATE code_sends SET img_hash=?, crop_hash=? WHERE id=?",
                                 (img_hash, crop_hash, row_id))
                    if crop_hash:  # 长期保留一张缩略图（按人统计用）
                        conn.execute(
                            "INSERT OR REPLACE INTO code_thumbs(code_hash, thumb, ts) "
                            "SELECT ?, thumb, ? FROM code_images WHERE hash=? AND thumb != ''",
                            (self._cl_hash(t), int(time.time()), crop_hash))
                    conn.commit()

    async def _cs_store_bytes(self, folder: Path, kind: str, data: bytes, max_bytes: int) -> str:
        h = hashlib.sha1(data).hexdigest()
        now = int(time.time())
        self._cl_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT path FROM code_images WHERE hash=?", (h,)).fetchone()
            if row and Path(row["path"]).exists():
                conn.execute("UPDATE code_images SET last_used=? WHERE hash=?", (now, h))
                conn.commit()
                return h
        out, ext, size = await self._cs_offload(_compress_image, data, max_bytes)
        thumb = await self._cs_offload(_thumb, out) if kind == "crop" else ""
        path = folder / f"{h}.{ext}"
        await self._cs_offload(path.write_bytes, out)
        with self._storage._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO code_images(hash, kind, path, bytes, width, height, thumb, created_at, last_used) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (h, kind, str(path), len(out), size[0], size[1], thumb, now, now))
            conn.commit()
        return h

    def _cs_maybe_clean(self, now: Optional[int] = None) -> None:
        now = int(now or time.time())
        if now - int(getattr(self, "_cs_last_clean", 0) or 0) < self.CODE_STORE_CLEAN_INTERVAL:
            return
        self._cs_last_clean = now
        try:
            self._cs_clean(now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理码记录失败: {e}")

    def _cs_clean(self, now: Optional[int] = None) -> int:
        """删除超过保留时长（_cs_ttl）的记录，以及不再被引用的图片文件。返回删除的图片数。"""
        now = int(now or time.time())
        cutoff = now - self._cs_ttl()
        self._cl_init_db()
        removed = 0
        with self._storage._connect() as conn:
            conn.execute("DELETE FROM code_sends WHERE ts < ?", (cutoff,))
            stale = conn.execute(
                "SELECT hash, path FROM code_images WHERE last_used < ? AND hash NOT IN "
                "(SELECT img_hash FROM code_sends UNION SELECT crop_hash FROM code_sends)", (cutoff,)).fetchall()
            for row in stale:
                try:
                    os.remove(row["path"])
                except OSError:
                    pass
                conn.execute("DELETE FROM code_images WHERE hash=?", (row["hash"],))
                removed += 1
            conn.commit()
        return removed

    def _cs_read_image(self, h: str) -> str:
        """图片 → data URL（WebUI 详情里显示）。"""
        if not h:
            return ""
        with self._storage._connect() as conn:
            row = conn.execute("SELECT path FROM code_images WHERE hash=?", (str(h),)).fetchone()
        if not row:
            return ""
        try:
            data = Path(row["path"]).read_bytes()
        except OSError:
            return ""
        ext = Path(row["path"]).suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "application/octet-stream")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    # ------------------------------------------------------------------
    # 统计（WebUI「码管理」）
    # ------------------------------------------------------------------
    # 码统计的排序方式 → ORDER BY（只用这里的固定写法，不拼接用户输入）
    CS_SORTS = {
        "count": "total DESC, last_ts DESC",          # 出现次数从多到少
        "users": "users DESC, total DESC, last_ts DESC",  # 发送人数从多到少
        "recent": "last_ts DESC, total DESC",         # 最近出现的在前
    }

    def _cs_stats(self, hours: int = 0, keyword: str = "", group_id: str = "", by: str = "code",
                  page: int = 1, page_size: int = 50, sort: str = "count") -> Tuple[List[dict], int]:
        """窗口内按码（或按「码 + 人」）汇总。keyword 匹配码内容、QQ 号或昵称。
        sort：count 出现次数 / users 发送人数 / recent 最近出现（见 CS_SORTS），未知值按 count。"""
        self._cl_init_db()
        hours = int(hours or 0) or min(max(self._cfg_int("code_limit_window_hours", 5), 1), CODE_LIMIT_WINDOW_MAX_HOURS)
        since = int(time.time()) - min(hours, self._cs_ttl_hours()) * 3600
        where, args = ["s.ts>=?"], [since]
        if group_id:
            where.append("s.group_id=?")
            args.append(str(group_id))
        keyword = str(keyword or "").strip()
        if keyword:
            where.append("(s.code_text LIKE ? OR s.user_id=? OR s.user_name LIKE ?)")
            args += [f"%{keyword}%", keyword, f"%{keyword}%"]
        group_by = "s.code_hash, s.user_id" if by == "user" else "s.code_hash"
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 50), 1), 200)
        cond = " AND ".join(where)
        order = self.CS_SORTS.get(str(sort or ""), self.CS_SORTS["count"])
        with self._storage._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM code_sends s WHERE {cond} "
                                     f"GROUP BY {group_by})", args).fetchone()["c"] or 0)
            rows = conn.execute(
                f"SELECT s.code_hash, MAX(s.code_text) AS code_text, COUNT(*) AS total, "
                f"SUM(s.outcome='pass') AS passed, SUM(s.outcome='limit') AS limited, "
                f"SUM(s.outcome='rule') AS ruled, SUM(s.outcome='allow') AS allowed, "
                f"COUNT(DISTINCT s.group_id) AS groups, COUNT(DISTINCT s.user_id) AS users, "
                f"MAX(s.ts) AS last_ts, MIN(s.ts) AS first_ts, MAX(s.user_id) AS user_id, MAX(s.user_name) AS user_name, "
                f"MAX(CASE WHEN s.crop_hash != '' THEN s.crop_hash END) AS crop_hash "
                f"FROM code_sends s WHERE {cond} GROUP BY {group_by} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size]).fetchall()
            hashes = [r["crop_hash"] for r in rows if r["crop_hash"]]
            thumbs = {}
            if hashes:
                marks = ",".join("?" * len(hashes))
                for r in conn.execute(f"SELECT hash, thumb FROM code_images WHERE hash IN ({marks})", hashes):
                    thumbs[r["hash"]] = r["thumb"]
        allow = getattr(self, "_code_allowed", lambda _t: False)
        block = getattr(self, "_code_block_set", lambda: set())()
        items = []
        for r in rows:
            text = r["code_text"]
            items.append({
                "code_hash": r["code_hash"], "text": text, "total": int(r["total"] or 0),
                "passed": int(r["passed"] or 0), "limited": int(r["limited"] or 0), "ruled": int(r["ruled"] or 0),
                "allowed": int(r["allowed"] or 0), "groups": int(r["groups"] or 0), "users": int(r["users"] or 0),
                "first_ts": int(r["first_ts"] or 0), "last_ts": int(r["last_ts"] or 0),
                "user_id": r["user_id"] if by == "user" else "", "user_name": r["user_name"] if by == "user" else "",
                "thumb": thumbs.get(r["crop_hash"] or "", ""),
                "state": "allow" if allow(text) else ("block" if text in block else ""),
            })
        return items, total

    def _cs_detail(self, code_hash: str, user_id: str = "", limit: int = 50) -> dict:
        """某个码最近的发送记录和图片（同一张图只给一次）。"""
        self._cl_init_db()
        where, args = ["code_hash=?"], [str(code_hash)]
        if user_id:
            where.append("user_id=?")
            args.append(str(user_id))
        with self._storage._connect() as conn:
            rows = conn.execute(f"SELECT * FROM code_sends WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
                                args + [min(max(int(limit), 1), 200)]).fetchall()
        sends, images, seen = [], [], set()
        for r in rows:
            sends.append({"ts": r["ts"], "group_id": r["group_id"], "user_id": r["user_id"], "user_name": r["user_name"],
                          "outcome": r["outcome"], "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"]),
                          "has_image": bool(r["img_hash"])})
            key = (r["img_hash"], r["crop_hash"])
            if (r["img_hash"] or r["crop_hash"]) and key not in seen and len(images) < 8:
                seen.add(key)
                images.append({"ts": r["ts"], "group_id": r["group_id"], "user_id": r["user_id"],
                               "orig": self._cs_read_image(r["img_hash"]), "crop": self._cs_read_image(r["crop_hash"])})
        text = rows[0]["code_text"] if rows else ""
        return {"code_hash": code_hash, "text": text, "sends": sends, "images": images,
                "ttl_hours": self._cs_ttl_hours()}

    # ------------------------------------------------------------------
    # 按人长期累计（WebUI「码管理 → 按人统计」）
    # ------------------------------------------------------------------
    # 排序方式 → ORDER BY（固定写法，不拼接用户输入）
    CU_SORTS = {
        "codes": "codes DESC, total DESC, last_ts DESC",   # 发过的不同码最多
        "total": "total DESC, codes DESC, last_ts DESC",   # 发码总次数最多
        "bad": "bad DESC, total DESC, last_ts DESC",       # 被撤回（规则 + 超限）最多
        "recent": "last_ts DESC, total DESC",              # 最近发过码
    }

    def _cu_users(self, keyword: str = "", group_id: str = "", sort: str = "codes",
                  page: int = 1, page_size: int = 50) -> Tuple[List[dict], int]:
        """按人汇总（全部历史）：发过几个不同的码、总次数、被撤回次数、涉及几个群。
        keyword 匹配 QQ 号（完全一致）、昵称或码内容（包含）。"""
        self._cl_init_db()
        where, args = ["1=1"], []
        if group_id:
            where.append("group_id=?")
            args.append(str(group_id))
        keyword = str(keyword or "").strip()
        if keyword:
            where.append("(user_id=? OR user_name LIKE ? OR code_text LIKE ?)")
            args += [keyword, f"%{keyword}%", f"%{keyword}%"]
        cond = " AND ".join(where)
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 50), 1), 200)
        order = self.CU_SORTS.get(str(sort or ""), self.CU_SORTS["codes"])
        with self._storage._connect() as conn:
            # 关键词按码内容命中时，只用来挑出「发过这个码的人」，汇总仍按此人全部的码计算
            users_sql = f"SELECT DISTINCT user_id FROM code_user_stats WHERE {cond}"
            base_where = "user_id IN (" + users_sql + ")" + (" AND group_id=?" if group_id else "")
            base_args = args + ([str(group_id)] if group_id else [])
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM ({users_sql})", args).fetchone()["c"] or 0)
            rows = conn.execute(
                f"SELECT user_id, MAX(user_name) AS user_name, COUNT(DISTINCT code_hash) AS codes, "
                f"SUM(total) AS total, SUM(passed) AS passed, SUM(limited) AS limited, SUM(ruled) AS ruled, "
                f"SUM(allowed) AS allowed, SUM(limited) + SUM(ruled) AS bad, COUNT(DISTINCT group_id) AS groups, "
                f"MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts "
                f"FROM code_user_stats WHERE {base_where} GROUP BY user_id ORDER BY {order} LIMIT ? OFFSET ?",
                base_args + [page_size, (page - 1) * page_size]).fetchall()
            # 昵称取最近一次发码时的
            names = {}
            ids = [r["user_id"] for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                for r in conn.execute(
                        f"SELECT user_id, user_name FROM code_user_stats WHERE user_id IN ({marks}) AND user_name != '' "
                        f"ORDER BY last_ts ASC", ids):
                    names[r["user_id"]] = r["user_name"]
        items = []
        for r in rows:
            items.append({
                "user_id": r["user_id"], "user_name": names.get(r["user_id"], r["user_name"] or ""),
                "codes": int(r["codes"] or 0), "total": int(r["total"] or 0), "passed": int(r["passed"] or 0),
                "limited": int(r["limited"] or 0), "ruled": int(r["ruled"] or 0), "allowed": int(r["allowed"] or 0),
                "groups": int(r["groups"] or 0), "first_ts": int(r["first_ts"] or 0), "last_ts": int(r["last_ts"] or 0),
            })
        return items, total

    def _cu_user_detail(self, user_id: str, limit: int = 500) -> dict:
        """某个人发过的全部码（全部历史）：每个码的次数、处理结果、所在的群、首次 / 最近时间、缩略图。"""
        self._cl_init_db()
        user_id = str(user_id or "").strip()
        with self._storage._connect() as conn:
            rows = conn.execute(
                "SELECT code_hash, MAX(code_text) AS code_text, SUM(total) AS total, SUM(passed) AS passed, "
                "SUM(limited) AS limited, SUM(ruled) AS ruled, SUM(allowed) AS allowed, "
                "GROUP_CONCAT(group_id) AS group_ids, MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts "
                "FROM code_user_stats WHERE user_id=? GROUP BY code_hash ORDER BY last_ts DESC LIMIT ?",
                (user_id, min(max(int(limit), 1), 2000))).fetchall()
            name_row = conn.execute(
                "SELECT user_name FROM code_user_stats WHERE user_id=? AND user_name != '' ORDER BY last_ts DESC LIMIT 1",
                (user_id,)).fetchone()
            hashes = [r["code_hash"] for r in rows]
            thumbs = {}
            if hashes:
                marks = ",".join("?" * len(hashes))
                for r in conn.execute(f"SELECT code_hash, thumb FROM code_thumbs WHERE code_hash IN ({marks})", hashes):
                    thumbs[r["code_hash"]] = r["thumb"]
            per_group = conn.execute(
                "SELECT group_id, SUM(total) AS total, COUNT(DISTINCT code_hash) AS codes, MAX(last_ts) AS last_ts "
                "FROM code_user_stats WHERE user_id=? GROUP BY group_id ORDER BY total DESC", (user_id,)).fetchall()
        allow = getattr(self, "_code_allowed", lambda _t: False)
        block = getattr(self, "_code_block_set", lambda: set())()
        codes = []
        for r in rows:
            text = r["code_text"]
            codes.append({
                "code_hash": r["code_hash"], "text": text, "total": int(r["total"] or 0),
                "passed": int(r["passed"] or 0), "limited": int(r["limited"] or 0), "ruled": int(r["ruled"] or 0),
                "allowed": int(r["allowed"] or 0),
                "group_ids": sorted({g for g in str(r["group_ids"] or "").split(",") if g}),
                "first_ts": int(r["first_ts"] or 0), "last_ts": int(r["last_ts"] or 0),
                "thumb": thumbs.get(r["code_hash"], ""),
                "state": "allow" if allow(text) else ("block" if text in block else ""),
            })
        return {
            "user_id": user_id, "user_name": name_row["user_name"] if name_row else "",
            "codes": codes, "code_count": len(codes), "total": sum(c["total"] for c in codes),
            "groups": [{"group_id": g["group_id"], "total": int(g["total"] or 0), "codes": int(g["codes"] or 0),
                        "last_ts": int(g["last_ts"] or 0)} for g in per_group],
        }

    # ------------------------------------------------------------------
    # 大屏（WebUI「码大屏」）：时段汇总、按小时走势、排行
    # ------------------------------------------------------------------
    def _cs_screen(self, hours: int = 24, top: int = 10, now: Optional[int] = None) -> dict:
        """最近 hours 小时（不超过记录保留时长）的发码汇总；另附全部历史的累计人数 / 码数 / 次数。"""
        self._cl_init_db()
        now = int(now or time.time())
        ttl = self._cs_ttl_hours()
        hours = min(max(int(hours or 24), 1), ttl)
        since = now - hours * 3600
        top = min(max(int(top or 10), 1), 50)
        bad = "SUM(outcome IN ('limit','rule'))"
        with self._storage._connect() as conn:
            t = conn.execute(
                f"SELECT COUNT(*) AS sends, COUNT(DISTINCT code_hash) AS codes, COUNT(DISTINCT user_id) AS users, "
                f"COUNT(DISTINCT group_id) AS groups, {bad} AS bad, SUM(outcome='allow') AS allowed "
                f"FROM code_sends WHERE ts>=?", (since,)).fetchone()
            # 按小时：桶的起点对齐到整点（本地时间由前端换算显示）
            first_bucket = since - since % 3600
            buckets = {}
            for r in conn.execute(
                    f"SELECT (ts - ts % 3600) AS b, COUNT(*) AS c, {bad} AS bad FROM code_sends WHERE ts>=? GROUP BY b",
                    (since,)):
                buckets[int(r["b"])] = (int(r["c"] or 0), int(r["bad"] or 0))
            series = []
            b = first_bucket
            while b <= now:
                c, x = buckets.get(b, (0, 0))
                series.append({"ts": b, "sends": c, "bad": x})
                b += 3600
            top_codes = conn.execute(
                f"SELECT code_hash, MAX(code_text) AS text, COUNT(*) AS total, COUNT(DISTINCT user_id) AS users, "
                f"COUNT(DISTINCT group_id) AS groups, {bad} AS bad, MAX(ts) AS last_ts FROM code_sends WHERE ts>=? "
                f"GROUP BY code_hash ORDER BY total DESC, last_ts DESC LIMIT ?", (since, top)).fetchall()
            top_users = conn.execute(
                f"SELECT user_id, MAX(user_name) AS user_name, COUNT(*) AS total, COUNT(DISTINCT code_hash) AS codes, "
                f"COUNT(DISTINCT group_id) AS groups, {bad} AS bad, MAX(ts) AS last_ts FROM code_sends WHERE ts>=? "
                f"GROUP BY user_id ORDER BY total DESC, last_ts DESC LIMIT ?", (since, top)).fetchall()
            top_groups = conn.execute(
                f"SELECT group_id, COUNT(*) AS total, COUNT(DISTINCT code_hash) AS codes, COUNT(DISTINCT user_id) AS users, "
                f"{bad} AS bad FROM code_sends WHERE ts>=? GROUP BY group_id ORDER BY total DESC LIMIT ?",
                (since, top)).fetchall()
            hashes = [r["code_hash"] for r in top_codes]
            thumbs = {}
            if hashes:
                marks = ",".join("?" * len(hashes))
                for r in conn.execute(f"SELECT code_hash, thumb FROM code_thumbs WHERE code_hash IN ({marks})", hashes):
                    thumbs[r["code_hash"]] = r["thumb"]
            life = conn.execute("SELECT COUNT(DISTINCT user_id) AS users, COUNT(DISTINCT code_hash) AS codes, "
                                "SUM(total) AS sends FROM code_user_stats").fetchone()
        allow = getattr(self, "_code_allowed", lambda _t: False)
        block = getattr(self, "_code_block_set", lambda: set())()

        def state(text):
            return "allow" if allow(text) else ("block" if text in block else "")

        return {
            "hours": hours, "ttl_hours": ttl, "now": now,
            "totals": {"sends": int(t["sends"] or 0), "codes": int(t["codes"] or 0), "users": int(t["users"] or 0),
                       "groups": int(t["groups"] or 0), "bad": int(t["bad"] or 0), "allowed": int(t["allowed"] or 0)},
            "lifetime": {"users": int(life["users"] or 0), "codes": int(life["codes"] or 0),
                         "sends": int(life["sends"] or 0)},
            "series": series,
            "top_codes": [{"code_hash": r["code_hash"], "text": r["text"], "total": int(r["total"] or 0),
                           "users": int(r["users"] or 0), "groups": int(r["groups"] or 0), "bad": int(r["bad"] or 0),
                           "last_ts": int(r["last_ts"] or 0), "thumb": thumbs.get(r["code_hash"], ""),
                           "state": state(r["text"])} for r in top_codes],
            "top_users": [{"user_id": r["user_id"], "user_name": r["user_name"] or "", "total": int(r["total"] or 0),
                           "codes": int(r["codes"] or 0), "groups": int(r["groups"] or 0), "bad": int(r["bad"] or 0),
                           "last_ts": int(r["last_ts"] or 0)} for r in top_users],
            "top_groups": [{"group_id": r["group_id"], "total": int(r["total"] or 0), "codes": int(r["codes"] or 0),
                            "users": int(r["users"] or 0), "bad": int(r["bad"] or 0)} for r in top_groups],
        }
