# -*- coding: utf-8 -*-
"""进群后自动设置机器人账号的群消息接收方式（二次开发新增）。

LuckyLilliaBot 扩展接口 set_group_msg_mask(group_id, mask)：
  1 接收并提醒   2 收进群助手（不提醒）   3 屏蔽群消息   4 接收消息但不提醒（默认）
改的是机器人自己这个 QQ 号的设置，不影响群成员，也不影响插件继续处理群消息
（mask=3 屏蔽后协议端可能收不到该群消息，WebUI 会提示）。

触发：
  1. 机器人进群（group_increase 通知里 user_id 是机器人自己）：等几秒后设置
  2. 漏报兜底：群成员缓存后台刷新（每 5 分钟）时对比群列表，给没处理过的新群设置
  3. 已有的群：WebUI 里点「给已有的群设置」，逐个群放进批量任务
第一次开启时把当时已在的群记为「基线」不去改动（避免覆盖你手动调过的设置），之后新进的群才自动设置。
处理过的群记在 meta 表 msg_mask_state，不会反复修改。
"""
import asyncio
import json
import random
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

MASK_LABELS = {1: "接收并提醒", 2: "收进群助手", 3: "屏蔽群消息", 4: "接收消息但不提醒"}
MM_META_KEY = "msg_mask_state"


class MsgMaskMixin:
    MM_JOIN_DELAY = (3.0, 8.0)
    MM_NEW_GROUP_INTERVAL = 2.0
    MM_NEW_GROUPS_PER_TICK = 10

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _mm_state(self) -> dict:
        try:
            data = json.loads(self._storage.get_meta(MM_META_KEY, "") or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("baseline_done", False)
        if not isinstance(data.get("groups"), dict):
            data["groups"] = {}
        return data

    def _mm_save(self, state: dict) -> None:
        self._storage.set_meta(MM_META_KEY, json.dumps(state, ensure_ascii=False))

    def _mm_mask(self) -> int:
        return min(max(self._cfg_int("auto_msg_mask_value", 4), 1), 4)

    def _mm_ready(self) -> bool:
        return (bool(self.config.get("disclaimer_agreed", False)) and self._cfg("enabled")
                and self._cfg("auto_msg_mask_enabled", False))

    # ------------------------------------------------------------------
    # 设置一个群
    # ------------------------------------------------------------------
    async def _mm_apply(self, client, group_id: str, mask: Optional[int] = None,
                        source: str = "自动", operator: str = "", reason: str = "") -> Tuple[bool, str]:
        gid = str(group_id)
        mask = min(max(int(mask or self._mm_mask()), 1), 4)
        ok, err = await self._call_group_api(
            client, "set_group_msg_mask", "设置群消息接收方式",
            group_id=self._safe_int(gid, 0), mask=mask,
        )
        state = self._mm_state()
        state["groups"][gid] = {"mask": mask, "ts": int(time.time()), "ok": bool(ok),
                                "error": "" if ok else str(err or "失败")[:200], "source": source}
        self._mm_save(state)
        self._record_op("设置群消息接收方式", source=source, operator=operator, group_id=gid,
                        reason=reason, result="成功" if ok else "失败",
                        detail=f"{MASK_LABELS[mask]}（mask={mask}）" + ("" if ok else f"：{err}"))
        if ok:
            logger.info(f"[GroupMgr] 群 {gid} 消息接收方式已设为「{MASK_LABELS[mask]}」")
        else:
            logger.warning(f"[GroupMgr] 设置群 {gid} 消息接收方式失败: {err}")
        return bool(ok), "" if ok else str(err or "失败")

    # ------------------------------------------------------------------
    # 触发 1：机器人进群
    # ------------------------------------------------------------------
    async def _handle_bot_join_msg_mask(self, event) -> bool:
        raw = self._get_raw_event(event)
        if not (isinstance(raw, dict) and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_increase"):
            return False
        self_id = str(raw.get("self_id", "") or "")
        if not self_id or str(raw.get("user_id", "") or "") != self_id:
            return False
        gid = str(raw.get("group_id", "") or "")
        if not gid or not self._mm_ready():
            return False
        state = self._mm_state()
        if (state["groups"].get(gid) or {}).get("ok"):
            return False
        client = await self._get_client(event)
        if not client:
            return False

        async def run():
            try:
                await asyncio.sleep(random.uniform(*self.MM_JOIN_DELAY))
                await self._mm_apply(client, gid, reason="机器人进群")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 进群后设置消息接收方式出错({gid}): {e}")

        tasks = getattr(self, "_mm_tasks", None)
        if tasks is None:
            tasks = set()
            self._mm_tasks = tasks
        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    # ------------------------------------------------------------------
    # 触发 2：后台对比群列表（漏报兜底）
    # ------------------------------------------------------------------
    async def _mm_check_new_groups(self, client, group_ids: List[str]) -> List[str]:
        """返回本次设置过的群。第一次运行只记录基线，不改动已有的群。"""
        if not self._mm_ready() or not group_ids:
            return []
        state = self._mm_state()
        current = [str(g) for g in group_ids]
        if not state["baseline_done"]:
            now = int(time.time())
            for gid in current:
                state["groups"].setdefault(gid, {"baseline": True, "ts": now})
            state["baseline_done"] = True
            self._mm_save(state)
            logger.info(f"[GroupMgr] 进群自动设置消息接收方式：已记录现有 {len(current)} 个群为基线（不改动）")
            return []
        new = [g for g in current if g not in state["groups"]][: self.MM_NEW_GROUPS_PER_TICK]
        done = []
        for i, gid in enumerate(new):
            if i:
                await asyncio.sleep(self.MM_NEW_GROUP_INTERVAL)
            ok, _err = await self._mm_apply(client, gid, reason="发现新群（进群通知可能漏报）")
            if ok:
                done.append(gid)
        return done

    # ------------------------------------------------------------------
    # 触发 3：已有的群（批量任务）
    # ------------------------------------------------------------------
    def _mm_job(self, group_ids: List[str], mask: int, source: str = "WebUI", operator: str = "") -> Optional[dict]:
        mask = min(max(int(mask or 4), 1), 4)
        items = [{"group_id": str(g), "action": "msgmask", "label": f"消息设为「{MASK_LABELS[mask]}」",
                  "target": "", "params": {"mask": mask, "source": source, "operator": operator}}
                 for g in group_ids]
        if not items:
            return None
        return self._bq_create_job(f"设置群消息接收方式：{len(items)} 个群 →「{MASK_LABELS[mask]}」", "msgmask",
                                   items, source=source, operator=operator)

    async def _bq_exec_msgmask(self, client, gid: str, _target: str, params: dict) -> Tuple[str, str]:
        ok, err = await self._mm_apply(client, gid, params.get("mask"), source=params.get("source", "WebUI"),
                                       operator=params.get("operator", ""), reason="批量设置")
        return ("ok", f"已设为「{MASK_LABELS[min(max(int(params.get('mask') or 4), 1), 4)]}」") if ok else ("failed", err)

    def _mm_status(self, groups: List[dict]) -> Dict:
        state = self._mm_state()
        rows = []
        for g in groups:
            gid = str(g.get("group_id"))
            st = state["groups"].get(gid) or {}
            if st.get("baseline"):
                status = "基线（开启前已在的群，未改动）"
            elif st.get("ok"):
                status = f"已设为「{MASK_LABELS.get(st.get('mask'), st.get('mask'))}」"
            elif st:
                status = f"设置失败：{st.get('error', '')}"
            else:
                status = "未处理"
            rows.append({"group_id": gid, "group_name": g.get("group_name", ""), "status": status,
                         "ts": int(st.get("ts") or 0), "ok": bool(st.get("ok"))})
        return {"baseline_done": bool(state["baseline_done"]), "groups": rows,
                "labels": {str(k): v for k, v in MASK_LABELS.items()}}
