# -*- coding: utf-8 -*-
"""群成员查找（二次开发新增）。WebUI「群成员查找」页。

搜索：在本地群成员缓存（group_directory）里按 QQ 号（完全相同 / 开头相同）或昵称、群名片（包含）找人，
不调用协议端接口。6 万成员搜一次约 20~40 毫秒。「re:」开头按正则匹配昵称 / 群名片（找群名片打广告的号）。
搜索结果可多选批量踢出并拉黑（页面复用 /minimal/kick 和 /minimal/blacklist/add）。
个人档案：把插件里关于这个人的记录汇总到一起——
  所在的群（角色、群名片、头衔、群等级、入群时间、最后发言时间）、
  发过的码（code_user_stats，长期累计）、发过的小程序（mp_sends）、上传的群文件（最近一次群文件扫描结果）、
  退群记录、违规记录（审核日志）、操作记录、是否在黑名单 / 白名单、是不是插件管理员。
"""
import re
from typing import Dict, List

SEARCH_LIMIT = 50
# 批量拉黑时一次最多列出的人数（页面传 limit）
SEARCH_LIMIT_MAX = 500
AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100"


class MemberLookupMixin:
    def _mbr_search(self, q: str, limit: int = SEARCH_LIMIT) -> dict:
        """返回 {items: [{user_id, name, names, groups, roles, max_level, exact, blacklisted, protected}], total, truncated}。

        q：纯数字按 QQ 号开头匹配；「re:」开头按正则匹配昵称 / 群名片（不区分大小写）；其它按包含匹配。
        name 优先取命中的那个名字（群名片打广告时一眼能看到广告内容）。正则写错时返回 error。
        """
        q = str(q or "").strip()
        if not q:
            return {"items": [], "total": 0, "truncated": False}
        limit = max(1, min(int(limit or SEARCH_LIMIT), SEARCH_LIMIT_MAX))
        pattern = None
        if q[:3].lower() == "re:":
            try:
                pattern = re.compile(q[3:].strip(), re.IGNORECASE)
            except re.error as e:
                return {"items": [], "total": 0, "truncated": False, "error": f"正则写得不对：{e}"}
            if not pattern.pattern:
                return {"items": [], "total": 0, "truncated": False}
        by_digits = pattern is None and q.isdigit()
        folded = q.casefold()

        def hit(text: str) -> bool:
            if not text:
                return False
            return bool(pattern.search(text)) if pattern is not None else folded in text.casefold()

        state = self._dir()
        by_uid: Dict[str, dict] = {}
        for gid, members in state["members"].items():
            for uid, m in members.items():
                card, nick = m.get("card") or "", m.get("nickname") or ""
                if by_digits:
                    if not uid.startswith(q):
                        continue
                    matched = []
                else:
                    matched = [x for x in (card, nick) if hit(x)]
                    if not matched:
                        continue
                it = by_uid.get(uid)
                if it is None:
                    it = by_uid[uid] = {"user_id": uid, "names": [], "matched": [], "groups": 0, "roles": [],
                                        "max_level": 0, "exact": uid == q}
                it["groups"] += 1
                for x in matched:
                    if x not in it["matched"]:
                        it["matched"].append(x)
                name = card or nick
                if name and name not in it["names"]:
                    it["names"].append(name)
                if m.get("role") in ("owner", "admin") and m["role"] not in it["roles"]:
                    it["roles"].append(m["role"])
                it["max_level"] = max(it["max_level"], int(m.get("level") or 0))
        items = sorted(by_uid.values(), key=lambda it: (not it["exact"], -it["groups"], it["user_id"]))
        black = getattr(self, "_user_black_set", None) or set()
        exempt = getattr(self, "_is_blacklist_exempt", None)
        for it in items[:limit]:
            matched = it.pop("matched")
            names = matched + [x for x in it["names"] if x not in matched]
            it["names"] = names[:5]
            it["name"] = names[0] if names else ""
            it["avatar"] = AVATAR_URL.format(uid=it["user_id"])
            it["blacklisted"] = it["user_id"] in black
            it["protected"] = bool(exempt(it["user_id"])) if callable(exempt) else False
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
