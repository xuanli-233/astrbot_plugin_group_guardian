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
- 成员身份（群主 / 管理员 / 成员）：通知会实时更新，所以缓存过了有效期也仍然可信，在
  _dir_role_trust_seconds（默认 24 小时，或成员有效期的 4 倍）内直接用，不调接口；每条群消息自带的
  发送者身份、群名片、昵称顺带写进缓存（_dir_learn_from_event，只有变了才写库）；不得不调
  get_group_member_info 时把结果也写回缓存（_dir_learn_member）。命中 / 调接口次数见 _dir_stats。
- 黑名单成员扫描：每次从协议端拉到某个群的成员列表后，交给 group_blacklist._bl_scan_schedule
  检查里面有没有黑名单用户并踢出。机器人进新群后 DIR_JOIN_FETCH_DELAY 秒拉一次该群成员；
  机器人被设为管理员时按缓存再扫一遍。
"""
import asyncio
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger


class GroupDirectoryMixin:
    DIR_BACKGROUND_TICK = 300
    DIR_BACKGROUND_BATCH = 2
    # 机器人进新群后等几秒再拉成员列表（协议端刚进群时列表可能还没同步好）
    DIR_JOIN_FETCH_DELAY = 5
    # 成员身份在缓存里至少可信这么久（过了成员有效期也照用：任免管理员有通知实时更新）
    DIR_ROLE_TRUST_MIN = 86400
    DIR_ROLES = ("owner", "admin", "member")

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

    async def _dir_members(self, client, gid, force: bool = False, trigger: str = "") -> Optional[Dict[str, dict]]:
        """某群成员 {QQ号: 成员信息}。拉取失败且没有缓存时返回 None。

        trigger：这次为什么拉取（黑名单成员扫描的操作记录里写明），不填 = 刷新群成员。
        """
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
            # 拉到最新成员：检查有没有黑名单用户（机器人后进的群，入群即踢管不到早就在里面的人）
            scan = getattr(self, "_bl_scan_schedule", None)
            if callable(scan):
                try:
                    scan(client, gid, members, trigger or "刷新群成员")
                except Exception as e:
                    logger.debug(f"[GroupMgr] 安排黑名单成员扫描失败({gid}): {e}")
            return members

    def _dir_cached_member(self, gid, uid) -> Optional[dict]:
        return (self._dir()["members"].get(str(gid)) or {}).get(str(uid))

    def _dir_role_trust_seconds(self) -> int:
        return max(self._dir_member_ttl() * 4, self.DIR_ROLE_TRUST_MIN)

    def _dir_count(self, key: str, n: int = 1) -> None:
        """本次运行的计数（WebUI「群成员缓存」页显示）：role_hits 身份查询命中缓存 / role_api 调接口查身份 /
        learned 从消息、查询结果补进缓存的成员信息。"""
        stats = getattr(self, "_dir_stats", None)
        if stats is None:
            stats = self._dir_stats = {"role_hits": 0, "role_api": 0, "learned": 0}
        stats[key] = stats.get(key, 0) + n

    def _dir_member_role(self, gid, uid) -> str:
        """成员身份。这个群的成员缓存在可信期内、且里面有这个人时返回身份，否则返回空串（调用方再走接口）。

        不要求缓存没过期：任免管理员、入群、退群都有通知实时改缓存，过期只说明很久没整体拉过列表。
        """
        gid = str(gid)
        state = self._dir()
        ts = state["members_ts"].get(gid, 0)
        member = None
        if ts and time.time() - ts < self._dir_role_trust_seconds():
            member = (state["members"].get(gid) or {}).get(str(uid))
        if member and member.get("role"):
            self._dir_count("role_hits")
            return member["role"]
        return ""

    def _dir_learn_member(self, gid, uid, role=None, card=None, nickname=None, join_time=None,
                          title=None, level=None) -> bool:
        """把零散得到的成员信息（消息里的发送者、单个成员查询结果）补进缓存。返回是否有变化。

        只补已经整体缓存过成员的群（没缓存过的群，下次整体拉取）；值没变时什么也不做，不写库。
        """
        gid, uid = str(gid or ""), str(uid or "")
        if not gid or not uid.isdigit():
            return False
        state = self._dir()
        members = state["members"].get(gid)
        if members is None:
            return False
        fields = {}
        if role in self.DIR_ROLES:
            fields["role"] = role
        for key, value in (("card", card), ("nickname", nickname), ("title", title)):
            if value is not None:
                fields[key] = str(value)
        if join_time:
            try:
                fields["join_time"] = int(join_time)
            except (TypeError, ValueError):
                pass
        if level is not None and self._dir_level(level):
            fields["level"] = self._dir_level(level)
        member = members.get(uid)
        if member is None:
            member = {"user_id": uid, "nickname": "", "card": "", "role": "member", "title": "",
                      "join_time": 0, "last_sent_time": 0, "level": 0}
        elif all(member.get(k) == v for k, v in fields.items()):
            return False
        member.update(fields)
        members[uid] = member
        self._dir_bump()
        if gid in state["groups"]:
            state["groups"][gid]["member_count"] = max(len(members), int(state["groups"][gid].get("member_count") or 0))
        self._dir_count("learned")
        self._dir_save_member_row(gid, uid, member)
        return True

    def _dir_learn_from_event(self, event) -> bool:
        """群消息自带发送者的身份、群名片、昵称：顺带更新缓存，省掉之后的身份查询。"""
        raw = self._get_raw_event(event) if callable(getattr(self, "_get_raw_event", None)) else None
        if not isinstance(raw, dict) or raw.get("post_type") != "message" or raw.get("message_type") != "group":
            return False
        sender = raw.get("sender")
        if not isinstance(sender, dict):
            return False
        return self._dir_learn_member(raw.get("group_id"), raw.get("user_id"), role=sender.get("role"),
                                      card=sender.get("card"), nickname=sender.get("nickname"),
                                      title=sender.get("title"), level=sender.get("level"))

    def _dir_learn_info(self, gid, uid, info) -> None:
        """get_group_member_info 的结果：计一次接口查询，并写回缓存。"""
        self._dir_count("role_api")
        if isinstance(info, dict):
            self._dir_learn_member(gid, uid, role=info.get("role"), card=info.get("card"),
                                   nickname=info.get("nickname"), join_time=info.get("join_time"),
                                   title=info.get("title"), level=info.get("level"))

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
        unique = set()
        for members in state["members"].values():
            unique.update(members)
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
            # 群成员总数：member_count 是 QQ 报的群人数（拉过成员的群为实际成员数），各群相加；
            # unique_members 是已缓存成员去重后的人数（同一个人在多个群只算一次）
            "total_members": sum(int(g.get("member_count") or 0) for g in groups),
            "cached_members": sum(g["members_cached"] for g in groups),
            "cached_groups": sum(1 for g in groups if g["members_cached"]),
            "unique_members": len(unique),
            "role_trust_seconds": self._dir_role_trust_seconds(),
            "stats": dict(getattr(self, "_dir_stats", None) or {"role_hits": 0, "role_api": 0, "learned": 0}),
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
                # 立即拉一次成员：顺带检查群里有没有黑名单用户（他们早于机器人进群，不会有入群通知）
                self._dir_spawn(self._dir_fetch_new_group(event, gid))
                return
            self._dir_upsert_member(gid, uid, role="member", join_time=int(time.time()))
        elif kind == "group_decrease":
            if uid == self_id or raw.get("sub_type") == "kick_me":
                self._dir_forget_group(gid)
            else:
                self._dir_remove_member(gid, uid)
        elif kind == "group_admin":
            self._dir_upsert_member(gid, uid, role="admin" if raw.get("sub_type") == "set" else "member")
            if uid == self_id and raw.get("sub_type") == "set":
                # 机器人刚成为管理员：之前因为不是管理员没踢的黑名单用户，现在可以踢了
                self._dir_spawn(self._dir_scan_after_admin(event, gid))
        elif kind == "group_card":
            self._dir_upsert_member(gid, uid, card=str(raw.get("card_new") or ""))

    def _dir_spawn(self, coro) -> None:
        tasks = getattr(self, "_dir_tasks", None)
        if tasks is None:
            tasks = self._dir_tasks = set()
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _dir_scan_wanted(self, gid: str) -> bool:
        """有黑名单用户、且这个群开着黑名单成员扫描时才值得为它专门拉一次成员。"""
        ready = getattr(self, "_bl_scan_enabled", None)
        return bool(getattr(self, "_user_black_set", None)) and callable(ready) and ready(gid)

    async def _dir_fetch_new_group(self, event, gid: str) -> None:
        try:
            if not self._dir_scan_wanted(gid):
                return
            await asyncio.sleep(self.DIR_JOIN_FETCH_DELAY)
            client = await self._get_client(event)
            if client:
                await self._dir_members(client, gid, force=True, trigger="机器人进群")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[GroupMgr] 进群后拉取群 {gid} 成员失败: {e}")

    async def _dir_scan_after_admin(self, event, gid: str) -> None:
        try:
            if not self._dir_scan_wanted(gid):
                return
            client = await self._get_client(event)
            scan = getattr(self, "_bl_scan_cached", None)
            if client and callable(scan):
                await scan(client, gid, trigger="机器人成为管理员")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[GroupMgr] 成为管理员后扫描群 {gid} 黑名单用户失败: {e}")

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
