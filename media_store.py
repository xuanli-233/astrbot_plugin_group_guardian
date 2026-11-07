# -*- coding: utf-8 -*-
"""违规记录（审核日志）里的图片落盘保存（二次开发新增）。

审核日志原来只记图片地址：QQ 图片链接带时效参数，过一段时间就打不开；AstrBot 下载到本地的图片在
data/temp/ 里，会被清理，而且浏览器打不开本机路径。现在每条日志写入后在后台把图片读出来（本地路径直接读，
链接则下载），压缩到 500KB 以内存到插件数据目录 log_images/（同一张图只存一份），日志里的地址换成
「saved:<sha1>」。WebUI 显示缩略图、点开看大图，全部走插件接口，不再出现本机路径。

读不到的（文件已被清理、链接已失效）：http(s) 链接原样保留（可能还能打开），本机路径换成
「failed:本地图片」，不再显示路径。
开关 log_image_save_enabled（默认开），图片保留 log_image_days 天（默认 30，到期只删图片，日志保留）。
"""
import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger

try:  # 图片压缩 / 缩略图与码记录共用
    from .code_limit import _compress_image, _thumb
except ImportError:
    try:
        from code_limit import _compress_image, _thumb
    except ImportError:
        def _compress_image(data, max_bytes):
            return data, "bin", (0, 0)

        def _thumb(data, size=96):
            return ""

SAVED_PREFIX = "saved:"
FAILED_PREFIX = "failed:"
_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
         "webp": "image/webp", "bmp": "image/bmp"}


def is_sha1(text: str) -> bool:
    text = str(text or "")
    return len(text) == 40 and all(c in "0123456789abcdef" for c in text)


class MediaStoreMixin:
    LOG_IMG_MAX_BYTES = 500 * 1024          # 存盘前压缩到不超过这么大
    LOG_IMG_DOWNLOAD_MAX = 15 * 1024 * 1024
    LOG_IMG_THUMB = 160
    LOG_IMG_PER_LOG = 5
    LOG_IMG_CLEAN_INTERVAL = 3600

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _ms_init_db(self) -> None:
        if getattr(self, "_ms_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS log_images ("
                "hash TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, "
                "thumb TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_log_images_created ON log_images(created_at)")
            conn.commit()
        self._ms_db_ready = True

    def _ms_dir(self) -> Optional[Path]:
        base = getattr(self, "_data_dir", None)
        if not base:
            return None
        path = Path(base) / "log_images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    async def _ms_offload(fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, fn, *args)
        except RuntimeError:  # 没有线程池（Pyodide 模拟环境）
            return fn(*args)

    async def _ms_save(self, data: bytes) -> str:
        """图片字节 → 压缩存盘，返回 sha1（同一张图只存一份）。"""
        folder = self._ms_dir()
        if folder is None or not data:
            return ""
        h = hashlib.sha1(data).hexdigest()
        self._ms_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT path FROM log_images WHERE hash=?", (h,)).fetchone()
        if row and Path(row["path"]).exists():
            return h
        out, ext, _size = await self._ms_offload(_compress_image, data, self.LOG_IMG_MAX_BYTES)
        thumb = await self._ms_offload(_thumb, out, self.LOG_IMG_THUMB)
        path = folder / f"{h}.{ext}"
        await self._ms_offload(path.write_bytes, out)
        with self._storage._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO log_images(hash, path, bytes, thumb, created_at) VALUES(?,?,?,?,?)",
                         (h, str(path), len(out), thumb or "", int(time.time())))
            conn.commit()
        return h

    def _ms_read(self, h: str) -> str:
        """sha1 → data URL（WebUI 看大图）。没有或已清理返回空。"""
        h = str(h or "").strip().lower()
        if not is_sha1(h):
            return ""
        self._ms_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT path FROM log_images WHERE hash=?", (h,)).fetchone()
        if not row:
            return ""
        try:
            data = Path(row["path"]).read_bytes()
        except OSError:
            return ""
        mime = _MIME.get(Path(row["path"]).suffix.lower().lstrip("."), "application/octet-stream")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    def _ms_thumbs(self, hashes: List[str]) -> Dict[str, str]:
        hashes = sorted({h for h in hashes if is_sha1(h)})
        found: Dict[str, str] = {}
        if not hashes:
            return found
        self._ms_init_db()
        with self._storage._connect() as conn:
            for i in range(0, len(hashes), 500):
                part = hashes[i:i + 500]
                marks = ",".join("?" * len(part))
                for r in conn.execute(f"SELECT hash, thumb FROM log_images WHERE hash IN ({marks})", part):
                    found[r["hash"]] = r["thumb"] or ""
        return found

    def _ms_days(self) -> int:
        return min(max(self._cfg_int("log_image_days", 30), 1), 3650)

    def _ms_clean(self, now: Optional[int] = None) -> int:
        """删除保存超过 log_image_days 天的图片（日志本身不动）。返回删除的张数。"""
        cutoff = int(now or time.time()) - self._ms_days() * 86400
        self._ms_init_db()
        with self._storage._connect() as conn:
            rows = conn.execute("SELECT hash, path FROM log_images WHERE created_at < ?", (cutoff,)).fetchall()
            for r in rows:
                try:
                    os.remove(r["path"])
                except OSError:
                    pass
                conn.execute("DELETE FROM log_images WHERE hash=?", (r["hash"],))
            conn.commit()
        return len(rows)

    def _ms_maybe_clean(self) -> None:
        now = int(time.time())
        if now - int(getattr(self, "_ms_last_clean", 0) or 0) < self.LOG_IMG_CLEAN_INTERVAL:
            return
        self._ms_last_clean = now
        try:
            self._ms_clean(now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理违规记录图片失败: {e}")

    # ------------------------------------------------------------------
    # 审核日志
    # ------------------------------------------------------------------
    def _ml_schedule_images(self, log_entry: dict) -> None:
        """_log_moderation 写完日志后调用：后台保存图片并改写日志里的地址。"""
        urls = [u for u in (log_entry.get("image_urls") or []) if u and not str(u).startswith(
            (SAVED_PREFIX, FAILED_PREFIX))]
        if not urls or not self._cfg("log_image_save_enabled", True):
            return
        tasks = getattr(self, "_ms_tasks", None)
        if tasks is None:
            tasks = self._ms_tasks = set()

        async def run():
            try:
                await self._ml_save_log_images(log_entry)
            except Exception as e:
                logger.debug(f"[GroupMgr] 保存违规记录图片失败: {e}")

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _ml_fetch(self, url: str) -> Optional[bytes]:
        download = getattr(self, "_download_bytes", None)
        if not callable(download):
            return None
        return await download(url, max_bytes=self.LOG_IMG_DOWNLOAD_MAX)

    async def _ml_save_log_images(self, log_entry: dict) -> List[str]:
        """把日志里的图片地址逐个保存，地址换成 saved:<sha1> / 保留链接 / failed:本地图片，写回数据库和内存。"""
        out = []
        for url in list(log_entry.get("image_urls") or [])[:self.LOG_IMG_PER_LOG]:
            url = str(url or "")
            if not url or url.startswith((SAVED_PREFIX, FAILED_PREFIX)):
                out.append(url)
                continue
            h = ""
            try:
                data = await self._ml_fetch(url)
                if data:
                    h = await self._ms_save(data)
            except Exception as e:
                logger.debug(f"[GroupMgr] 违规记录图片读取失败: {e}")
            if h:
                out.append(SAVED_PREFIX + h)
            elif url.lower().startswith(("http://", "https://")):
                out.append(url)
            else:  # 本机路径 / file://：浏览器打不开，不再显示路径
                out.append(FAILED_PREFIX + "本地图片")
        log_entry["image_urls"] = out  # 内存里的那份（_moderation_logs 里是同一个 dict）
        try:
            with self._storage._connect() as conn:
                conn.execute("UPDATE moderation_logs SET image_urls=? WHERE id=?",
                             (json.dumps(out, ensure_ascii=False), int(log_entry.get("id") or 0)))
                conn.commit()
        except Exception as e:
            logger.debug(f"[GroupMgr] 更新违规记录图片地址失败: {e}")
        self._ms_maybe_clean()
        return out

    def _ml_describe_images(self, image_urls: List[str]) -> dict:
        """日志详情接口用：saved → 缩略图，http 链接原样，其余记为未能保存。"""
        saved = [str(u)[len(SAVED_PREFIX):] for u in image_urls or [] if str(u).startswith(SAVED_PREFIX)]
        thumbs = self._ms_thumbs(saved)
        images = [{"hash": h, "thumb": thumbs.get(h, ""), "expired": h not in thumbs} for h in saved]
        links = [u for u in image_urls or [] if str(u).lower().startswith(("http://", "https://"))]
        failed = sum(1 for u in image_urls or []
                     if not str(u).startswith(SAVED_PREFIX) and not str(u).lower().startswith(("http://", "https://")))
        return {"images": images, "links": links, "failed": failed}
