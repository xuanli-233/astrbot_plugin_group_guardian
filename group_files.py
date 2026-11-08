# -*- coding: utf-8 -*-
"""群文件跨群管理、批量发言、批量传群文件（二次开发新增）。WebUI「群文件」页、「批量发布」页。

群文件
  扫描勾选的群（根目录 + 一级文件夹，get_group_root_files / get_group_files_by_folder），每个群之间按
  directory_min_interval_ms 间隔，扫描在后台执行、页面轮询进度。结果只在内存（重启后重新扫描）。
  页面上按上传者 / 文件名 / 群筛选，「按上传者汇总」一键选中某人在所有群上传的文件；
  「重复文件」= 同一个群里文件名 + 大小都相同的（QQ 不返回 md5，按这两项判断），保留最早上传的一份。
  删除走批量任务队列（动作 delfile → delete_group_file），可暂停 / 重试，每删一个写操作记录「删除群文件」。

批量发言（动作 sendmsg → send_group_msg）：文字 + 可选一张图片。
批量传群文件（动作 upfile → upload_group_file）：文件传到插件数据目录 outbox/ 后，把本机路径交给协议端
  （AstrBot 与 LuckyLilliaBot 在同一台机器上时可用）；也可以直接填服务器上的文件路径（适合大文件）。
  outbox/ 里 7 天前的文件在下次上传时清理。
"""
import asyncio
import base64
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

GF_MAX_FOLDERS = 50            # 每个群最多扫描多少个文件夹
GF_MAX_FILES_PER_GROUP = 3000
GF_MAX_GROUPS = 500
OUTBOX_MAX_BYTES = 30 * 1024 * 1024       # 从浏览器上传的文件上限（经 JSON base64 传输，不宜过大）
OUTBOX_LOCAL_MAX_BYTES = 4 * 1024 * 1024 * 1024
OUTBOX_KEEP_SECONDS = 7 * 86400
SEND_TEXT_MAX = 4500
_SAFE_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_file_name(name: str) -> str:
    name = _SAFE_NAME.sub("_", str(name or "").strip()).strip(". ")
    return name[:120] or "file"


def file_item(group_id: str, raw: dict, folder_id: str = "", folder_name: str = "") -> dict:
    """协议端返回的一个文件 → 统一字段（go-cqhttp / LLOneBot 字段名）。"""
    def num(*keys):
        for k in keys:
            try:
                if raw.get(k) not in (None, ""):
                    return int(raw.get(k))
            except (TypeError, ValueError):
                continue
        return 0
    return {
        "group_id": str(group_id), "file_id": str(raw.get("file_id") or raw.get("id") or ""),
        "busid": num("busid"), "name": str(raw.get("file_name") or raw.get("name") or ""),
        "size": num("file_size", "size"), "uploader": str(raw.get("uploader") or raw.get("uploader_id") or ""),
        "uploader_name": str(raw.get("uploader_name") or ""), "upload_time": num("upload_time", "modify_time"),
        "download_times": num("download_times"), "folder_id": folder_id, "folder_name": folder_name,
    }


def duplicate_ids(files: List[dict], cross_group: bool = False) -> List[str]:
    """重复文件：文件名 + 大小相同（默认只在同一个群里比），每组保留最早上传的一份，返回其余的 key。"""
    groups: Dict[tuple, List[dict]] = {}
    for f in files:
        if not f.get("name"):
            continue
        key = (f["name"], f.get("size", 0)) if cross_group else (f["group_id"], f["name"], f.get("size", 0))
        groups.setdefault(key, []).append(f)
    out = []
    for items in groups.values():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda f: (f.get("upload_time") or 0, f.get("file_id")))
        out.extend(file_key(f) for f in items[1:])
    return out


def file_key(f: dict) -> str:
    return f"{f.get('group_id')}:{f.get('file_id')}"


IMAGE_INLINE_MAX = 20 * 1024 * 1024


def image_payload(image: str) -> Tuple[str, str]:
    """发给协议端的图片：本机图片（file:// 或绝对路径）读出来转成 base64://，链接原样。
    base64 各 OneBot 实现都支持，也不受中文文件名在 file:// 里被百分号编码的影响。返回 (值, 错误)。"""
    image = str(image or "").strip()
    if not image:
        return "", ""
    path = ""
    if image.startswith("file:"):
        from urllib.parse import unquote, urlparse
        from urllib.request import url2pathname
        path = url2pathname(unquote(urlparse(image).path))
    elif os.path.isabs(image):
        path = image
    if not path:
        return image, ""
    try:
        if not os.path.isfile(path):
            return "", "图片文件已不存在（outbox 里超过 7 天的文件会被清理）"
        if os.path.getsize(path) > IMAGE_INLINE_MAX:
            return "", "图片超过 20 MB"
        with open(path, "rb") as fh:
            return "base64://" + base64.b64encode(fh.read()).decode("ascii"), ""
    except OSError as e:
        return "", f"读取图片失败：{e}"


class GroupFilesMixin:
    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _gf_state(self) -> dict:
        st = getattr(self, "_gf_scan_state", None)
        if st is None:
            st = self._gf_scan_state = {"running": False, "started_at": 0, "finished_at": 0, "groups": [],
                                        "done": 0, "current": "", "files": [], "errors": {}, "folders": 0}
        return st

    def _gf_interval(self) -> float:
        return min(max(self._cfg_int("directory_min_interval_ms", 300), 0), 5000) / 1000.0

    async def _gf_list_folder(self, client, gid: str, folder_id: str) -> Tuple[bool, dict, str]:
        """get_group_files_by_folder：LLOneBot / go-cqhttp 用 folder_id，个别实现用 folder，失败时换一个再试。"""
        ok, data, err = await self._call_group_api_result(
            client, "get_group_files_by_folder", "获取群文件夹", group_id=self._safe_int(gid, 0), folder_id=folder_id)
        if not ok:
            ok2, data2, _err2 = await self._call_group_api_result(
                client, "get_group_files_by_folder", "获取群文件夹", group_id=self._safe_int(gid, 0), folder=folder_id)
            if ok2:
                return True, data2 if isinstance(data2, dict) else {}, ""
        return ok, data if isinstance(data, dict) else {}, err

    async def _gf_scan_group(self, client, gid: str) -> Tuple[List[dict], str, int]:
        """一个群的全部文件（根目录 + 一级文件夹）。返回 (文件, 错误, 文件夹数)。"""
        ok, data, err = await self._call_group_api_result(
            client, "get_group_root_files", "获取群文件", group_id=self._safe_int(gid, 0))
        if not ok:
            return [], err or "获取群文件失败", 0
        data = data if isinstance(data, dict) else {}
        files = [file_item(gid, f) for f in (data.get("files") or []) if isinstance(f, dict)]
        folders = [f for f in (data.get("folders") or []) if isinstance(f, dict)][:GF_MAX_FOLDERS]
        errors = []
        for folder in folders:
            folder_id = str(folder.get("folder_id") or folder.get("id") or "")
            if not folder_id:
                continue
            await asyncio.sleep(self._gf_interval())
            ok, sub, err = await self._gf_list_folder(client, gid, folder_id)
            if not ok:
                errors.append(f"文件夹「{folder.get('folder_name', '')}」：{err}")
                continue
            name = str(folder.get("folder_name") or "")
            files.extend(file_item(gid, f, folder_id, name) for f in (sub.get("files") or []) if isinstance(f, dict))
            if len(files) >= GF_MAX_FILES_PER_GROUP:
                break
        return files[:GF_MAX_FILES_PER_GROUP], "；".join(errors), len(folders)

    def _gf_start_scan(self, groups: List[str]) -> str:
        st = self._gf_state()
        if st["running"]:
            return "正在扫描中，请等这次扫描完成"
        groups = [str(g) for g in groups if str(g).strip()][:GF_MAX_GROUPS]
        if not groups:
            return "请选择要扫描的群"
        st.update(running=True, started_at=int(time.time()), finished_at=0, groups=groups, done=0, current="",
                  files=[], errors={}, folders=0)

        async def run():
            try:
                client = await self._get_client()
                if not client:
                    st["errors"]["*"] = "无法获取QQ客户端"
                    return
                for i, gid in enumerate(groups):
                    st["current"] = gid
                    if i:
                        await asyncio.sleep(self._gf_interval())
                    try:
                        files, err, folders = await self._gf_scan_group(client, gid)
                    except Exception as e:
                        files, err, folders = [], f"{type(e).__name__}: {e}", 0
                    st["files"].extend(files)
                    st["folders"] += folders
                    if err:
                        st["errors"][gid] = err
                    st["done"] = i + 1
            finally:
                st["running"] = False
                st["current"] = ""
                st["finished_at"] = int(time.time())

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            st["running"] = False
            return "无法启动后台扫描"
        tasks = getattr(self, "_gf_tasks", None)
        if tasks is None:
            tasks = self._gf_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return ""

    def _gf_snapshot(self, with_files: bool = True) -> dict:
        st = self._gf_state()
        out = {k: st[k] for k in ("running", "started_at", "finished_at", "groups", "done", "current", "errors", "folders")}
        out["total"] = len(st["groups"])
        out["file_count"] = len(st["files"])
        if with_files:
            out["files"] = st["files"]
            out["duplicates"] = duplicate_ids(st["files"])
        return out

    def _gf_forget(self, group_id: str, file_id: str) -> None:
        st = self._gf_state()
        st["files"] = [f for f in st["files"] if not (f["group_id"] == str(group_id) and f["file_id"] == str(file_id))]

    # ------------------------------------------------------------------
    # 删除（批量任务 delfile）
    # ------------------------------------------------------------------
    def _gf_delete_job(self, items: List[dict], reason: str = "", source: str = "WebUI", operator: str = "") -> dict:
        known = {file_key(f): f for f in self._gf_state()["files"]}
        job_items, seen = [], set()
        for it in items:
            key = f"{it.get('group_id')}:{it.get('file_id')}"
            f = known.get(key)
            if not f or key in seen:
                continue
            seen.add(key)
            job_items.append({"group_id": f["group_id"], "action": "delfile", "target": f["file_id"],
                              "label": f"删除「{f['name'][:40]}」" + (f"（上传者 {f['uploader']}）" if f["uploader"] else ""),
                              "params": {"busid": f["busid"], "name": f["name"], "uploader": f["uploader"],
                                         "size": f["size"], "reason": reason, "source": source, "operator": operator}})
        if not job_items:
            return {}
        uploaders = sorted({it["params"]["uploader"] for it in job_items if it["params"]["uploader"]})
        title = f"删除群文件 {len(job_items)} 个（{len({i['group_id'] for i in job_items})} 个群）"
        if len(uploaders) == 1:
            title += f"，上传者 {uploaders[0]}"
        return self._bq_create_job(title, "delfile", job_items, source=source, operator=operator,
                                   meta={"reason": reason})

    async def _bq_exec_delfile(self, client, gid: str, file_id: str, params: dict) -> Tuple[str, str]:
        ok, err = await self._call_group_api(client, "delete_group_file", "删除群文件", group_id=self._safe_int(gid, 0),
                                             file_id=file_id, busid=self._safe_int(params.get("busid"), 0))
        size = int(params.get("size") or 0)
        self._record_op("删除群文件", source=params.get("source", ""), operator=params.get("operator", ""),
                        group_id=gid, target=params.get("uploader", ""), reason=params.get("reason", ""),
                        result="成功" if ok else f"失败：{err}",
                        detail=f"{params.get('name', '')}（{size // 1024} KB）")
        if not ok:
            return "failed", err or "删除失败"
        self._gf_forget(gid, file_id)
        return "ok", "已删除"

    # ------------------------------------------------------------------
    # 待发送的文件（outbox）
    # ------------------------------------------------------------------
    def _outbox_dir(self) -> Optional[Path]:
        base = getattr(self, "_data_dir", None)
        if not base:
            return None
        path = Path(base) / "outbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _outbox_clean(self, now: Optional[int] = None) -> int:
        folder = self._outbox_dir()
        if folder is None:
            return 0
        cutoff = int(now or time.time()) - OUTBOX_KEEP_SECONDS
        removed = 0
        for p in folder.rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        for d in folder.iterdir():  # 删空了的子目录
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                continue
        return removed

    def _outbox_save(self, name: str, data_b64: str) -> Tuple[Optional[dict], str]:
        """浏览器上传的文件（base64）→ 存到 outbox/，返回 {path, name, size}。"""
        raw = str(data_b64 or "")
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        if len(raw) > OUTBOX_MAX_BYTES * 4 // 3 + 16:
            return None, f"文件超过 {OUTBOX_MAX_BYTES // 1024 // 1024} MB，请改用「服务器上的文件路径」"
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception:
            return None, "文件数据无效"
        if not data:
            return None, "文件是空的"
        if len(data) > OUTBOX_MAX_BYTES:
            return None, f"文件超过 {OUTBOX_MAX_BYTES // 1024 // 1024} MB，请改用「服务器上的文件路径」"
        folder = self._outbox_dir()
        if folder is None:
            return None, "插件数据目录不可用"
        try:
            self._outbox_clean()
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理 outbox 失败: {e}")
        name = safe_file_name(name)
        # 每个文件放在自己的子目录里，文件名保持原样（群里看到的就是这个名字）
        sub = folder / hashlib.sha1(data).hexdigest()[:12]
        sub.mkdir(exist_ok=True)
        path = sub / name
        path.write_bytes(data)
        return {"path": str(path.resolve()), "name": name, "size": len(data)}, ""

    @staticmethod
    def _outbox_local(path: str) -> Tuple[Optional[dict], str]:
        """服务器上已有的文件（与协议端同一台机器）：只检查存在和大小，不读取内容。"""
        text = str(path or "").strip().strip('"')
        if not text:
            return None, "请填写文件路径"
        p = Path(text)
        if not p.is_absolute():
            return None, "请填写完整路径（例如 D:\\资料\\通知.pdf）"
        try:
            if not p.is_file():
                return None, "找不到这个文件（路径要写服务器上的，不是你电脑上的）"
            size = p.stat().st_size
        except OSError as e:
            return None, f"无法读取文件信息：{e}"
        if size <= 0:
            return None, "文件是空的"
        if size > OUTBOX_LOCAL_MAX_BYTES:
            return None, "文件超过 4 GB"
        return {"path": str(p.resolve()), "name": p.name, "size": size}, ""

    # ------------------------------------------------------------------
    # 批量发言（sendmsg）/ 批量传群文件（upfile）
    # ------------------------------------------------------------------
    def _bulk_send_job(self, groups: List[str], text: str, image: Optional[dict] = None,
                       source: str = "WebUI", operator: str = "") -> Tuple[dict, str]:
        text = str(text or "").strip()
        if not text and not image:
            return {}, "请填写要发送的内容或选择图片"
        if len(text) > SEND_TEXT_MAX:
            return {}, f"内容过长（上限 {SEND_TEXT_MAX} 字）"
        preview = (text[:30] + ("…" if len(text) > 30 else "")) or "[图片]"
        items = [{"group_id": g, "action": "sendmsg", "label": f"发送「{preview}」",
                  "params": {"text": text, "image": (image or {}).get("path", ""), "source": source,
                             "operator": operator}} for g in groups]
        job = self._bq_create_job(f"批量发言到 {len(items)} 个群：{preview}", "sendmsg", items, source=source,
                                  operator=operator)
        return job, ""

    async def _bq_exec_sendmsg(self, client, gid: str, _target: str, params: dict) -> Tuple[str, str]:
        segments = []
        if params.get("text"):
            segments.append({"type": "text", "data": {"text": params["text"]}})
        image = params.get("image")
        if image:
            payload, err = image_payload(image)
            if err:
                return "failed", err
            segments.append({"type": "image", "data": {"file": payload}})
        ok, err = await self._call_group_api(client, "send_group_msg", "批量发言", group_id=self._safe_int(gid, 0),
                                             message=segments)
        self._record_op("批量发言", source=params.get("source", ""), operator=params.get("operator", ""),
                        group_id=gid, result="成功" if ok else f"失败：{err}",
                        detail=(params.get("text") or "")[:200] + (" [图片]" if image else ""))
        return ("ok", "已发送") if ok else ("failed", err or "发送失败")

    def _bulk_upload_job(self, groups: List[str], file: dict, name: str = "",
                         source: str = "WebUI", operator: str = "") -> Tuple[dict, str]:
        if not file or not file.get("path"):
            return {}, "请先选择文件"
        name = safe_file_name(name or file.get("name") or Path(file["path"]).name)
        items = [{"group_id": g, "action": "upfile", "label": f"上传「{name[:40]}」",
                  "params": {"path": file["path"], "name": name, "size": int(file.get("size") or 0),
                             "source": source, "operator": operator}} for g in groups]
        job = self._bq_create_job(f"批量传群文件到 {len(items)} 个群：{name}", "upfile", items, source=source,
                                  operator=operator)
        return job, ""

    # 上传大文件比较慢：单项超时放宽到 10 分钟（batch_queue._bq_execute 读这个）
    BQ_TIMEOUT_UPFILE = 600

    async def _bq_exec_upfile(self, client, gid: str, _target: str, params: dict) -> Tuple[str, str]:
        path = str(params.get("path") or "")
        if not path or not os.path.isfile(path):
            return "failed", "文件已不存在（outbox 里超过 7 天的文件会被清理）"
        # 不走 _call_group_api_result（它的协议端超时 20 秒，传大文件不够）
        try:
            result = await asyncio.wait_for(client.call_action(
                "upload_group_file", group_id=self._safe_int(gid, 0), file=path,
                name=params.get("name") or os.path.basename(path)), timeout=self.BQ_TIMEOUT_UPFILE - 15)
            ok, err = self._check_api_result(result, "上传群文件")
        except asyncio.TimeoutError:
            ok, err = False, f"上传超时（{self.BQ_TIMEOUT_UPFILE - 15} 秒）"
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        self._record_op("上传群文件", source=params.get("source", ""), operator=params.get("operator", ""),
                        group_id=gid, result="成功" if ok else f"失败：{err}",
                        detail=f"{params.get('name', '')}（{int(params.get('size') or 0) // 1024} KB）")
        return ("ok", "已上传") if ok else ("failed", err or "上传失败")
