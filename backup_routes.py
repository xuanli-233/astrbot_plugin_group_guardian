# -*- coding: utf-8 -*-
"""备份 / 通知转发群：可以设多个群，每个群单独选要收哪几类内容（二次开发新增）。

配置 backup_routes（list，WebUI「规则 → 转发群」里维护），每项一个群：
  "群号:类别1,类别2"     例如 "123456:keyword,wxcode,bot_join"
类别见 BACKUP_KINDS。同一类内容会发到所有勾选了它的群，各发一份。

旧配置兼容：从没保存过 backup_routes 时，按旧的 backup_group_id（全部类别，
设了 wxcode_forward_group_id 时葵花码除外）和 wxcode_forward_group_id（只收葵花码）推算。
在 WebUI 保存一次后改用 backup_routes，并清空两个旧配置。

所有转发群本身都不参与审核（_is_backup_group），避免备份内容被再次撤回。
"""
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

ROUTES_KEY = "backup_routes"
LEGACY_BACKUP_KEY = "backup_group_id"
LEGACY_WXCODE_KEY = "wxcode_forward_group_id"
BACKUP_ROUTES_MAX = 20

# (类别, 名称, 说明)
BACKUP_KINDS: List[Tuple[str, str, str]] = [
    ("keyword", "关键词命中", "关键词规则命中后撤回的原消息"),
    ("wxcode", "二维码 / 葵花码命中", "图片里的码命中规则（或解不出的可疑码）撤回前的原消息和识别内容"),
    ("code_limit", "码发送超限", "同一个码发送次数超过限制被撤回的原消息"),
    ("miniapp", "小程序命中", "小程序卡片 / 链接命中规则撤回的原消息"),
    ("manual_recall", "指令撤回", "/撤回最新消息、/批量撤回、/回复撤回 撤回的消息"),
    ("leave_recall", "退群撤回", "有人退群 / 被踢后撤回的他最近的消息"),
    ("bot_join", "机器人进群", "机器人被拉进新群时推送群名、人数、邀请人"),
    ("bot_friend", "机器人加好友", "机器人新加好友时推送昵称、验证消息"),
]
BACKUP_KIND_IDS = [k for k, _n, _d in BACKUP_KINDS]
BACKUP_KIND_NAMES = {k: n for k, n, _d in BACKUP_KINDS}
# 表格表头用的短名字
BACKUP_KIND_SHORT = {"keyword": "关键词", "wxcode": "二维码 / 葵花码", "code_limit": "码超限", "miniapp": "小程序",
                     "manual_recall": "指令撤回", "leave_recall": "退群撤回", "bot_join": "机器人进群",
                     "bot_friend": "机器人加好友"}


def parse_route(entry) -> Optional[dict]:
    """"123456:keyword,wxcode" / {"group_id", "kinds"} → {"group_id", "kinds"}；无效返回 None。"""
    if isinstance(entry, dict):
        gid = str(entry.get("group_id") or "").strip()
        kinds = entry.get("kinds") or []
        if isinstance(kinds, str):
            kinds = kinds.split(",")
    else:
        text = str(entry or "").strip().replace("：", ":").replace("，", ",")
        gid, _sep, rest = text.partition(":")
        gid = gid.strip()
        kinds = rest.split(",") if rest.strip() else []
    if not gid.isdigit():
        return None
    seen = []
    for k in kinds:
        k = str(k or "").strip()
        if k in BACKUP_KIND_IDS and k not in seen:
            seen.append(k)
    seen.sort(key=BACKUP_KIND_IDS.index)
    return {"group_id": gid, "kinds": seen}


def format_route(route: dict) -> str:
    return f"{route['group_id']}:{','.join(route['kinds'])}"


class BackupRoutesMixin:
    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _br_routes(self) -> List[dict]:
        """当前所有转发群（含旧配置推算出来的）。按群号去重，后面的合并到前面。"""
        cfg = getattr(self, "config", None) or {}
        raw = cfg.get(ROUTES_KEY) if ROUTES_KEY in cfg else None
        legacy_backup = str(cfg.get(LEGACY_BACKUP_KEY, "") or "").strip()
        legacy_wx = str(cfg.get(LEGACY_WXCODE_KEY, "") or "").strip()
        cache_key = (repr(raw), legacy_backup, legacy_wx)
        cached = getattr(self, "_br_cache", None)
        if cached and cached[0] == cache_key:
            return cached[1]
        if isinstance(raw, list) and (raw or not (legacy_backup or legacy_wx)):
            entries = raw
        else:
            entries = []
            if legacy_backup.isdigit():
                kinds = [k for k in BACKUP_KIND_IDS if not (k == "wxcode" and legacy_wx.isdigit())]
                entries.append({"group_id": legacy_backup, "kinds": kinds})
            if legacy_wx.isdigit():
                entries.append({"group_id": legacy_wx, "kinds": ["wxcode"]})
        merged: Dict[str, dict] = {}
        for entry in entries:
            route = parse_route(entry)
            if not route:
                continue
            if route["group_id"] in merged:
                kinds = merged[route["group_id"]]["kinds"]
                kinds.extend(k for k in route["kinds"] if k not in kinds)
                kinds.sort(key=BACKUP_KIND_IDS.index)
            else:
                merged[route["group_id"]] = route
        routes = list(merged.values())
        self._br_cache = (cache_key, routes)
        return routes

    def _br_targets(self, kind: str) -> List[str]:
        """收 kind 这类内容的所有群号。"""
        return [r["group_id"] for r in self._br_routes() if kind in r["kinds"]]

    def _is_backup_group(self, group_id) -> bool:
        gid = str(group_id or "").strip()
        return bool(gid) and any(r["group_id"] == gid for r in self._br_routes())

    # ------------------------------------------------------------------
    # 保存（WebUI）
    # ------------------------------------------------------------------
    def _br_save(self, items, source: str = "WebUI", operator: str = "") -> Tuple[List[dict], str]:
        """保存转发群列表。返回 (保存后的列表, 错误信息)。"""
        if not isinstance(items, list):
            return [], "格式不对"
        routes: List[dict] = []
        seen = set()
        for item in items:
            route = parse_route(item)
            if not route:
                return [], f"群号不对：{(item or {}).get('group_id') if isinstance(item, dict) else item}"
            if route["group_id"] in seen:
                return [], f"群 {route['group_id']} 重复了，请合并成一行"
            seen.add(route["group_id"])
            routes.append(route)
        if len(routes) > BACKUP_ROUTES_MAX:
            return [], f"最多 {BACKUP_ROUTES_MAX} 个转发群"
        old = {r["group_id"]: r["kinds"] for r in self._br_routes()}
        previous = {k: self.config[k] for k in (ROUTES_KEY, LEGACY_BACKUP_KEY, LEGACY_WXCODE_KEY) if k in self.config}
        self.config[ROUTES_KEY] = [format_route(r) for r in routes]
        # 改用新配置后清空旧的两项，避免以后被误用
        self.config[LEGACY_BACKUP_KEY] = ""
        self.config[LEGACY_WXCODE_KEY] = ""
        if not self._save_config_safe():
            for k in (ROUTES_KEY, LEGACY_BACKUP_KEY, LEGACY_WXCODE_KEY):
                if k in previous:
                    self.config[k] = previous[k]
                else:
                    self.config.pop(k, None)
            return [], "配置持久化失败，本次修改未生效"
        new = {r["group_id"]: r["kinds"] for r in routes}
        changes = []

        def names(kinds):
            return "、".join(BACKUP_KIND_NAMES[k] for k in kinds) or "（什么都不收）"
        for gid, kinds in new.items():
            if gid not in old:
                changes.append(f"新增 {gid}：{names(kinds)}")
            elif old[gid] != kinds:
                changes.append(f"{gid}：{names(old[gid])} → {names(kinds)}")
        changes += [f"移除 {gid}" for gid in old if gid not in new]
        if changes:
            self._record_op("修改转发群", source=source, operator=operator, detail="；".join(changes)[:2000])
        logger.info(f"[GroupMgr] 转发群已更新：{len(routes)} 个")
        return self._br_routes(), ""
