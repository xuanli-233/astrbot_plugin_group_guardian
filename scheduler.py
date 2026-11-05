# -*- coding: utf-8 -*-
"""后台任务。

- 定时解禁：/禁言 /批量禁言 登记的解禁计划到期后解除（auto_unban_enabled，默认关），失败退避重试
- 群名定时改名 / 倒计时（group_name_task.py），每分钟检查一次
- 群成员缓存后台补齐（group_directory.py）
- 批量任务执行器（batch_queue.py）
后台任务没有 event，OneBot client 通过 _get_client(None) 的 platform_manager 回退获取；
插件 terminate 时取消全部任务，避免热重载后残留协程。
原版的申诉超时、名片周期同步、词库自学习、AI 误判复盘任务已删除。
"""
import asyncio
import time

from astrbot.api import logger


UNBAN_RETRY_BASE_SECONDS = 30
UNBAN_RETRY_MAX_SECONDS = 3600

_TASK_ATTRS = (
    ("_scheduler_task", "定时解禁任务"),
    ("_group_name_task", "群名定时任务"),
    ("_dir_task", "群成员缓存任务"),
    ("_dir_refresh_task", "群成员缓存刷新"),
    ("_bq_task", "批量任务执行器"),
)


class SchedulerMixin:
    def _init_scheduler(self) -> None:
        """初始化调度器状态。在 __init__ 中调用。"""
        for attr, _label in _TASK_ATTRS:
            setattr(self, attr, None)
        self._scheduler_stop = False

    def _start_scheduler(self) -> None:
        """启动后台任务（幂等：已在运行则不重复启动）。"""
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_stop = False
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            if callable(getattr(self, "_group_name_loop", None)):
                self._group_name_task = asyncio.create_task(self._group_name_loop())
            if callable(getattr(self, "_dir_background_loop", None)):
                self._dir_task = asyncio.create_task(self._dir_background_loop())
            if callable(getattr(self, "_bq_loop", None)):
                self._bq_task = asyncio.create_task(self._bq_loop())
        except RuntimeError:
            # 无运行中的事件循环（极少见），放弃后台任务，不影响其它功能
            logger.debug("[GroupMgr] 无事件循环，跳过调度器启动")

    async def _stop_scheduler(self) -> None:
        """停止全部后台任务。在 terminate 中调用。"""
        self._scheduler_stop = True
        for attr, label in _TASK_ATTRS:
            task = getattr(self, attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(f"[GroupMgr] {label}已取消")

    async def _scheduler_loop(self) -> None:
        consecutive_errors = 0
        while not self._scheduler_stop:
            interval = self._clamp_int(self.config.get("auto_unban_scan_interval", 60), 60, 10, 3600)
            await asyncio.sleep(interval)
            if self._scheduler_stop:
                break
            try:
                await self._run_due_unbans()
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[GroupMgr] 定时解禁任务出错({consecutive_errors}): {e}")
                if consecutive_errors >= 10:
                    logger.error("[GroupMgr] 定时解禁连续出错过多，暂停 5 分钟")
                    await asyncio.sleep(300)
                    consecutive_errors = 0

    async def _run_due_unbans(self) -> None:
        """执行所有到期的定时解禁。"""
        now = int(time.time())
        due = self._storage.list_due_unbans(now)
        if not due:
            return
        for item in due:
            gid = str(item.get("group_id", ""))
            uid = item.get("user_id", "")
            if not self._cfg("auto_unban_enabled", False, group_id=gid):
                continue
            ok = await self._unban_member(gid, uid)
            if ok:
                self._storage.delete_scheduled_unban(item.get("id"))
                logger.info(f"[GroupMgr] 定时解禁: 群{gid} 用户{uid}")
                continue

            retry_count = max(0, int(item.get("retry_count", 0) or 0)) + 1
            delay = min(
                UNBAN_RETRY_MAX_SECONDS,
                UNBAN_RETRY_BASE_SECONDS * (2 ** min(retry_count - 1, 7)),
            )
            next_retry_at = now + delay
            self._storage.mark_scheduled_unban_retry(
                item.get("id"), next_retry_at, "OneBot 解禁失败"
            )
            logger.warning(
                f"[GroupMgr] 定时解禁失败，{delay}秒后重试: "
                f"群{gid} 用户{uid}（第{retry_count}次）"
            )

    def _schedule_unban(self, group_id: str, user_id: str, mute_seconds: int) -> None:
        """登记一条定时解禁计划。mute_seconds<=0（永久禁言）时按一个较大的兜底时长处理。

        仅在 auto_unban_enabled 开启时登记。OneBot 自带到期解禁，本功能用于
        永久禁言托管解禁、重启后补解禁等场景。
        """
        if not group_id or not user_id:
            return
        group_id = str(group_id)
        if not self._cfg("auto_unban_enabled", False, group_id=group_id):
            return
        now = int(time.time())
        if mute_seconds and mute_seconds > 0:
            unban_at = now + int(mute_seconds)
        else:
            # 永久禁言：按配置的兜底托管时长（默认 7 天）后解禁
            fallback = self._cfg_int("auto_unban_permanent_hours", 168, group_id=group_id)
            unban_at = now + fallback * 3600
        try:
            self._storage.add_scheduled_unban(group_id, user_id, unban_at, now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 登记定时解禁失败: {e}")
