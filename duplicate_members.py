# -*- coding: utf-8 -*-
"""重复群成员检测（最小化插件，二次开发新增）。

同一个 QQ 号同时在多个「管理的群」里即视为重复成员。检测只读本地群成员缓存（group_directory），
不额外调用接口；缓存不完整时结果也不完整，WebUI 会提示缓存覆盖了多少个群。

不参与统计：备份群 / 葵花码转发群、插件不管理的群、机器人自己。
默认不列出（可在 WebUI 打开）：插件管理员、用户白名单、在任一群是群主 / 管理员的人、已忽略的人。

处理方式（每一步都写操作记录）：
  keep_one   只保留一个群（默认最早加入的群），从其余群踢出；可选在被踢的群里 QQ 原生拉黑
  kick_all   从所有群踢出；可选加入插件用户黑名单（同时 QQ 原生拉黑）
  ignore     加入忽略名单，以后不再列出（如工作人员）；可取消
"""
import json
import time
from typing import Dict, List, Optional

from astrbot.api import logger


class DuplicateMemberMixin:
    DUP_IGNORE_META_KEY = "duplicate_member_ignore"

    # ------------------------------------------------------------------
    # 忽略名单
    # ------------------------------------------------------------------
    def _dup_ignored(self) -> Dict[str, dict]:
        try:
            data = json.loads(self._storage.get_meta(self.DUP_IGNORE_META_KEY, "") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _dup_set_ignored(self, user_id: str, ignore: bool, reason: str = "",
                         source: str = "WebUI", operator: str = "") -> None:
        data = self._dup_ignored()
        uid = str(user_id)
        if ignore:
            data[uid] = {"reason": reason, "source": source, "time": int(time.time())}
        else:
            data.pop(uid, None)
        self._storage.set_meta(self.DUP_IGNORE_META_KEY, json.dumps(data, ensure_ascii=False))
        self._record_op("忽略重复成员" if ignore else "取消忽略重复成员", source=source, operator=operator,
                        target=uid, reason=reason)

    # ------------------------------------------------------------------
    # 检测
    # ------------------------------------------------------------------
    def _dup_scope_groups(self) -> List[str]:
        """参与统计的群：缓存里有的、插件管理的、不是备份群的群。"""
        state = self._dir()
        is_backup = getattr(self, "_is_backup_group", lambda _g: False)
        return [gid for gid in state["groups"] if self._group_id_allowed(gid) and not is_backup(gid)]

    def _dup_find(self, min_groups: int = 2, include_exempt: bool = False,
                  include_ignored: bool = False, keyword: str = "") -> dict:
        """返回 {"items": [...], "coverage": {...}}，items 按所在群数从多到少排序。"""
        state = self._dir()
        scope = self._dup_scope_groups()
        cached = [g for g in scope if g in state["members"]]
        bot_uin = str(getattr(self, "_bot_uin_cache", 0) or "")
        ignored = self._dup_ignored()
        black = getattr(self, "_user_black_set", None) or set()
        white = getattr(self, "_user_white_set", None) or set()
        try:
            admins = set(self._get_all_admin_ids())
        except Exception:
            admins = set()
        index: Dict[str, List[dict]] = {}
        for gid in cached:
            group = state["groups"].get(gid) or {}
            for uid, m in state["members"][gid].items():
                if uid == bot_uin:
                    continue
                index.setdefault(uid, []).append({
                    "group_id": gid, "group_name": group.get("group_name", ""),
                    "role": m.get("role", "member"), "card": m.get("card", ""), "nickname": m.get("nickname", ""),
                    "join_time": int(m.get("join_time") or 0), "last_sent_time": int(m.get("last_sent_time") or 0),
                })
        keyword = str(keyword or "").strip().casefold()
        items = []
        for uid, groups in index.items():
            if len(groups) < max(2, int(min_groups or 2)):
                continue
            managers = [g["group_id"] for g in groups if g["role"] in ("owner", "admin")]
            exempt = uid in admins or uid in white or bool(managers)
            if exempt and not include_exempt:
                continue
            if uid in ignored and not include_ignored:
                continue
            names = [g["card"] or g["nickname"] for g in groups if g["card"] or g["nickname"]]
            if keyword and keyword not in uid and not any(keyword in n.casefold() for n in names):
                continue
            groups.sort(key=lambda g: (g["join_time"] or 1 << 62, scope.index(g["group_id"])))
            items.append({
                "user_id": uid, "name": names[0] if names else "", "count": len(groups), "groups": groups,
                "keep_default": groups[0]["group_id"], "exempt": exempt, "managers": managers,
                "plugin_admin": uid in admins, "whitelisted": uid in white,
                "blacklisted": uid in black, "ignored": ignored.get(uid),
            })
        items.sort(key=lambda it: (-it["count"], it["user_id"]))
        return {
            "items": items,
            "coverage": {
                "groups_total": len(scope), "groups_cached": len(cached),
                "missing": [g for g in scope if g not in state["members"]],
                "stale": [g for g in cached if not self._dir_members_fresh(g)],
                "oldest_update": min((state["members_ts"].get(g, 0) for g in cached), default=0),
            },
        }

    # ------------------------------------------------------------------
    # 处理
    # ------------------------------------------------------------------
    async def _dup_handle(self, client, user_ids: List[str], mode: str, reason: str,
                          keep: Optional[Dict[str, str]] = None, block: bool = False,
                          source: str = "WebUI", operator: str = "") -> dict:
        """对一批重复成员执行 keep_one / kick_all。keep：{QQ号: 要保留的群}，缺省为最早加入的群。

        返回 {"results": {QQ号: {"kicked": [...], "failed": {群: 原因}, "kept": 群, "skipped": 原因}}}
        """
        found = {it["user_id"]: it for it in self._dup_find(include_exempt=True, include_ignored=True)["items"]}
        keep = keep or {}
        results = {}
        for uid in user_ids:
            uid = str(uid)
            item = found.get(uid)
            out = {"kicked": [], "failed": {}, "kept": "", "skipped": ""}
            results[uid] = out
            if item is None:
                out["skipped"] = "缓存中已不是重复成员（可能已处理或缓存已更新）"
                continue
            if item["plugin_admin"] or item["whitelisted"]:
                out["skipped"] = "插件管理员 / 白名单用户，不处理"
                continue
            group_ids = [g["group_id"] for g in item["groups"]]
            if mode == "keep_one":
                kept = keep.get(uid) if keep.get(uid) in group_ids else item["keep_default"]
                out["kept"] = kept
                targets = [g for g in group_ids if g != kept]
                action = "重复成员清理（保留一个群）"
                native_block = bool(block)
            elif mode == "kick_all":
                targets = group_ids
                action = "重复成员清理（全部踢出）"
                native_block = bool(block)
                if block:
                    self._add_user_blacklist(uid, reason or "重复加群", source=source, operator=operator)
            else:
                out["skipped"] = f"未知的处理方式 {mode}"
                continue
            log = {"action": action, "source": source, "operator": operator,
                   "reason": reason + (f"（保留群 {out['kept']}）" if out["kept"] else "")}
            for gid in targets:
                try:
                    r = await self._kick_targets_in_group(client, gid, [uid], native_block, log=log)
                except Exception as e:
                    logger.warning(f"[GroupMgr] 重复成员清理出错({gid}/{uid}): {e}")
                    out["failed"][gid] = str(e)
                    continue
                if r["kicked"]:
                    out["kicked"].append(gid)
                elif r["skipped"]:
                    out["failed"][gid] = r["skipped"]
                elif r["failed"]:
                    out["failed"][gid] = r["failed"].get(uid, "踢人失败")
                elif r["absent"]:
                    out["failed"][gid] = "已不在该群"
                    remover = getattr(self, "_dir_remove_member", None)
                    if callable(remover):
                        remover(gid, uid)
        return {"results": results}
