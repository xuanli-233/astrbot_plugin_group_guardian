# -*- coding: utf-8 -*-
"""实时处理日志（WebUI「实时日志」面板，二次开发新增）。

每条进入审核流程的群消息登记一条记录，处理过程中更新「当前步骤」，结束时写入结果与耗时：
  收到 → 检查关键词 → 下载图片 / 定位 / 解码（识别服务慢时可以看到进度）→ 结果（放行 / 撤回 / 跳过 / 出错）

性能：
- 只存在内存里的环形缓冲（live_log_size 条，默认 300），不写数据库、不调用接口；
  消息内容只保留前 LIVE_PREVIEW_CHARS 个字，每条最多记 LIVE_MAX_STEPS 个步骤。
- 每次变更给记录打一个递增序号；WebUI 带着上次的序号轮询，只取回变化的记录。
- 当前记录通过 contextvars 传递，深层代码（识别流水线等）调用 live_stage() 更新进度，不用改函数签名；
  没有登记（关闭、测试、后台任务）时这些调用什么也不做。
"""
import contextvars
import time
from collections import deque
from typing import Optional

_CURRENT = contextvars.ContextVar("group_guardian_live_entry", default=None)

LIVE_PREVIEW_CHARS = 80
LIVE_MAX_STEPS = 12
STATUS_RUNNING = "running"


class LiveLog:
    def __init__(self, size: int = 300):
        self.items = deque(maxlen=max(10, int(size)))
        self.seq = 0
        self.epoch = int(time.time() * 1000)

    def resize(self, size: int) -> None:
        size = max(10, int(size))
        if size != self.items.maxlen:
            self.items = deque(self.items, maxlen=size)

    def bump(self, entry: dict) -> None:
        self.seq += 1
        entry["seq"] = self.seq

    def add(self, entry: dict) -> None:
        self.bump(entry)
        entry["id"] = entry["seq"]
        self.items.append(entry)

    def snapshot(self, since: int = 0, epoch: int = 0, limit: int = 300) -> dict:
        reset = int(epoch or 0) != self.epoch or int(since or 0) > self.seq
        floor = 0 if reset else int(since or 0)
        changed = [e for e in self.items if e["seq"] > floor]
        if len(changed) > limit:
            changed = changed[-limit:]
        running = sum(1 for e in self.items if e["status"] == STATUS_RUNNING)
        return {
            "epoch": self.epoch, "seq": self.seq, "reset": reset, "size": self.items.maxlen,
            "running": running, "items": [_public(e) for e in changed],
        }


def _public(entry: dict) -> dict:
    out = dict(entry)
    out["steps"] = [list(s) for s in entry.get("steps", [])]
    if out["status"] == STATUS_RUNNING:
        out["ms"] = int((time.time() - entry["ts"]) * 1000)
    return out


def _current():
    return _CURRENT.get()


def live_stage(text: str) -> None:
    """更新当前消息的处理步骤（没有登记时什么也不做）。"""
    cur = _current()
    if not cur:
        return
    log, entry = cur
    if entry["status"] != STATUS_RUNNING:
        return
    text = str(text)[:60]
    entry["stage"] = text
    steps = entry["steps"]
    if len(steps) < LIVE_MAX_STEPS:
        steps.append((int((time.time() - entry["ts"]) * 1000), text))
    log.bump(entry)


def live_set(status: str, result: str = "", reason: str = "") -> None:
    """写入结果：hit（命中处理）/ pass（放行）/ skip（跳过）/ error（出错）。先写的结果不会被后写的「放行」覆盖。"""
    cur = _current()
    if not cur:
        return
    log, entry = cur
    if entry["status"] != STATUS_RUNNING:
        return
    if status == "pass" and entry.get("final"):
        return
    entry["final"] = status
    entry["result"] = str(result)[:60]
    if reason:
        entry["reason"] = str(reason)[:120]
    log.bump(entry)


class LiveLogMixin:
    def _live_log(self) -> LiveLog:
        log = getattr(self, "_live_log_obj", None)
        size = self._cfg_int("live_log_size", 300) if hasattr(self, "_cfg_int") else 300
        size = min(max(size, 50), 2000)
        if log is None:
            log = LiveLog(size)
            self._live_log_obj = log
        else:
            log.resize(size)
        return log

    def _live_begin(self, event) -> Optional[tuple]:
        """登记一条群消息，返回句柄（交给 _live_end）。功能关闭或不是群消息时返回 None。"""
        try:
            if not self._cfg("live_log_enabled", True):
                return None
            group_id = self._get_group_id(event)
            if not group_id:
                return None
            user_id = self._try_get_sender_id(event)
            try:
                user_name = event.get_sender_name() or ""
            except Exception:
                user_name = ""
            text = ""
            if self._cfg("live_log_show_text", True):
                text = str(getattr(event, "message_str", "") or "").strip().replace("\n", " ")
                if len(text) > LIVE_PREVIEW_CHARS:
                    text = text[:LIVE_PREVIEW_CHARS] + "…"
            images = 0
            try:
                chain = getattr(getattr(event, "message_obj", None), "message", None) or []
                images = sum(1 for seg in chain if type(seg).__name__.lower() == "image"
                             or (isinstance(seg, dict) and seg.get("type") == "image"))
            except Exception:
                images = 0
            log = self._live_log()
            entry = {
                "ts": time.time(), "group_id": str(group_id), "user_id": str(user_id or ""),
                "user_name": str(user_name)[:32], "text": text, "images": images,
                "stage": "收到", "status": STATUS_RUNNING, "final": "", "result": "", "reason": "",
                "steps": [], "ms": 0,
            }
            log.add(entry)
            handle = (log, entry)
            _CURRENT.set(handle)
            return handle
        except Exception:
            return None

    @staticmethod
    def _live_end(handle, stopped: bool = False, error: Exception = None) -> None:
        if not handle:
            return
        log, entry = handle
        if entry["status"] == STATUS_RUNNING:
            if error is not None:
                entry["status"] = "error"
                entry["result"] = "出错"
                entry["reason"] = f"{type(error).__name__}: {error}"[:120]
            elif entry.get("final"):
                entry["status"] = entry["final"]
            else:
                entry["status"] = "pass"
                entry["result"] = entry.get("result") or ("已处理" if stopped else "放行")
            entry["ms"] = int((time.time() - entry["ts"]) * 1000)
            entry["stage"] = ""
            log.bump(entry)
        if _CURRENT.get() is handle:
            _CURRENT.set(None)
