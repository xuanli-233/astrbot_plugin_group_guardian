# -*- coding: utf-8 -*-
"""群成员查找（二次开发新增）。WebUI「群成员查找」页。

搜索：在本地群成员缓存（group_directory）里按 QQ 号（完全相同 / 开头相同）或昵称、群名片（包含）找人，
不调用协议端接口。6 万成员搜一次约 20~40 毫秒。
个人档案：把插件里关于这个人的记录汇总到一起——
  所在的群（角色、群名片、头衔、群等级、入群时间、最后发言时间）、
  发过的码（code_user_stats，长期累计）、发过的小程序（mp_sends）、上传的群文件（最近一次群文件扫描结果）、
  退群记录、违规记录（审核日志）、操作记录、是否在黑名单 / 白名单、是不是插件管理员。
"""
from typing import Dict, List

SEARCH_LIMIT = 50
AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100"


class MemberLookupMixin:
    def _mbr_search(self, q: str, limit: int = SEARCH_LIMIT) -> dict:
        """返回 {items: [{user_id, name, names, groups, roles, max_level, exact}], total, truncated}。"""
        q = str(q or "").strip()
        if not q:
            return {"items": [], "total": 0, "truncated": False}
        state = self._dir()
        folded = q.casefold()
        by_uid: Dict[str, dict] = {}
        for gid, members in state["members"].items():
            for uid, m in members.items():
                if q.isdigit():
                    if not uid.startswith(q):
                        continue
                else:
                    card, nick = (m.get("card") or "").casefold(), (m.get("nickname") or "").casefold()
                    if folded not in card and folded not in nick:
                        continue
                it = by_uid.get(uid)
                if it is None:
                    it = by_uid[uid] = {"user_id": uid, "names": [], "groups": 0, "roles": [], "max_level": 0,
                                        "exact": uid == q}
                it["groups"] += 1
                name = m.get("card") or m.get("nickname") or ""
                if name and name not in it["names"]:
                    it["names"].append(name)
                if m.get("role") in ("owner", "admin") and m["role"] not in it["roles"]:
                    it["roles"].append(m["role"])
                it["max_level"] = max(it["max_level"], int(m.get("level") or 0))
        items = sorted(by_uid.values(), key=lambda it: (not it["exact"], -it["groups"], it["user_id"]))
        for it in items:
            it["name"] = it["names"][0] if it["names"] else ""
            it["names"] = it["names"][:5]
            it["avatar"] = AVATAR_URL.format(uid=it["user_id"])
        return {"items": items[:limit], "total": len(items), "truncated": len(items) > limit}

    def _mbr_groups(self, uid: str) -> List[dict]:
        state = self._dir()
        out = []
        for gid, members in state["members"].items():
            m = members.get(uid)
            if not m:
                continue
            g = state["groups"].get(gid) or {}
            out.append({"group_id": gid, "group_name": g.get("group_name", ""), "role": m.get("role", "member"),
                        "card": m.get("card", ""), "nickname": m.get("nickname", ""), "title": m.get("title", ""),
                        "level": int(m.get("level") or 0), "join_time": int(m.get("join_time") or 0),
                        "last_sent_time": int(m.get("last_sent_time") or 0),
                        "cached_at": int(state["members_ts"].get(gid, 0) or 0),
                        "bot_role": g.get("bot_role", "")})
        out.sort(key=lambda g: g["join_time"] or 1 << 62)
        return out

    def _mbr_profile(self, uid: str) -> dict:
        uid = str(uid or "").strip()
        state = self._dir()
        groups = self._mbr_groups(uid)
        names = [g["card"] or g["nickname"] for g in groups if g["card"] or g["nickname"]]
        try:
            admins = set(self._get_all_admin_ids())
        except Exception:
            admins = set()
        black = getattr(self, "_user_black_set", None) or set()
        white = getattr(self, "_user_white_set", None) or set()
        meta_all = self._bl_meta_all() if callable(getattr(self, "_bl_meta_all", None)) else {}
        out = {
            "user_id": uid, "name": names[0] if names else "", "names": list(dict.fromkeys(names))[:10],
            "avatar": AVATAR_URL.format(uid=uid), "groups": groups,
            "coverage": {"groups_total": len(state["groups"]), "groups_cached": len(state["members"])},
            "plugin_admin": uid in admins, "whitelisted": uid in white, "blacklisted": uid in black,
            "blacklist": meta_all.get(uid) if uid in black else None,
        }
        # 码（长期累计）
        cu = getattr(self, "_cu_user_detail", None)
        out["codes"] = cu(uid, limit=200) if callable(cu) else None
        # 小程序
        mp = getattr(self, "_mp_user", None)
        out["miniapps"] = mp(uid) if callable(mp) else None
        lk = getattr(self, "_lk_user", None)
        out["links"] = lk(uid) if callable(lk) else None
        # 群文件（最近一次扫描）
        st = getattr(self, "_gf_scan_state", None)
        if st:
            files = [f for f in st.get("files", []) if f.get("uploader") == uid]
            out["files"] = {"items": files[:200], "total": len(files), "scanned_at": st.get("finished_at", 0),
                            "scanned_groups": len(st.get("groups") or []), "running": bool(st.get("running"))}
        else:
            out["files"] = {"items": [], "total": 0, "scanned_at": 0, "scanned_groups": 0, "running": False}
        # 退群记录
        if callable(getattr(self, "_ll_user_summary", None)):
            summary = self._ll_user_summary(uid)
            records, _total = self._ll_list(keyword=uid, page_size=20)
            out["leaves"] = {"summary": summary, "items": [r for r in records if r["user_id"] == uid]}
        else:
            out["leaves"] = None
        # 违规记录（审核日志）
        try:
            out["violations"] = {"items": self._storage.list_logs(20, 0, user_id=uid),
                                 "total": int(self._storage.count_logs_filtered(user_id=uid))}
        except Exception:
            out["violations"] = {"items": [], "total": 0}
        # 操作记录（踢人、拉黑、撤回……对象是他的）
        if callable(getattr(self, "_op_list", None)):
            ops, total = self._op_list(keyword=uid, page_size=30)
            out["ops"] = {"items": [o for o in ops if str(o.get("target") or "") == uid or uid in str(o.get("detail") or "")],
                          "total": total}
        else:
            out["ops"] = {"items": [], "total": 0}
        return out
