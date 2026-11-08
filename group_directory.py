# -*- coding: utf-8 -*-
"""群 / 群成员本地缓存（最小化插件，二次开发新增）。

协议端的列表接口有限流，跨群踢人、批量公告、群名任务、管理页面如果每次都实时拉取
get_group_list / get_group_member_list，群一多就会集中发出大量请求。这里把群列表和
每个群的成员列表缓存到 SQLite（内存里也有一份），按以下规则使用：

- 读：缓存未过期直接返回；过期或不存在时才调用接口刷新。过期时间可配置
  （directory_group_ttl / directory_member_ttl）。接口失败时退回旧缓存。
- 限流：所有列表类调用经 _dir_call 排队，相邻两次至少间隔 directory_min_interval_ms；
  同一个群同时只有一个刷新在进行。
- 实时更新：入群 / 退群 / 被踢 / 管理员任免 / 改名片等通知事件直接改缓存
  （_dir_handle_notice），机器人踢人成功后也同步移除，所以 TTL 可以设得较长。
- 后台：每 DIR_BACKGROUND_TICK 秒最多刷新 DIR_BACKGROUND_BATCH 个过期的群
  （directory_background_refresh_enabled），慢慢补齐，不集中请求。
"""
import asyncio
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger


class GroupDirectoryMixin:
    DIR_BACKGROUND_TICK = 300
    DIR_BACKGROUND_BATCH = 2

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _dir_init_db(self) -> None:
        if getattr(self, "_dir_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dir_groups ("
                "group_id TEXT PRIMARY KEY, group_name TEXT NOT NULL DEFAULT '', "
                "member_count INTEGER NOT NULL DEFAULT 0, bot_role TEXT NOT NULL DEFAULT '', "
                "members_updated_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dir_members ("
                "group_id TEXT NOT NULL, user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', "
                "card TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'member', "
                "title TEXT NOT NULL DEFAULT '', join_time INTEGER NOT NULL DEFAULT 0, "
                "last_sent_time INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (group_id, user_id))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dir_members_user ON dir_members(user_id)")
            # 群等级（活跃等级，协议端 level 字段）：旧库补列
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(dir_members)").fetchall()}
            if "level" not in cols:
                conn.execute("ALTER TABLE dir_members ADD COLUMN level INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        self._dir_db_ready = True

    def _dir_bump(self) -> None:
        """成员 / 群名缓存变了：版本号 +1（重复成员检测按版本号缓存结果）。"""
        self._dir_version = int(getattr(self, "_dir_version", 0) or 0) + 1

    @staticmethod
    async def _dir_offload(fn, *args):
        """把 SQLite 写入放到线程池，避免几千行写入卡住事件循环；没有线程池（Pyodide）时直接执行。"""
        loop = asyncio.get_running_loop()
        try:
            fut = loop.run_in_executor(None, fn, *args)
        except Exception:
            return fn(*args)
        return await fut

    def _dir(self) -> dict:
        """内存中的缓存，首次访问时从 SQLite 载入。"""
        state = getattr(self, "_dir_state", None)
        if state is not None:
            return state
        state = {"groups": {}, "groups_ts": 0, "members": {}, "members_ts": {}}
        try:
            self._dir_init_db()
            with self._storage._connect() as conn:
                for row in conn.execute("SELECT * FROM dir_groups"):
                    state["groups"][row["group_id"]] = {
                        "group_id": row["group_id"], "group_name": row["group_name"],
                        "member_count": row["member_count"], "bot_role": row["bot_role"],
                    }
                    state["members_ts"][row["group_id"]] = row["members_updated_at"]
                    state["groups_ts"] = max(state["groups_ts"], row["updated_at"])
                for row in conn.execute("SELECT * FROM dir_members"):
                    state["members"].setdefault(row["group_id"], {})[row["user_id"]] = {
                        "user_id": row["user_id"], "nickname": row["nickname"], "card": row["card"],
                        "role": row["role"], "title": row["title"], "join_time": row["join_time"],
                        "last_sent_time": row["last_sent_time"],
                        "level": int(row["level"] or 0) if "level" in row.keys() else 0,
                    }
        except Exception as e:
            logger.warning(f"[GroupMgr] 载入群成员缓存失败，将重新拉取: {e}")
        self._dir_state = state
        return state

    def _dir_save_groups(self, groups: Dict[str, dict], now: int) -> None:
        try:
            self._dir_init_db()
            with self._storage._connect() as conn:
                keep = list(groups)
                if keep:
                    marks = ",".join("?" * len(keep))
                    conn.execute(f"DELETE FROM dir_groups WHERE group_id NOT IN ({marks})", keep)
                    conn.execute(f"DELETE FROM dir_members WHERE group_id NOT IN ({marks})", keep)
                else:
                    conn.execute("DELETE FROM dir_groups")
                    conn.execute("DELETE FROM dir_members")
                members_ts = self._dir()["members_ts"]
                for gid, g in groups.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO dir_groups(group_id, group_name, member_count, bot_role, "
                        "members_updated_at, updated_at) VALUES(?,?,?,?,?,?)",
                        (gid, g.get("group_name", ""), int(g.get("member_count") or 0), g.get("bot_role", ""),
                         int(members_ts.get(gid, 0)), now),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"[GroupMgr] 保存群列表缓存失败: {e}")

    @staticmethod
    def _dir_member_tuple(m: dict) -> tuple:
        return (m.get("nickname", ""), m.get("card", ""), m.get("role", "member"), m.get("title", ""),
                int(m.get("join_time") or 0), int(m.get("last_sent_time") or 0), int(m.get("level") or 0))

    def _dir_save_members(self, gid: str, members: Dict[str, dict], now: int,
                          old: Optional[Dict[str, dict]] = None) -> None:
        """写一个群的成员。old 是刷新前的缓存时只写变化的行（2000 人的群通常只有几十行变化），
        否则整群重写。"""
        try:
            self._dir_init_db()
            with self._storage._connect() as conn:
                if old is None:
                    conn.execute("DELETE FROM dir_members WHERE group_id=?", (gid,))
                    changed = list(members.items())
                else:
                    gone = [uid for uid in old if uid not in members]
                    if gone:
                        conn.executemany("DELETE FROM dir_members WHERE group_id=? AND user_id=?",
                                         [(gid, uid) for uid in gone])
                    changed = [(uid, m) for uid, m in members.items()
                               if uid not in old or self._dir_member_tuple(old[uid]) != self._dir_member_tuple(m)]
                conn.executemany(
                    "INSERT OR REPLACE INTO dir_members(group_id, user_id, nickname, card, role, title, join_time, "
                    "last_sent_time, updated_at, level) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(gid, uid, m.get("nickname", ""), m.get("card", ""), m.get("role", "member"),
                      m.get("title", ""), int(m.get("join_time") or 0), int(m.get("last_sent_time") or 0), now,
                      int(m.get("level") or 0))
                     for uid, m in changed],
                )
                g = self._dir()["groups"].get(gid) or {"group_id": gid, "group_name": ""}
                conn.execute(
                    "INSERT OR REPLACE INTO dir_groups(group_id, group_name, member_count, bot_role, "
                    "members_updated_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (gid, g.get("group_name", ""), len(members), g.get("bot_role", ""), now,
                     int(self._dir()["groups_ts"] or now)),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[GroupMgr] 保存群 {gid} 成员缓存失败: {e}")

    def _dir_save_member_row(self, gid: str, uid: str, member: Optional[dict]) -> None:
        """单个成员增删改（通知事件触发），member 为 None 表示删除。"""
        try:
            self._dir_init_db()
            with self._storage._connect() as conn:
                if member is None:
                    conn.execute("DELETE FROM dir_members WHERE group_id=? AND user_id=?", (gid, uid))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO dir_members(group_id, user_id, nickname, card, role, title, "
                        "join_time, last_sent_time, updated_at, level) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (gid, uid, member.get("nickname", ""), member.get("card", ""), member.get("role", "member"),
                         member.get("title", ""), int(member.get("join_time") or 0),
                         int(member.get("last_sent_time") or 0), int(time.time()), int(member.get("level") or 0)),
                    )
                conn.execute("UPDATE dir_groups SET member_count=? WHERE group_id=?",
                             (len(self._dir()["members"].get(gid, {})), gid))
                conn.commit()
        except Exception as e:
            logger.debug(f"[GroupMgr] 更新成员缓存失败({gid}/{uid}): {e}")

    # ------------------------------------------------------------------
    # 限流
    # ------------------------------------------------------------------
    def _dir_lock(self, name: str) -> asyncio.Lock:
        locks = getattr(self, "_dir_locks", None)
        if locks is None:
            locks = self._dir_locks = {}
        lock = locks.get(name)
        if lock is None:
            lock = locks[name] = asyncio.Lock()
        return lock

    async def _dir_call(self, client, action: str, name: str, **kwargs):
        """限流后调用协议端：所有列表类调用排队，相邻两次至少间隔 directory_min_interval_ms。"""
        interval = max(0, self._cfg_int("directory_min_interval_ms", 1500)) / 1000.0
        async with self._dir_lock("__api__"):
            last = getattr(self, "_dir_last_call", 0.0)
            wait = last + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._call_group_api_result(client, action, name, **kwargs)
            finally:
                self._dir_last_call = time.monotonic()
                self._dir_api_calls = getattr(self, "_dir_api_calls", 0) + 1

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _dir_group_ttl(self) -> int:
        return self._cfg_int("directory_group_ttl", 3600)

    def _dir_member_ttl(self) -> int:
        return self._cfg_int("directory_member_ttl", 21600)

    def _dir_groups_fresh(self) -> bool:
        state = self._dir()
        return bool(state["groups"]) and time.time() - state["groups_ts"] < self._dir_group_ttl()

    def _dir_members_fresh(self, gid: str) -> bool:
        state = self._dir()
        return gid in state["members"] and time.time() - state["members_ts"].get(gid, 0) < self._dir_member_ttl()

    async def _dir_groups(self, client, force: bool = False) -> Tuple[List[dict], str]:
        """群列表 [{group_id, group_name, member_count, bot_role}]，返回 (列表, 错误说明)。"""
        state = self._dir()
        if not force and self._dir_groups_fresh():
            return list(state["groups"].values()), ""
        async with self._dir_lock("groups"):
            if not force and self._dir_groups_fresh():  # 等锁期间别人已刷新
                return list(state["groups"].values()), ""
            # 强制刷新时带 no_cache：让协议端也跳过自己的缓存，拿到最新群名（不认识该参数的实现会忽略）
            ok, data, err = await self._dir_call(client, "get_group_list", "获取群列表",
                                                 **({"no_cache": True} if force else {}))
            if not ok:
                if state["groups"]:
                    logger.warning(f"[GroupMgr] 获取群列表失败，使用缓存: {err}")
                    return list(state["groups"].values()), ""
                return [], err or "获取群列表失败"
            now = int(time.time())
            groups: Dict[str, dict] = {}
            for item in self._extract_list_result(data):
                if not isinstance(item, dict) or item.get("group_id") is None:
                    continue
                gid = str(item.get("group_id"))
                old = state["groups"].get(gid) or {}
                groups[gid] = {
                    "group_id": gid,
                    "group_name": str(item.get("group_name") or ""),
                    "member_count": int(item.get("member_count") or old.get("member_count") or 0),
                    "bot_role": old.get("bot_role", ""),
                }
            for gone in set(state["groups"]) - set(groups):
                state["members"].pop(gone, None)
                state["members_ts"].pop(gone, None)
            state["groups"] = groups
            state["groups_ts"] = now
            self._dir_bump()
            self._dir_save_groups(groups, now)
            return list(groups.values()), ""

    async def _dir_members(self, client, gid, force: bool = False) -> Optional[Dict[str, dict]]:
        """某群成员 {QQ号: 成员信息}。拉取失败且没有缓存时返回 None。"""
        gid = str(gid)
        state = self._dir()
        if not force and self._dir_members_fresh(gid):
            return state["members"][gid]
        async with self._dir_lock(f"members:{gid}"):
            if not force and self._dir_members_fresh(gid):
                return state["members"][gid]
            ok, data, err = await self._dir_call(
                client, "get_group_member_list", "获取群成员列表",
                group_id=self._safe_int(gid, 0), no_cache=bool(force),
            )
            items = self._extract_list_result(data) if ok else []
            if not ok or not items:
                if gid in state["members"]:
                    logger.debug(f"[GroupMgr] 获取群 {gid} 成员失败，使用缓存: {err}")
                    return state["members"][gid]
                return None
            now = int(time.time())
            members: Dict[str, dict] = {}
            for item in items:
                if not isinstance(item, dict) or item.get("user_id") is None:
                    continue
                uid = str(item.get("user_id"))
                members[uid] = {
                    "user_id": uid,
                    "nickname": str(item.get("nickname") or ""),
                    "card": str(item.get("card") or ""),
                    "role": str(item.get("role") or "member"),
                    "title": str(item.get("title") or ""),
                    "join_time": int(item.get("join_time") or 0),
                    "last_sent_time": int(item.get("last_sent_time") or 0),
                    "level": self._dir_level(item.get("level")),
                }
            old = state["members"].get(gid)
            state["members"][gid] = members
            state["members_ts"][gid] = now
            self._dir_bump()
            group = state["groups"].setdefault(gid, {"group_id": gid, "group_name": ""})
            group["member_count"] = len(members)
            bot_uin = str(await self._get_bot_uin(client) or "")
            if bot_uin and bot_uin in members:
                group["bot_role"] = members[bot_uin]["role"]
            await self._dir_offload(self._dir_save_members, gid, members, now, old)
            return members

    def _dir_cached_member(self, gid, uid) -> Optional[dict]:
        return (self._dir()["members"].get(str(gid)) or {}).get(str(uid))

    def _dir_member_role(self, gid, uid) -> str:
        """缓存未过期时返回成员角色，否则返回空串（调用方再走接口）。"""
        gid = str(gid)
        if not self._dir_members_fresh(gid):
            return ""
        member = self._dir_cached_member(gid, uid)
        return member["role"] if member else ""

    def _dir_find_user(self, uid) -> List[dict]:
        """在缓存里查找某人所在的群。"""
        uid = str(uid)
        state = self._dir()
        out = []
        for gid, members in state["members"].items():
            m = members.get(uid)
            if m:
                g = state["groups"].get(gid) or {}
                out.append({"group_id": gid, "group_name": g.get("group_name", ""), "role": m["role"],
                            "nickname": m["nickname"], "card": m["card"],
                            "cached_at": state["members_ts"].get(gid, 0)})
        return out

    def _dir_status(self) -> dict:
        state = self._dir()
        groups = []
        for gid, g in state["groups"].items():
            groups.append({
                "group_id": gid, "group_name": g.get("group_name", ""),
                "member_count": g.get("member_count", 0), "bot_role": g.get("bot_role", ""),
                "members_cached": len(state["members"].get(gid, {})),
                "members_updated_at": state["members_ts"].get(gid, 0),
                "members_fresh": self._dir_members_fresh(gid),
            })
        return {
            "groups": groups, "groups_updated_at": state["groups_ts"], "groups_fresh": self._dir_groups_fresh(),
            "group_ttl": self._dir_group_ttl(), "member_ttl": self._dir_member_ttl(),
            "min_interval_ms": self._cfg_int("directory_min_interval_ms", 1500),
            "api_calls": getattr(self, "_dir_api_calls", 0),
            "refreshing": self._dir_refresh_progress(),
        }

    # ------------------------------------------------------------------
    # 实时更新
    # ------------------------------------------------------------------
    def _dir_upsert_member(self, gid, uid, **fields) -> None:
        gid, uid = str(gid), str(uid)
        state = self._dir()
        if gid not in state["members"]:
            return  # 该群成员还没缓存过，下次读取时整体拉取
        member = state["members"][gid].get(uid) or {
            "user_id": uid, "nickname": "", "card": "", "role": "member", "title": "",
            "join_time": int(time.time()), "last_sent_time": 0,
        }
        member.update({k: v for k, v in fields.items() if v is not None})
        state["members"][gid][uid] = member
        self._dir_bump()
        if gid in state["groups"]:
            state["groups"][gid]["member_count"] = len(state["members"][gid])
        self._dir_save_member_row(gid, uid, member)

    @staticmethod
    def _dir_level(value) -> int:
        """协议端的群等级（LLBot 是字符串 "35"；取不到时为 0）。"""
        try:
            return max(0, int(str(value or "0").strip() or 0))
        except (TypeError, ValueError):
            return 0

    def _dir_remove_member(self, gid, uid) -> None:
        gid, uid = str(gid), str(uid)
        state = self._dir()
        if state["members"].get(gid, {}).pop(uid, None) is not None:
            self._dir_bump()
            if gid in state["groups"]:
                state["groups"][gid]["member_count"] = len(state["members"][gid])
            self._dir_save_member_row(gid, uid, None)

    def _dir_set_group_name(self, gid, name: str) -> None:
        gid = str(gid)
        group = self._dir()["groups"].get(gid)
        if group is not None and group.get("group_name") != name:
            group["group_name"] = name
            self._dir_bump()
            self._dir_save_groups(self._dir()["groups"], int(self._dir()["groups_ts"] or time.time()))

    def _dir_forget_group(self, gid) -> None:
        gid = str(gid)
        state = self._dir()
        state["groups"].pop(gid, None)
        state["members"].pop(gid, None)
        state["members_ts"].pop(gid, None)
        self._dir_bump()
        self._dir_save_groups(state["groups"], int(state["groups_ts"] or time.time()))

    async def _dir_handle_notice(self, event) -> None:
        """入群 / 退群 / 被踢 / 管理员任免 / 改名片通知实时更新缓存。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict) or raw.get("post_type") != "notice":
            return
        kind = raw.get("notice_type")
        gid = str(raw.get("group_id", "") or "")
        uid = str(raw.get("user_id", "") or "")
        if not gid or not uid:
            return
        self_id = str(raw.get("self_id", "") or "")
        if kind == "group_increase":
            if uid == self_id:
                self._dir()["groups_ts"] = 0  # 机器人进了新群：下次读取时刷新群列表
                return
            self._dir_upsert_member(gid, uid, role="member", join_time=int(time.time()))
        elif kind == "group_decrease":
            if uid == self_id or raw.get("sub_type") == "kick_me":
                self._dir_forget_group(gid)
            else:
                self._dir_remove_member(gid, uid)
        elif kind == "group_admin":
            self._dir_upsert_member(gid, uid, role="admin" if raw.get("sub_type") == "set" else "member")
        elif kind == "group_card":
            self._dir_upsert_member(gid, uid, card=str(raw.get("card_new") or ""))

    # ------------------------------------------------------------------
    # 批量刷新（WebUI 与后台）
    # ------------------------------------------------------------------
    def _dir_refresh_progress(self) -> Optional[dict]:
        task = getattr(self, "_dir_refresh_task", None)
        if task is None or task.done():
            return None
        return dict(getattr(self, "_dir_refresh_info", {}) or {})

    async def _dir_refresh_groups(self, client, groups: Optional[List[str]] = None) -> dict:
        """逐个限速刷新：先刷新群列表，再刷新 groups（None = 全部）的成员。"""
        listed, err = await self._dir_groups(client, force=True)
        targets = [g["group_id"] for g in listed] if groups is None else [str(g) for g in groups]
        info = {"total": len(targets), "done": 0, "failed": [], "error": err}
        self._dir_refresh_info = info
        for gid in targets:
            members = await self._dir_members(client, gid, force=True)
            if members is None:
                info["failed"].append(gid)
            info["done"] += 1
        return info

    def _dir_start_refresh(self, client, groups: Optional[List[str]] = None) -> bool:
        """在后台启动批量刷新；已在进行时返回 False。"""
        if self._dir_refresh_progress() is not None:
            return False
        self._dir_refresh_info = {"total": 0, "done": 0, "failed": [], "error": ""}
        self._dir_refresh_task = asyncio.create_task(self._dir_refresh_groups(client, groups))
        return True

    async def _dir_background_tick(self) -> None:
        if not self._cfg("directory_background_refresh_enabled", True):
            return
        if not self.config.get("disclaimer_agreed", False) or not self._cfg("enabled"):
            return
        client = await self._get_client()
        if not client:
            return
        groups, _err = await self._dir_groups(client)
        # 进群自动设置消息接收方式的漏报兜底（group_msg_mask.py）：群列表里出现没处理过的新群就补上
        check_mask = getattr(self, "_mm_check_new_groups", None)
        if callable(check_mask) and groups:
            try:
                await check_mask(client, [g["group_id"] for g in groups])
            except Exception as e:
                logger.debug(f"[GroupMgr] 检查新群消息接收方式失败: {e}")
        stale = [g["group_id"] for g in groups if not self._dir_members_fresh(g["group_id"])]
        stale.sort(key=lambda gid: self._dir()["members_ts"].get(gid, 0))
        for gid in stale[: self.DIR_BACKGROUND_BATCH]:
            await self._dir_members(client, gid)

    async def _dir_background_loop(self) -> None:
        while not getattr(self, "_scheduler_stop", False):
            await asyncio.sleep(self.DIR_BACKGROUND_TICK)
            try:
                await self._dir_background_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 群成员缓存后台刷新出错: {e}")
