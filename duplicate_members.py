# -*- coding: utf-8 -*-
"""重复群成员检测（最小化插件，二次开发新增）。

同一个 QQ 号同时在多个「管理的群」里即视为重复成员。检测只读本地群成员缓存（group_directory），
不额外调用接口；缓存不完整时结果也不完整，WebUI 会提示缓存覆盖了多少个群。

不参与统计：备份群 / 葵花码转发群、插件不管理的群、机器人自己。
检测范围可以在 WebUI 里勾选（存在 meta 表 duplicate_member_scope；没勾选 = 所有管理的群）。
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
    DUP_SCOPE_META_KEY = "duplicate_member_scope"
    DUP_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100"

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
    def _dup_candidate_groups(self) -> List[str]:
        """可以参与统计的群：缓存里有的、插件管理的、不是备份群的群。"""
        state = self._dir()
        is_backup = getattr(self, "_is_backup_group", lambda _g: False)
        return [gid for gid in state["groups"] if self._group_id_allowed(gid) and not is_backup(gid)]

    def _dup_scope_selected(self) -> List[str]:
        """WebUI 里勾选的检测范围；空 = 所有管理的群。"""
        try:
            data = json.loads(self._storage.get_meta(self.DUP_SCOPE_META_KEY, "") or "[]")
        except Exception:
            return []
        return [str(g) for g in data] if isinstance(data, list) else []

    def _dup_set_scope(self, group_ids: List[str], source: str = "WebUI", operator: str = "") -> List[str]:
        candidates = set(self._dup_candidate_groups())
        selected = []
        for gid in group_ids or []:
            gid = str(gid)
            if gid in candidates and gid not in selected:
                selected.append(gid)
        self._storage.set_meta(self.DUP_SCOPE_META_KEY, json.dumps(selected, ensure_ascii=False))
        self._record_op("设置重复成员检测范围", source=source, operator=operator,
                        detail=("、".join(selected) if selected else "所有管理的群"))
        return selected

    def _dup_scope_groups(self) -> List[str]:
        """参与统计的群：可参与的群 ∩ 勾选的范围（没勾选时全部）。"""
        candidates = self._dup_candidate_groups()
        selected = set(self._dup_scope_selected())
        if not selected:
            return candidates
        return [gid for gid in candidates if gid in selected]

    DUP_SORTS = ("count", "recent_join", "uid")

    def _dup_signature(self) -> tuple:
        """影响检测结果的输入：成员缓存版本、检测范围、忽略名单、黑白名单、插件管理员、机器人 QQ。"""
        black = getattr(self, "_user_black_set", None) or set()
        white = getattr(self, "_user_white_set", None) or set()
        try:
            admins = frozenset(self._get_all_admin_ids())
        except Exception:
            admins = frozenset()
        return (int(getattr(self, "_dir_version", 0) or 0), tuple(self._dup_scope_groups()),
                tuple(sorted(self._dup_ignored().items(), key=lambda kv: kv[0])) if self._dup_ignored() else (),
                hash(frozenset(black)), hash(frozenset(white)), hash(admins),
                str(getattr(self, "_bot_uin_cache", 0) or ""))

    def _dup_all(self) -> List[dict]:
        """全部重复成员（含管理员 / 白名单 / 已忽略），按输入缓存：6 万成员重算约 0.1 秒，缓存命中几乎不花时间。"""
        sig = self._dup_signature()
        cached = getattr(self, "_dup_cache", None)
        if cached and cached[0] == sig:
            return cached[1]
        items = self._dup_find_uncached(include_exempt=True, include_ignored=True)["items"]
        self._dup_cache = (sig, items)
        return items

    def _dup_find(self, min_groups: int = 2, include_exempt: bool = False,
                  include_ignored: bool = False, keyword: str = "") -> dict:
        """返回 {"items": [...], "coverage": {...}}，items 按所在群数从多到少排序（结果走缓存）。"""
        items = self._dup_filter(self._dup_all(), min_groups, include_exempt, include_ignored, keyword)
        return {"items": items, "coverage": self._dup_coverage()}

    @staticmethod
    def _dup_filter(items: List[dict], min_groups: int = 2, include_exempt: bool = False,
                    include_ignored: bool = False, keyword: str = "") -> List[dict]:
        need = max(2, int(min_groups or 2))
        keyword = str(keyword or "").strip().casefold()
        out = []
        for it in items:
            if it["count"] < need or (it["exempt"] and not include_exempt) or (it["ignored"] and not include_ignored):
                continue
            if keyword and keyword not in it["user_id"] and not any(
                    keyword in (g["card"] or g["nickname"]).casefold() for g in it["groups"]):
                continue
            out.append(it)
        return out

    def _dup_page(self, min_groups: int = 2, include_exempt: bool = False, include_ignored: bool = False,
                  keyword: str = "", sort: str = "count", page: int = 1, page_size: int = 50) -> dict:
        """WebUI 用：筛选后分页。返回 {items（本页）, total, page, page_size, by_count, coverage}。"""
        items = self._dup_filter(self._dup_all(), min_groups, include_exempt, include_ignored, keyword)
        if sort == "recent_join":
            items = sorted(items, key=lambda it: -max(g["join_time"] for g in it["groups"]))
        elif sort == "uid":
            items = sorted(items, key=lambda it: int(it["user_id"]) if it["user_id"].isdigit() else 0)
        by_count: Dict[int, int] = {}
        for it in items:
            by_count[it["count"]] = by_count.get(it["count"], 0) + 1
        page_size = min(max(int(page_size or 50), 10), 200)
        pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(max(int(page or 1), 1), pages)
        return {"items": items[(page - 1) * page_size: page * page_size], "total": len(items), "page": page,
                "page_size": page_size, "pages": pages,
                "by_count": [{"count": k, "people": v} for k, v in sorted(by_count.items(), reverse=True)],
                "coverage": self._dup_coverage()}

    def _dup_matching_ids(self, min_groups: int = 2, include_ignored: bool = False, keyword: str = "") -> List[str]:
        """按条件批量处理：符合筛选条件、且不是管理员 / 白名单的全部 QQ（与列表顺序一致）。"""
        return [it["user_id"] for it in self._dup_filter(self._dup_all(), min_groups, False, include_ignored, keyword)
                if not (it["plugin_admin"] or it["whitelisted"])]

    def _dup_coverage(self) -> dict:
        state = self._dir()
        scope = self._dup_scope_groups()
        selected = set(self._dup_scope_selected())
        cached = [g for g in scope if g in state["members"]]
        return {
            "groups_total": len(scope), "groups_cached": len(cached),
            "missing": [g for g in scope if g not in state["members"]],
            "stale": [g for g in cached if not self._dir_members_fresh(g)],
            "oldest_update": min((state["members_ts"].get(g, 0) for g in cached), default=0),
            "scope_custom": bool(selected),
            "scope_all": [
                {"group_id": gid, "group_name": (state["groups"].get(gid) or {}).get("group_name", ""),
                 "selected": (not selected) or gid in selected, "cached": gid in state["members"]}
                for gid in self._dup_candidate_groups()
            ],
        }

    def _dup_find_uncached(self, min_groups: int = 2, include_exempt: bool = False,
                           include_ignored: bool = False, keyword: str = "") -> dict:
        state = self._dir()
        scope = self._dup_scope_groups()
        selected = set(self._dup_scope_selected())
        cached = [g for g in scope if g in state["members"]]
        bot_uin = str(getattr(self, "_bot_uin_cache", 0) or "")
        ignored = self._dup_ignored()
        black = getattr(self, "_user_black_set", None) or set()
        white = getattr(self, "_user_white_set", None) or set()
        try:
            admins = set(self._get_all_admin_ids())
        except Exception:
            admins = set()
        order = {gid: i for i, gid in enumerate(scope)}
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
                    "level": int(m.get("level") or 0),
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
            groups.sort(key=lambda g: (g["join_time"] or 1 << 62, order.get(g["group_id"], 0)))
            items.append({
                "user_id": uid, "name": names[0] if names else "", "count": len(groups), "groups": groups,
                "avatar": self.DUP_AVATAR_URL.format(uid=uid),
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
                "scope_custom": bool(selected),
                "scope_all": [
                    {"group_id": gid, "group_name": (state["groups"].get(gid) or {}).get("group_name", ""),
                     "selected": (not selected) or gid in selected, "cached": gid in state["members"]}
                    for gid in self._dup_candidate_groups()
                ],
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
        found = {it["user_id"]: it for it in self._dup_all()}
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

    def _dup_job(self, user_ids: List[str], mode: str, reason: str,
                 keep: Optional[Dict[str, str]] = None, block: bool = False,
                 source: str = "WebUI", operator: str = "") -> dict:
        """与 _dup_handle 相同的判断，但不立即踢人：每个「人 × 要踢的群」放进批量任务队列。

        返回 {"job": 任务摘要或 None, "results": {QQ号: {"kept", "groups": [...], "skipped"}}}
        """
        found = {it["user_id"]: it for it in self._dup_all()}
        keep = keep or {}
        results, items = {}, []
        for uid in user_ids:
            uid = str(uid)
            item = found.get(uid)
            out = {"kept": "", "groups": [], "skipped": ""}
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
            elif mode == "kick_all":
                targets = group_ids
                action = "重复成员清理（全部踢出）"
                if block:
                    self._add_user_blacklist(uid, reason or "重复加群", source=source, operator=operator)
            else:
                out["skipped"] = f"未知的处理方式 {mode}"
                continue
            out["groups"] = targets
            params = {"block": bool(block), "source": source, "operator": operator, "log_action": action,
                      "reason": reason + (f"（保留群 {out['kept']}）" if out["kept"] else "")}
            label = ("踢出并拉黑" if block else "踢出") + ("（保留一个群）" if mode == "keep_one" else "")
            items.extend({"group_id": gid, "action": "kick", "label": label, "target": uid, "params": params}
                         for gid in targets)
        job = None
        if items:
            people = len({it["target"] for it in items})
            title = f"重复成员清理：{people} 人{'保留一个群' if mode == 'keep_one' else '全部踢出'}" + \
                    (f"（{reason}）" if reason else "")
            job = self._bq_create_job(title, "kick", items, source=source, operator=operator)
        return {"job": job, "results": results}
