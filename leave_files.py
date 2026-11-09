# -*- coding: utf-8 -*-
"""退群 / 被踢后自动删除该成员上传的群文件（二次开发新增）。

触发：group_decrease 通知
  sub_type=leave  主动退群   （leave_files_on_leave）
  sub_type=kick   被管理员踢 （leave_files_on_kick，含本插件自己踢的人）
总开关 leave_files_enabled，默认关；可在多群配置中按群开关。

做法：扫描这个群的群文件（根目录 + 一级文件夹，和「群文件」页同一套 _gf_scan_group），
找出上传者是这个人的文件，逐个 delete_group_file。单次最多 LEAVE_FILES_MAX 个。
同一个群的文件列表缓存 LEAVE_FILES_CACHE_SECONDS 秒（批量踢人时不会每人扫一遍），
所有删除排队执行；结果写一条操作记录「退群删文件」。机器人需要是群管理员。
"""
import asyncio
import time
from typing import Dict, List, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from .leave_log import decrease_kind
except ImportError:  # 单元测试直接加载本文件
    from leave_log import decrease_kind


class LeaveFilesMixin:
    LEAVE_FILES_MAX = 200
    LEAVE_FILES_CACHE_SECONDS = 120
    LEAVE_FILES_INTERVAL = 0.5

    async def _lf_group_files(self, client, gid: str) -> Tuple[List[dict], str]:
        """群文件列表（缓存）。返回 (文件, 错误)。"""
        cache = getattr(self, "_lf_cache", None)
        if cache is None:
            cache = self._lf_cache = {}
        hit = cache.get(gid)
        if hit and time.time() - hit[0] < self.LEAVE_FILES_CACHE_SECONDS:
            return hit[1], ""
        files, err, _folders = await self._gf_scan_group(client, gid)
        if not files and err:
            return [], err
        cache[gid] = (time.time(), files)
        if len(cache) > 200:
            for key in sorted(cache, key=lambda k: cache[k][0])[:100]:
                cache.pop(key, None)
        return files, ""

    def _lf_forget(self, gid: str, file_id: str) -> None:
        hit = (getattr(self, "_lf_cache", None) or {}).get(gid)
        if hit:
            hit[1][:] = [f for f in hit[1] if f.get("file_id") != file_id]
        forget = getattr(self, "_gf_forget", None)
        if callable(forget):
            forget(gid, file_id)

    async def _lf_delete_user_files(self, client, group_id: str, user_id: str, trigger: str,
                                    operator: str = "") -> Dict:
        """删除该成员在这个群上传的文件，返回 {deleted, failed, total, errors}。"""
        gid, uid = str(group_id), str(user_id)
        result: Dict = {"deleted": 0, "failed": 0, "total": 0, "errors": [], "names": []}
        lock = getattr(self, "_lf_lock", None)
        if lock is None:
            lock = self._lf_lock = asyncio.Lock()
        async with lock:
            files, err = await self._lf_group_files(client, gid)
            if err:
                self._record_op("退群删文件", source="自动", operator=operator, group_id=gid, target=uid,
                                reason=trigger, result="失败", detail=f"获取群文件失败：{err}")
                logger.warning(f"[GroupMgr] {trigger}删文件：群 {gid} 获取群文件失败: {err}")
                return result
            mine = [f for f in files if str(f.get("uploader") or "") == uid and f.get("file_id")]
            mine = mine[: self.LEAVE_FILES_MAX]
            result["total"] = len(mine)
            for i, f in enumerate(mine):
                if i:
                    await asyncio.sleep(self.LEAVE_FILES_INTERVAL)
                ok, e = await self._call_group_api(
                    client, "delete_group_file", "删除群文件", group_id=self._safe_int(gid, 0),
                    file_id=f["file_id"], busid=self._safe_int(f.get("busid"), 0))
                if ok:
                    result["deleted"] += 1
                    result["names"].append(f.get("name") or f["file_id"])
                    self._lf_forget(gid, f["file_id"])
                else:
                    result["failed"] += 1
                    if e and e not in result["errors"]:
                        result["errors"].append(e)
        if result["total"]:
            status = "成功" if not result["failed"] else ("失败" if not result["deleted"] else "部分成功")
            detail = f"删除 {result['deleted']}/{result['total']} 个文件"
            if result["names"]:
                shown = "、".join(n[:40] for n in result["names"][:10])
                detail += f"：{shown}" + (" 等" if len(result["names"]) > 10 else "")
            if result["errors"]:
                detail += "；失败原因：" + "；".join(result["errors"][:3])
            self._record_op("退群删文件", source="自动", operator=operator, group_id=gid, target=uid,
                            reason=trigger, result=status, detail=detail[:2000])
            logger.info(f"[GroupMgr] {trigger}：群 {gid} 成员 {uid}，{detail[:200]}")
        return result

    async def _handle_leave_files(self, event: AstrMessageEvent) -> bool:
        """group_decrease 通知 → 后台删除该成员上传的群文件。返回是否已安排。"""
        raw = self._get_raw_event(event)
        if not (isinstance(raw, dict) and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_decrease"):
            return False
        sub_type = decrease_kind(raw)
        if sub_type == "leave":
            trigger, switch = "主动退群", "leave_files_on_leave"
        elif sub_type == "kick":
            trigger, switch = "被踢出群", "leave_files_on_kick"
        else:
            return False
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        self_id = str(raw.get("self_id", "") or "")
        if not group_id or not user_id or user_id == self_id:
            return False
        if self._is_backup_group(group_id) or not self._blacklist_feature_ready(group_id):
            return False
        if not self._cfg("leave_files_enabled", False, group_id=group_id):
            return False
        if not self._cfg(switch, True, group_id=group_id):
            return False
        client = await self._get_client(event)
        if not client:
            return False
        operator = str(raw.get("operator_id", "") or "")
        if operator == self_id:
            operator = "机器人"
        elif operator == user_id:
            operator = ""
        tasks = getattr(self, "_lf_tasks", None)
        if tasks is None:
            tasks = self._lf_tasks = set()

        async def run():
            try:
                await self._lf_delete_user_files(client, group_id, user_id, trigger, operator)
            except Exception as e:
                logger.warning(f"[GroupMgr] 退群删文件出错({group_id}/{user_id}): {e}")

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True
