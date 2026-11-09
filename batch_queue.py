# -*- coding: utf-8 -*-
"""批量任务队列（二次开发新增）。

所有「对多个群 / 多个人」的写操作都先变成一个批量任务，任务里每一项是「哪个群、做什么、对谁」：
  跨群踢人 / 踢黑、/批量踢黑、重复成员处理、全群公告 / 待办、群名后缀、群名倒计时（设置 / 每日更新 / 立即更新）、
  定时改群名。
后台执行器全局同一时间只执行一项；两项之间等待「间隔 + 0~抖动 的随机值」（batch_interval_seconds /
batch_jitter_seconds，单个任务可单独设置），降低协议端风控。

执行方式：
  auto    按间隔自动执行
  manual  不自动执行，在 WebUI「批量任务」里手动「执行下一项」/「执行这一项」/「全部执行」（转为自动）
任务可暂停、继续、取消剩余项、重试失败项；每一项可单独执行或跳过。

存储：SQLite 表 batch_jobs / batch_items（插件重启后保留）。重启时「执行中」的项标记为失败（可重试），
不会自动重做（例如避免重复踢人）。

执行器（_bq_exec_<action>）复用各模块的单群函数，操作记录、黑名单等行为与原来一致：
  kick    群 target 踢出 QQ（params: block, reason, log_action）
  notice  发群公告（params: content, options, image, todo）
  rename  改群名（params: op = suffix / countdown_set / countdown_update，见 _bq_exec_rename）
  delfile / sendmsg / upfile  删除群文件 / 批量发言 / 批量传群文件（group_files.py）
"""
import asyncio
import json
import random
import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_OK = "ok"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"
ITEM_CANCELLED = "cancelled"

JOB_ACTIVE = "active"
JOB_PAUSED = "paused"
JOB_DONE = "done"
JOB_CANCELLED = "cancelled"

MODE_AUTO = "auto"
MODE_MANUAL = "manual"


class BatchQueueMixin:
    BQ_ITEM_TIMEOUT = 180
    BQ_MAX_ITEMS = 5000
    # 创建说明（meta["note"]）的上限：任务列表每次轮询都会带上，不宜过大
    BQ_NOTE_MAX_LINES = 200
    BQ_NOTE_LINE_MAX = 300
    BQ_KEEP_JOBS = 300
    BQ_IDLE_POLL = 5.0

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _bq_init_db(self) -> None:
        if getattr(self, "_bq_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS batch_jobs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, title TEXT NOT NULL, "
                "kind TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', operator TEXT NOT NULL DEFAULT '', "
                "mode TEXT NOT NULL DEFAULT 'auto', state TEXT NOT NULL DEFAULT 'active', "
                "interval_s REAL, jitter_s REAL, meta TEXT NOT NULL DEFAULT '{}', finished_at INTEGER)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS batch_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, seq INTEGER NOT NULL, "
                "group_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', "
                "target TEXT NOT NULL DEFAULT '', params TEXT NOT NULL DEFAULT '{}', "
                "status TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT '', "
                "started_at INTEGER, finished_at INTEGER)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_items_job ON batch_items(job_id, seq)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_items_status ON batch_items(status)")
            # 上次运行时正在执行的项：结果未知，标记失败（可重试），不自动重做
            conn.execute(
                "UPDATE batch_items SET status=?, result=?, finished_at=? WHERE status=?",
                (ITEM_FAILED, "插件重启时中断，结果未知（可重试）", int(time.time()), ITEM_RUNNING),
            )
            conn.commit()
        self._bq_db_ready = True

    def _bq_interval_settings(self, job: Optional[dict] = None) -> Tuple[float, float]:
        interval = job.get("interval_s") if job else None
        jitter = job.get("jitter_s") if job else None
        if interval is None:
            interval = self._cfg_int("batch_interval_seconds", 3)
        if jitter is None:
            jitter = self._cfg_int("batch_jitter_seconds", 2)
        return max(0.0, float(interval)), max(0.0, float(jitter))

    def _bq_default_mode(self) -> str:
        mode = str(self._cfg_str("batch_default_mode", MODE_AUTO) or MODE_AUTO).strip().lower()
        return mode if mode in (MODE_AUTO, MODE_MANUAL) else MODE_AUTO

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------
    def _bq_create_job(self, title: str, kind: str, items: List[dict], source: str = "", operator: str = "",
                       mode: Optional[str] = None, meta: Optional[dict] = None,
                       interval: Optional[float] = None, jitter: Optional[float] = None) -> dict:
        """items: [{group_id, action, label, target, params}]。返回任务摘要（含 id、项数、mode）。"""
        self._bq_init_db()
        items = list(items or [])[: self.BQ_MAX_ITEMS]
        fill = getattr(self, "_op_fill_operator", None)
        if callable(fill):  # WebUI 创建的任务记下是谁建的；每项执行时的操作记录也带上
            operator = fill(source, operator)
            for it in items:
                params = it.get("params")
                if isinstance(params, dict) and not params.get("operator"):
                    filled = fill(params.get("source", ""), "")
                    if filled:
                        params["operator"] = filled
        mode = mode if mode in (MODE_AUTO, MODE_MANUAL) else self._bq_default_mode()
        now = int(time.time())
        with self._storage._connect() as conn:
            cur = conn.execute(
                "INSERT INTO batch_jobs(created_at, title, kind, source, operator, mode, state, interval_s, jitter_s, meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, str(title)[:200], kind, source or "", str(operator or ""), mode,
                 JOB_ACTIVE if items else JOB_DONE,
                 None if interval is None else float(interval), None if jitter is None else float(jitter),
                 json.dumps(meta or {}, ensure_ascii=False)),
            )
            job_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO batch_items(job_id, seq, group_id, action, label, target, params) VALUES(?,?,?,?,?,?,?)",
                [(job_id, i + 1, str(it.get("group_id") or ""), str(it["action"]), str(it.get("label") or "")[:120],
                  str(it.get("target") or ""), json.dumps(it.get("params") or {}, ensure_ascii=False))
                 for i, it in enumerate(items)],
            )
            if not items:
                conn.execute("UPDATE batch_jobs SET finished_at=? WHERE id=?", (now, job_id))
            conn.commit()
        self._bq_cleanup()
        self._record_op("创建批量任务", source=source or "", operator=str(operator or ""),
                        detail=f"#{job_id} {title}（{len(items)} 项，{'自动' if mode == MODE_AUTO else '手动'}执行）")
        self._bq_wake()
        return {"id": job_id, "title": title, "items": len(items), "mode": mode, "eta_seconds": self._bq_eta(len(items))}

    @staticmethod
    def _bq_fmt_seconds(seconds: float) -> str:
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds} 秒"
        if seconds < 3600:
            return f"{seconds // 60} 分 {seconds % 60} 秒"
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"

    def _bq_created_text(self, job: dict) -> str:
        """指令回复：任务已创建。"""
        if not job.get("items"):
            return f"批量任务 #{job['id']}「{job['title']}」没有需要执行的项"
        interval, jitter = self._bq_interval_settings()
        text = (f"已创建批量任务 #{job['id']}「{job['title']}」，共 {job['items']} 项。\n"
                f"间隔 {interval:g}" + (f"–{interval + jitter:g}" if jitter else "") + " 秒")
        if job["mode"] == MODE_AUTO:
            text += f"，自动执行，预计约 {self._bq_fmt_seconds(job['eta_seconds'])}。"
            if self._cfg("batch_notify_enabled", True):
                text += "完成后在本群发送结果。"
        else:
            text += "，等待在 WebUI「批量任务」页面手动执行。"
        return text

    async def _bq_kick_job(self, client, targets: List[str], block: bool, groups: Optional[List[str]] = None,
                           reason: str = "", source: str = "", operator: str = "", notify_group: str = "",
                           title: str = "", log_action: str = "") -> Tuple[Optional[dict], str, dict]:
        """跨群踢人 / 踢黑：每个「群 × QQ」一项。返回 (任务, 错误, 附加信息)。

        拉黑在创建任务时立即生效（之后的加群申请、入群都会被拦）；本地缓存确认不在某群的人不建项。
        """
        info = {"protected": [t for t in targets if self._is_blacklist_exempt(t)], "blacklisted": -1, "absent": 0}
        targets = [t for t in targets if t not in info["protected"]][: self.KICK_TARGETS_MAX]
        if not targets:
            return None, "没有可处理的 QQ 号（插件管理员 / 白名单用户不会被处理）", info
        if groups is None:
            groups, err = await self._list_managed_groups(client)
            if not groups:
                return None, err or "没有可管理的群", info
        else:
            groups = [g for g in groups if self._group_id_allowed(g)]
            if not groups:
                return None, "所选的群都不在管理范围内", info
        if block:
            info["blacklisted"] = sum(1 for t in targets if self._add_user_blacklist(
                t, reason or "踢出并拉黑", source=source, operator=operator))
        cached = {}
        try:
            cached = self._dir()["members"]
        except Exception:
            cached = {}
        params = {"block": bool(block), "reason": reason, "source": source, "operator": operator}
        if log_action:
            params["log_action"] = log_action
        label = "踢出并拉黑" if block else "踢出"
        items = []
        for gid in groups:
            members = cached.get(str(gid))
            for uid in targets:
                if members is not None and str(uid) not in members:
                    info["absent"] += 1
                    continue
                items.append({"group_id": gid, "action": "kick", "label": label, "target": uid, "params": params})
        if not items:
            return None, "按本地群成员缓存，这些 QQ 号不在任何管理的群里（可先刷新群成员缓存）", info
        names = "、".join(targets[:3]) + (f" 等 {len(targets)} 人" if len(targets) > 3 else "")
        job = self._bq_create_job(title or f"{label} {names}" + (f"（{reason}）" if reason else ""), "kick", items,
                                  source=source, operator=operator, meta={"notify_group": notify_group})
        return job, "", info

    def _bq_eta(self, count: int, job: Optional[dict] = None) -> int:
        interval, jitter = self._bq_interval_settings(job)
        return int(max(0, count - 1) * (interval + jitter / 2))

    def _bq_cleanup(self) -> None:
        try:
            with self._storage._connect() as conn:
                old = [r[0] for r in conn.execute(
                    "SELECT id FROM batch_jobs WHERE state IN (?, ?) ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (JOB_DONE, JOB_CANCELLED, self.BQ_KEEP_JOBS))]
                if old:
                    marks = ",".join("?" * len(old))
                    conn.execute(f"DELETE FROM batch_items WHERE job_id IN ({marks})", old)
                    conn.execute(f"DELETE FROM batch_jobs WHERE id IN ({marks})", old)
                    conn.commit()
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理批量任务失败: {e}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @staticmethod
    def _bq_row_job(row) -> dict:
        job = dict(row)
        try:
            job["meta"] = json.loads(job.get("meta") or "{}")
        except Exception:
            job["meta"] = {}
        return job

    def _bq_counts(self, conn, job_ids: List[int]) -> Dict[int, Dict[str, int]]:
        if not job_ids:
            return {}
        marks = ",".join("?" * len(job_ids))
        out: Dict[int, Dict[str, int]] = {jid: {} for jid in job_ids}
        for r in conn.execute(
                f"SELECT job_id, status, COUNT(*) AS n FROM batch_items WHERE job_id IN ({marks}) GROUP BY job_id, status",
                job_ids):
            out[r["job_id"]][r["status"]] = r["n"]
        return out

    def _bq_jobs(self, limit: int = 30, offset: int = 0) -> Tuple[List[dict], int]:
        self._bq_init_db()
        limit = max(1, min(int(limit or 30), 100))
        with self._storage._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM batch_jobs").fetchone()[0]
            rows = [self._bq_row_job(r) for r in conn.execute(
                "SELECT * FROM batch_jobs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, max(0, int(offset or 0))))]
            counts = self._bq_counts(conn, [r["id"] for r in rows])
        for job in rows:
            c = counts.get(job["id"], {})
            job["counts"] = c
            job["total"] = sum(c.values())
            job["interval_effective"], job["jitter_effective"] = self._bq_interval_settings(job)
        return rows, int(total)

    def _bq_job(self, job_id: int) -> Optional[dict]:
        self._bq_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not row:
                return None
            job = self._bq_row_job(row)
            items = []
            for r in conn.execute("SELECT * FROM batch_items WHERE job_id=? ORDER BY seq", (job["id"],)):
                it = dict(r)
                try:
                    it["params"] = json.loads(it.get("params") or "{}")
                except Exception:
                    it["params"] = {}
                items.append(it)
            counts = self._bq_counts(conn, [job["id"]]).get(job["id"], {})
        job["items"] = items
        job["counts"] = counts
        job["total"] = sum(counts.values())
        job["interval_effective"], job["jitter_effective"] = self._bq_interval_settings(job)
        return job

    BQ_PAGE_STATUSES = ("pending", "running", "ok", "failed", "skipped", "cancelled")

    def _bq_job_page(self, job_id: int, status: str = "", page: int = 0, page_size: int = 100) -> Optional[dict]:
        """WebUI 明细：只取一页（几千项的任务每 2 秒轮询一次，不能每次全量）。
        status 过滤某种状态；page=0 且不过滤时取「当前项」（执行中的，否则第一个待执行的）所在的页。"""
        self._bq_init_db()
        page_size = min(max(int(page_size or 100), 20), 500)
        with self._storage._connect() as conn:
            row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not row:
                return None
            job = self._bq_row_job(row)
            counts = self._bq_counts(conn, [job["id"]]).get(job["id"], {})
            where, args = "job_id=?", [job["id"]]
            if status in self.BQ_PAGE_STATUSES:
                where += " AND status=?"
                args.append(status)
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM batch_items WHERE {where}", args).fetchone()["c"])
            cur = conn.execute("SELECT id, seq FROM batch_items WHERE job_id=? AND status='running' ORDER BY seq LIMIT 1",
                               (job["id"],)).fetchone() or \
                conn.execute("SELECT id, seq FROM batch_items WHERE job_id=? AND status='pending' ORDER BY seq LIMIT 1",
                             (job["id"],)).fetchone()
            pages = max(1, (total + page_size - 1) // page_size)
            if int(page or 0) <= 0:
                page = 1
                if cur is not None and not status:
                    before = conn.execute("SELECT COUNT(*) AS c FROM batch_items WHERE job_id=? AND seq<?",
                                          (job["id"], cur["seq"])).fetchone()["c"]
                    page = before // page_size + 1
            page = min(max(int(page), 1), pages)
            items = []
            for r in conn.execute(f"SELECT * FROM batch_items WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
                                  args + [page_size, (page - 1) * page_size]):
                it = dict(r)
                it.pop("params", None)  # 公告全文等不必每次轮询都传
                items.append(it)
        job.update(items=items, counts=counts, total=sum(counts.values()), filtered_total=total, page=page,
                   page_size=page_size, pages=pages, status_filter=status if status in self.BQ_PAGE_STATUSES else "",
                   current_id=cur["id"] if cur is not None else None)
        job["interval_effective"], job["jitter_effective"] = self._bq_interval_settings(job)
        return job

    def _bq_status(self) -> dict:
        st = dict(getattr(self, "_bq_state", None) or {"state": "idle"})
        if st.get("next_at"):
            st["wait_seconds"] = max(0, round(st["next_at"] - time.time(), 1))
        return st

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    def _bq_set_state(self, job_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._storage._connect() as conn:
            conn.execute(f"UPDATE batch_jobs SET {sets} WHERE id=?", list(fields.values()) + [int(job_id)])
            conn.commit()

    def _bq_control(self, job_id: int, action: str, operator: str = "", source: str = "WebUI") -> str:
        """pause / resume / cancel / retry / auto / manual。返回错误说明，成功返回空串。"""
        job = self._bq_job(job_id)
        if not job:
            return "任务不存在"
        jid = job["id"]
        now = int(time.time())
        if action == "pause":
            if job["state"] != JOB_ACTIVE:
                return "任务不在执行中"
            self._bq_set_state(jid, state=JOB_PAUSED)
        elif action == "resume":
            if job["state"] != JOB_PAUSED:
                return "任务没有暂停"
            self._bq_set_state(jid, state=JOB_ACTIVE)
        elif action in ("auto", "manual"):
            self._bq_set_state(jid, mode=MODE_AUTO if action == "auto" else MODE_MANUAL)
            if action == "auto" and job["state"] == JOB_PAUSED:
                self._bq_set_state(jid, state=JOB_ACTIVE)
        elif action == "cancel":
            with self._storage._connect() as conn:
                conn.execute("UPDATE batch_items SET status=?, result=?, finished_at=? WHERE job_id=? AND status=?",
                             (ITEM_CANCELLED, "已取消", now, jid, ITEM_PENDING))
                conn.commit()
            self._bq_finish_if_done(jid, cancelled=True)
        elif action == "retry":
            with self._storage._connect() as conn:
                n = conn.execute("UPDATE batch_items SET status=?, result='', started_at=NULL, finished_at=NULL "
                                 "WHERE job_id=? AND status=?", (ITEM_PENDING, jid, ITEM_FAILED)).rowcount
                conn.commit()
            if not n:
                return "没有失败的项"
            self._bq_set_state(jid, state=JOB_ACTIVE, finished_at=None)
        else:
            return f"未知操作 {action}"
        labels = {"pause": "暂停", "resume": "继续", "cancel": "取消", "retry": "重试失败项",
                  "auto": "改为自动执行", "manual": "改为手动执行"}
        self._record_op("批量任务" + labels.get(action, action), source=source, operator=operator,
                        detail=f"#{jid} {job['title']}")
        self._bq_wake()
        return ""

    def _bq_set_note(self, job_id: int, lines) -> str:
        """保存创建任务时页面上显示的说明（每人 / 每群的处理计划等），「批量任务」页可随时查看。

        存在 meta["note"]，最多 BQ_NOTE_MAX_LINES 行、每行 BQ_NOTE_LINE_MAX 字。返回错误说明，成功返回空串。
        """
        if not isinstance(lines, (list, tuple)):
            lines = str(lines or "").splitlines()
        clean = [str(x)[: self.BQ_NOTE_LINE_MAX] for x in lines if str(x).strip()][: self.BQ_NOTE_MAX_LINES]
        self._bq_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT meta FROM batch_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not row:
                return "任务不存在"
            try:
                meta = json.loads(row["meta"] or "{}")
            except Exception:
                meta = {}
            meta["note"] = clean
            conn.execute("UPDATE batch_jobs SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), int(job_id)))
            conn.commit()
        return ""

    def _bq_skip_item(self, item_id: int, operator: str = "") -> str:
        with self._storage._connect() as conn:
            row = conn.execute("SELECT job_id, status FROM batch_items WHERE id=?", (int(item_id),)).fetchone()
            if not row:
                return "任务项不存在"
            if row["status"] not in (ITEM_PENDING, ITEM_FAILED):
                return "只能跳过待执行或失败的项"
            conn.execute("UPDATE batch_items SET status=?, result=?, finished_at=? WHERE id=?",
                         (ITEM_SKIPPED, "手动跳过", int(time.time()), int(item_id)))
            conn.commit()
        self._bq_finish_if_done(row["job_id"])
        return ""

    def _bq_update_settings(self, job_id: int, interval, jitter) -> str:
        def norm(v):
            if v is None or v == "":
                return None
            v = float(v)
            if v < 0 or v > 3600:
                raise ValueError
            return v
        try:
            interval, jitter = norm(interval), norm(jitter)
        except (TypeError, ValueError):
            return "间隔和抖动需为 0–3600 秒"
        if not self._bq_job(job_id):
            return "任务不存在"
        self._bq_set_state(job_id, interval_s=interval, jitter_s=jitter)
        self._bq_wake()
        return ""

    def _bq_wake(self) -> None:
        event = getattr(self, "_bq_event", None)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def _bq_exec_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_bq_lock", None)
        if lock is None:
            lock = self._bq_lock = asyncio.Lock()
        return lock

    def _bq_next_item(self, job_id: Optional[int] = None, item_id: Optional[int] = None,
                      auto_only: bool = True) -> Optional[dict]:
        self._bq_init_db()
        with self._storage._connect() as conn:
            if item_id is not None:
                row = conn.execute("SELECT i.*, j.mode, j.state FROM batch_items i JOIN batch_jobs j ON j.id=i.job_id "
                                   "WHERE i.id=?", (int(item_id),)).fetchone()
            elif job_id is not None:
                row = conn.execute("SELECT i.*, j.mode, j.state FROM batch_items i JOIN batch_jobs j ON j.id=i.job_id "
                                   "WHERE i.job_id=? AND i.status=? ORDER BY i.seq LIMIT 1",
                                   (int(job_id), ITEM_PENDING)).fetchone()
            else:
                row = conn.execute(
                    "SELECT i.*, j.mode, j.state FROM batch_items i JOIN batch_jobs j ON j.id=i.job_id "
                    "WHERE i.status=? AND j.state=? " + ("AND j.mode='auto' " if auto_only else "") +
                    "ORDER BY j.id, i.seq LIMIT 1", (ITEM_PENDING, JOB_ACTIVE)).fetchone()
        return dict(row) if row else None

    async def _bq_execute(self, item: dict) -> Tuple[str, str]:
        """执行一项，返回 (状态, 说明)。"""
        client = await self._get_client()
        if not client:
            return "", "无法获取QQ客户端"
        executor = getattr(self, f"_bq_exec_{item['action']}", None)
        if not callable(executor):
            return ITEM_FAILED, f"未知的操作类型 {item['action']}"
        params = item.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params or "{}")
            except Exception:
                params = {}
        # 单项超时：执行器可以用 BQ_TIMEOUT_<动作> 单独放宽（例如上传群文件）
        timeout = getattr(self, f"BQ_TIMEOUT_{str(item['action']).upper()}", self.BQ_ITEM_TIMEOUT)
        try:
            status, message = await asyncio.wait_for(
                executor(client, str(item.get("group_id") or ""), str(item.get("target") or ""), params or {}),
                timeout=timeout)
        except asyncio.TimeoutError:
            status, message = ITEM_FAILED, f"执行超时（{timeout} 秒）"
        except Exception as e:
            logger.warning(f"[GroupMgr] 批量任务项执行出错 #{item.get('id')}: {e}")
            status, message = ITEM_FAILED, f"出错：{type(e).__name__}: {e}"
        return status, str(message or "")[:500]

    async def _bq_run_item(self, item: dict) -> Tuple[str, str]:
        """在执行锁内执行一项并写回结果。返回 (状态, 说明)；客户端不可用时项保持待执行。"""
        iid = item["id"]
        with self._storage._connect() as conn:
            changed = conn.execute("UPDATE batch_items SET status=?, started_at=? WHERE id=? AND status=?",
                                   (ITEM_RUNNING, int(time.time()), iid, ITEM_PENDING)).rowcount
            conn.commit()
        if not changed:
            return "", "该项已不是待执行状态"
        self._bq_state = {"state": "running", "job_id": item["job_id"], "item_id": iid,
                          "label": item.get("label", ""), "group_id": item.get("group_id", "")}
        status, message = await self._bq_execute(item)
        if not status:  # 没有客户端：放回队列，稍后再试
            with self._storage._connect() as conn:
                conn.execute("UPDATE batch_items SET status=?, started_at=NULL WHERE id=?", (ITEM_PENDING, iid))
                conn.commit()
            self._bq_state = {"state": "waiting_client", "message": message}
            return "", message
        with self._storage._connect() as conn:
            conn.execute("UPDATE batch_items SET status=?, result=?, finished_at=? WHERE id=?",
                         (status, message, int(time.time()), iid))
            conn.commit()
        self._bq_last_done = time.time()
        self._bq_next_delay = None
        self._bq_state = {"state": "idle"}
        await self._bq_finish_if_done_async(item["job_id"])
        return status, message

    async def _bq_run_now(self, job_id: Optional[int] = None, item_id: Optional[int] = None) -> Tuple[str, str]:
        """手动执行（不等待间隔）：指定项，或某任务的下一项。"""
        async with self._bq_exec_lock():
            item = self._bq_next_item(job_id=job_id, item_id=item_id, auto_only=False)
            if not item:
                return "", "没有待执行的项"
            if item["status"] != ITEM_PENDING:
                return "", "该项已不是待执行状态"
            if item["state"] in (JOB_DONE, JOB_CANCELLED):
                return "", "任务已结束"
            return await self._bq_run_item(item)

    async def _bq_drain(self, job_id: Optional[int] = None, limit: int = 100000) -> int:
        """不等待间隔，依次执行（测试与「立即全部执行」用）。返回执行的项数。"""
        count = 0
        while count < limit:
            async with self._bq_exec_lock():
                item = self._bq_next_item(job_id=job_id, auto_only=False) if job_id is not None else \
                    self._bq_next_item(auto_only=False)
                if not item:
                    break
                status, _msg = await self._bq_run_item(item)
                if not status:
                    break
            count += 1
        return count

    async def _bq_tick(self) -> bool:
        """自动执行一步：到了间隔就执行下一项。返回是否执行了。"""
        if not self.config.get("disclaimer_agreed", False) or not self._cfg("enabled"):
            self._bq_state = {"state": "disabled"}
            return False
        async with self._bq_exec_lock():
            item = self._bq_next_item()
            if not item:
                self._bq_state = {"state": "idle"}
                self._bq_next_delay = None
                return False
            last = getattr(self, "_bq_last_done", 0.0)
            delay = getattr(self, "_bq_next_delay", None)
            if delay is None:
                job = self._bq_job_settings(item["job_id"])
                interval, jitter = self._bq_interval_settings(job)
                delay = interval + random.uniform(0, jitter) if jitter > 0 else interval
                self._bq_next_delay = delay
            due = last + delay
            if time.time() < due:
                self._bq_state = {"state": "waiting", "job_id": item["job_id"], "item_id": item["id"],
                                  "label": item.get("label", ""), "next_at": due}
                return False
            status, _msg = await self._bq_run_item(item)
            return bool(status)

    def _bq_job_settings(self, job_id: int) -> dict:
        with self._storage._connect() as conn:
            row = conn.execute("SELECT interval_s, jitter_s FROM batch_jobs WHERE id=?", (int(job_id),)).fetchone()
        return dict(row) if row else {}

    async def _bq_loop(self) -> None:
        self._bq_event = asyncio.Event()
        while not getattr(self, "_scheduler_stop", False):
            try:
                ran = await self._bq_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 批量任务执行器出错: {e}")
                ran = False
            if ran:
                continue
            st = getattr(self, "_bq_state", {}) or {}
            wait = self.BQ_IDLE_POLL
            if st.get("next_at"):
                wait = max(0.05, min(wait, st["next_at"] - time.time()))
            try:
                await asyncio.wait_for(self._bq_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self._bq_event.clear()

    # ------------------------------------------------------------------
    # 完成
    # ------------------------------------------------------------------
    def _bq_finish_if_done(self, job_id: int, cancelled: bool = False) -> bool:
        with self._storage._connect() as conn:
            left = conn.execute("SELECT COUNT(*) FROM batch_items WHERE job_id=? AND status IN (?, ?)",
                                (int(job_id), ITEM_PENDING, ITEM_RUNNING)).fetchone()[0]
            if left:
                return False
            state = conn.execute("SELECT state FROM batch_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not state or state[0] in (JOB_DONE, JOB_CANCELLED):
                return False
            conn.execute("UPDATE batch_jobs SET state=?, finished_at=? WHERE id=?",
                         (JOB_CANCELLED if cancelled else JOB_DONE, int(time.time()), int(job_id)))
            conn.commit()
        return True

    async def _bq_finish_if_done_async(self, job_id: int) -> None:
        if not self._bq_finish_if_done(job_id):
            return
        job = self._bq_job(job_id)
        if not job:
            return
        try:
            await self._bq_on_finished(job)
        except Exception as e:
            logger.warning(f"[GroupMgr] 批量任务完成处理出错 #{job_id}: {e}")

    @staticmethod
    def _bq_summary(job: dict, max_lines: int = 15) -> str:
        c = job.get("counts") or {}
        lines = [f"批量任务 #{job['id']}「{job['title']}」已完成：成功 {c.get(ITEM_OK, 0)}，"
                 f"失败 {c.get(ITEM_FAILED, 0)}，跳过 {c.get(ITEM_SKIPPED, 0)}"
                 + (f"，取消 {c[ITEM_CANCELLED]}" if c.get(ITEM_CANCELLED) else "")]
        bad = [it for it in job.get("items", []) if it["status"] == ITEM_FAILED]
        for it in bad[:max_lines]:
            target = f" {it['target']}" if it.get("target") else ""
            lines.append(f"· 群 {it['group_id']}{target} {it['label']}：{it['result']}")
        if len(bad) > max_lines:
            lines.append(f"……另有 {len(bad) - max_lines} 项失败，详见 WebUI「批量任务」")
        return "\n".join(lines)

    async def _bq_on_finished(self, job: dict) -> None:
        meta = job.get("meta") or {}
        if meta.get("todo_content"):
            ok_groups = [it["group_id"] for it in job["items"] if it["status"] == ITEM_OK]
            if ok_groups and hasattr(self, "_record_todo"):
                self._record_todo(meta["todo_content"], ok_groups)
        notify = str(meta.get("notify_group") or "")
        if notify and self._cfg("batch_notify_enabled", True):
            client = await self._get_client()
            if client:
                await self._call_group_api(client, "send_group_msg", "批量任务完成通知",
                                           group_id=self._safe_int(notify, 0),
                                           message=[{"type": "text", "data": {"text": self._bq_summary(job)}}])

    # ------------------------------------------------------------------
    # 执行器
    # ------------------------------------------------------------------
    async def _bq_exec_kick(self, client, gid: str, uid: str, params: dict) -> Tuple[str, str]:
        block = bool(params.get("block"))
        if self._is_blacklist_exempt(uid):
            return ITEM_SKIPPED, "插件管理员 / 白名单用户，不处理"
        log = {"source": params.get("source", ""), "operator": params.get("operator", ""),
               "reason": params.get("reason", "")}
        if params.get("log_action"):
            log["action"] = params["log_action"]
        r = await self._kick_targets_in_group(client, gid, [uid], block, log=log)
        if r.get("kicked"):
            return ITEM_OK, "已踢出并拉黑" if block else "已踢出"
        if r.get("skipped"):
            return ITEM_SKIPPED, r["skipped"]
        if (r.get("failed") or {}).get(uid):
            return ITEM_FAILED, r["failed"][uid]
        if r.get("absent"):
            return ITEM_SKIPPED, "不在该群"
        return ITEM_FAILED, "踢人失败"

    async def _bq_exec_notice(self, client, gid: str, _target: str, params: dict) -> Tuple[str, str]:
        task = {"todo": bool(params.get("todo")), "content": params.get("content", ""),
                "options": params.get("options") or {}, "image": params.get("image", ""),
                "source": params.get("source", ""), "operator": params.get("operator", "")}
        ok, err = await self._publish_notice_one(client, gid, task)
        return (ITEM_OK, "已发布") if ok else (ITEM_FAILED, err or "发送失败")

    async def _bq_exec_rename(self, client, gid: str, _target: str, params: dict) -> Tuple[str, str]:
        """op:
        suffix            改为 params.suffix（空 = 清除）；end_countdown 时同时结束该群倒计时
        countdown_set     按 target/template 渲染今天的后缀并改名，成功后登记倒计时
        countdown_update  按该群已登记的倒计时渲染并改名（force=False 时今天已更新则跳过）
        """
        op = params.get("op", "suffix")
        log = {"source": params.get("source", "自动"), "operator": params.get("operator", "")}
        if params.get("reason"):
            log["reason"] = params["reason"]
        # 每日更新按「建任务那天」渲染：跨过午夜才轮到的项不会算成第二天
        try:
            today = date.fromisoformat(params["today"]) if params.get("today") else date.today()
        except ValueError:
            today = date.today()
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            try:
                if op == "suffix":
                    action = params.get("log_action") or ("清除群名后缀" if not params.get("suffix") else "修改群名后缀")
                    ok, info = await self._apply_group_suffix(client, gid, params.get("suffix", ""), tasks,
                                                              log=dict(log, action=action))
                    if params.get("end_countdown") and (tasks.get(gid) or {}).get("countdown"):
                        tasks[gid]["countdown"] = None
                    return (ITEM_OK, info) if ok else (ITEM_FAILED, info)
                if op == "countdown_set":
                    target = datetime.strptime(params["target"], "%Y-%m-%d").date()
                    template = params["template"]
                    suffix = self._render_countdown(template, target, today)
                    if suffix is None:
                        return ITEM_SKIPPED, "目标日期已经过去了"
                    ok, info = await self._apply_group_suffix(
                        client, gid, suffix, tasks, template,
                        log=dict(log, action="设置群名倒计时", reason=f"目标 {target.isoformat()}，模板 {template}"))
                    if ok:
                        tasks.setdefault(gid, {})["countdown"] = {
                            "target": target.isoformat(), "template": template, "last_date": today.isoformat()}
                    return (ITEM_OK, info) if ok else (ITEM_FAILED, info)
                if op == "countdown_update":
                    cd = (tasks.get(gid) or {}).get("countdown")
                    if not cd:
                        return ITEM_SKIPPED, "该群的倒计时已停止"
                    cd.pop("queued", None)
                    if not params.get("force") and cd.get("last_date") == today.isoformat():
                        return ITEM_SKIPPED, "今天已经更新过"
                    # 这期间群名被别人改过：终止并删除倒计时，不再改名
                    current, stopped = await self._countdown_tampered(client, gid, tasks, log=log, today=today)
                    if stopped:
                        return ITEM_SKIPPED, stopped
                    target = datetime.strptime(cd["target"], "%Y-%m-%d").date()
                    suffix = self._render_countdown(cd["template"], target, today)
                    expired = suffix is None
                    ok, info = await self._apply_group_suffix(
                        client, gid, "" if expired else suffix, tasks, cd["template"],
                        log=dict(log, action="群名倒计时更新"), current=current)
                    if not ok:
                        cd["retry_after"] = int(time.time()) + self.COUNTDOWN_RETRY_SECONDS
                        return ITEM_FAILED, info
                    cd.pop("retry_after", None)
                    if expired:
                        tasks[gid]["countdown"] = None
                        self._record_op("群名倒计时结束", group_id=gid, source="自动", detail="目标日期已过，已去掉后缀")
                        return ITEM_OK, f"{info}（目标日期已过，倒计时结束）"
                    cd["last_date"] = today.isoformat()
                    return ITEM_OK, info
                return ITEM_FAILED, f"未知的改名操作 {op}"
            finally:
                self._save_name_tasks(tasks)
