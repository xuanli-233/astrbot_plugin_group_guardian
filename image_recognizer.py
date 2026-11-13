# -*- coding: utf-8 -*-
"""图片识别器对接：微信葵花码识别 / 本地 OCR（最小化插件，二次开发新增）。

两类识别器共用一套接口，返回「识别出的文字列表」，由 keyword_guard 做规则过滤：
  kind = "wxcode"     葵花码（小程序码）识别，配置前缀 wxcode_
  kind = "local_ocr"  图片文字识别，配置前缀 local_ocr_

驱动（<kind>_driver）：
  http    POST 到 <kind>_service_url，请求体 JSON：
            {"kind": "wxcode", "image_url": "...", "image_base64": "...", "group_id": "..."}
          返回 JSON，以下任一形式均可：
            ["文本1", "文本2"]
            {"texts": ["文本1"]} / {"texts": [{"text": "文本1"}]}
            {"results": [...]} / {"data": ...} / {"text": "文本1"}
  python  加载 <kind>_python_module 指定的 .py 文件（相对路径相对插件数据目录下的 extensions/），
          调用其中的 recognize(image_bytes: bytes, image_url: str)，可为同步或 async 函数，
          返回值格式同上。
  llbot   （仅 local_ocr）调用 LuckyLilliaBot 自带的 ocr_image 接口。
  rapidocr（仅 local_ocr）对接 RapidOCR 文字识别服务（service_api/文字识别服务API.md）：
          POST {local_ocr_service_url}/v1/ocr，multipart 字段 file + detail=false，
          返回 {"text", "texts": [...], "count", "timing_ms"}；没有文字也是 200（count=0）。
          地址留空时用 http://127.0.0.1:8110。服务单进程串行、等待队列有限：插件同时最多发
          OCR_SERVICE_INFLIGHT 个请求，503（队列满）按 Retry-After 有限次重试；
          415 / 413 / 422（格式不支持、太大、图片损坏）跳过这张图；GIF 不发给服务。
          同一张图（按内容哈希）OCR_CACHE_TTL 秒内只识别一次。服务不可用时放行并记日志，不误撤回。
  off     关闭。

图片下载复用 image_audit 的受控下载（大小上限、超时、禁止内网地址）。
每条消息最多识别 RECOGNIZE_MAX_IMAGES 张图片。
没识别成功的原因（下载失败、服务不可用、格式跳过）记在 RECOGNIZE_NOTES，实时日志据此说明。
"""
import asyncio
import base64
import contextvars
import hashlib
import importlib.util
import inspect
import os
import time
from pathlib import Path
from typing import List, Tuple

from astrbot.api import logger

try:  # 下载失败原因（image_audit.py）
    from .image_audit import last_download_error
except ImportError:
    try:
        from image_audit import last_download_error
    except ImportError:
        def last_download_error():
            return ""

# 最近一次 _recognize_images 里没识别成功的图片的原因（每张图一条）
RECOGNIZE_NOTES = contextvars.ContextVar("group_guardian_recognize_notes", default=())

OCR_SERVICE_DEFAULT_URL = "http://127.0.0.1:8110"


class OcrServiceError(Exception):
    """文字识别服务调用失败（连不上、超时、队列一直满、服务异常）。按放行处理。"""


class OcrSkipped(Exception):
    """这张图不做文字识别（格式不支持、太大、图片损坏）。按没有文字处理。"""


class ImageRecognizerMixin:
    RECOGNIZE_MAX_IMAGES = 4
    RECOGNIZE_CONCURRENCY = 2
    OCR_SERVICE_RETRIES = 2
    OCR_SERVICE_INFLIGHT = 4
    OCR_CACHE_TTL = 600
    OCR_CACHE_MAX = 500

    # ------------------------------------------------------------------
    # 结果解析
    # ------------------------------------------------------------------
    @classmethod
    def _parse_recognizer_output(cls, value, depth: int = 0) -> List[str]:
        if value is None or depth > 4:
            return []
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", "ignore")
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, dict):
            for key in ("texts", "results", "data", "result", "items"):
                if key in value:
                    return cls._parse_recognizer_output(value[key], depth + 1)
            for key in ("text", "content", "value"):
                if isinstance(value.get(key), str):
                    return cls._parse_recognizer_output(value[key], depth + 1)
            return []
        if isinstance(value, (list, tuple)):
            out: List[str] = []
            for item in value:
                out.extend(cls._parse_recognizer_output(item, depth + 1))
            return out
        return []

    # ------------------------------------------------------------------
    # 驱动
    # ------------------------------------------------------------------
    def _recognizer_timeout(self, kind: str) -> float:
        return float(max(1, min(self._cfg_int(f"{kind}_timeout", 10), 60)))

    async def _recognize_http(self, kind: str, image_url: str, data: bytes, group_id: str) -> List[str]:
        url = str(self.config.get(f"{kind}_service_url", "") or "").strip()
        if not url:
            return []
        import aiohttp
        payload = {
            "kind": kind,
            "image_url": image_url,
            "image_base64": base64.b64encode(data).decode("ascii") if data else "",
            "group_id": str(group_id or ""),
        }
        timeout = aiohttp.ClientTimeout(total=self._recognizer_timeout(kind))
        # 识别服务由管理员配置，通常是本机/内网地址，不走图片下载的公网限制
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    logger.warning(f"[GroupMgr] {kind} 识别服务返回 HTTP {resp.status}")
                    return []
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
        return self._parse_recognizer_output(body)

    def _load_recognizer_module(self, kind: str):
        raw = str(self.config.get(f"{kind}_python_module", "") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = Path(self._get_data_dir()) / "extensions" / raw
        if not path.is_file():
            logger.warning(f"[GroupMgr] {kind} 识别模块不存在: {path}")
            return None
        mtime = os.path.getmtime(path)
        cache = getattr(self, "_recognizer_modules", None)
        if cache is None:
            cache = self._recognizer_modules = {}
        hit = cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        spec = importlib.util.spec_from_file_location(f"gg_recognizer_{kind}", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "recognize", None)):
            logger.warning(f"[GroupMgr] {kind} 识别模块缺少 recognize(image_bytes, image_url) 函数: {path}")
            return None
        cache[str(path)] = (mtime, module)
        return module

    async def _recognize_python(self, kind: str, image_url: str, data: bytes) -> List[str]:
        module = self._load_recognizer_module(kind)
        if module is None:
            return []
        func = module.recognize
        if inspect.iscoroutinefunction(func):
            result = await asyncio.wait_for(func(data, image_url), timeout=self._recognizer_timeout(kind))
        else:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, func, data, image_url),
                timeout=self._recognizer_timeout(kind),
            )
        return self._parse_recognizer_output(result)

    async def _recognize_llbot_ocr(self, image_url: str) -> List[str]:
        client = await self._get_client()
        if not client:
            return []
        ok, data, error = await self._call_group_api_result(client, "ocr_image", "图片OCR", image=image_url)
        if not ok:
            logger.debug(f"[GroupMgr] ocr_image 失败: {error}")
            return []
        return self._parse_recognizer_output(data)

    # ------------------------------------------------------------------
    # RapidOCR 文字识别服务（rapidocr 驱动）
    # ------------------------------------------------------------------
    def _ocr_service_base(self) -> str:
        """服务地址可以只写 http://ip:端口，也可以写到 /v1/ocr；留空用本机默认端口。"""
        base = str(self.config.get("local_ocr_service_url", "") or "").strip().rstrip("/")
        for suffix in ("/v1/ocr", "/health", "/ready"):
            if base.endswith(suffix):
                base = base[:-len(suffix)].rstrip("/")
        return base or OCR_SERVICE_DEFAULT_URL

    @staticmethod
    def _ocr_image_type(data: bytes) -> Tuple[str, str]:
        """按文件头判断 (MIME, 上传用的文件名)。"""
        head = bytes(data[:12] or b"")
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png", "image.png"
        if head[:3] == b"\xff\xd8\xff":
            return "image/jpeg", "image.jpg"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp", "image.webp"
        if head[:2] == b"BM":
            return "image/bmp", "image.bmp"
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif", "image.gif"
        return "application/octet-stream", "image.img"

    # HTTP（测试与模拟环境可整体替换这两个方法）
    async def _ocr_http_post(self, url: str, data: bytes, filename: str, mime: str,
                             timeout: float) -> Tuple[int, dict, dict]:
        """multipart 上传一张图片，返回 (HTTP 状态码, JSON 响应体, 响应头)。"""
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=mime)
        form.add_field("detail", "false")
        # 识别服务由管理员配置，通常是本机地址，不走图片下载的公网限制
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, data=form) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, body if isinstance(body, dict) else {}, dict(resp.headers)

    async def _ocr_http_get(self, url: str, timeout: float = 5.0) -> Tuple[int, dict]:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, body if isinstance(body, dict) else {}

    @staticmethod
    def _ocr_error_text(body: dict) -> str:
        """从错误响应里取一句说明（字段名按常见写法都试一下）。"""
        if not isinstance(body, dict):
            return ""
        for key in ("detail", "error", "message", "code"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("code") or value.get("detail")
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()[:200]
        return ""

    def _ocr_warn(self, reason: str) -> None:
        """服务不可用写一条 warning；同一原因每 10 分钟只写一次，避免每张图都刷一行。"""
        seen = getattr(self, "_ocr_warned", None)
        if seen is None:
            seen = self._ocr_warned = {}
        now = time.time()
        key = str(reason)[:80]
        if now - seen.get(key, 0) < 600:
            return
        if len(seen) > 200:
            seen.clear()
        seen[key] = now
        logger.warning(f"[GroupMgr] 图片文字识别失败（已放行）：{reason}")

    async def _ocr_service_recognize(self, data: bytes) -> dict:
        """调用文字识别服务。返回 {"texts": [...], "ms": 服务端识别耗时或 None, "cached": bool}。

        不识别的图片抛 OcrSkipped，服务不可用抛 OcrServiceError。
        """
        mime, filename = self._ocr_image_type(data)
        if mime == "image/gif":
            raise OcrSkipped("GIF 动图不做文字识别，已跳过")
        cache = getattr(self, "_ocr_cache", None)
        if cache is None:
            cache = self._ocr_cache = {}
        key = hashlib.sha1(data).hexdigest()
        hit = cache.get(key)
        if hit and time.time() - hit[0] < self.OCR_CACHE_TTL:
            return {"texts": list(hit[1]), "ms": None, "cached": True}
        url = self._ocr_service_base() + "/v1/ocr"
        timeout = self._recognizer_timeout("local_ocr")
        gate = getattr(self, "_ocr_gate", None)
        if gate is None:
            gate = self._ocr_gate = asyncio.Semaphore(self.OCR_SERVICE_INFLIGHT)
        try:
            await asyncio.wait_for(gate.acquire(), timeout=timeout * 3)
        except asyncio.TimeoutError:
            raise OcrServiceError("等待识别的图片太多，排队超时")
        try:
            body = {}
            for attempt in range(self.OCR_SERVICE_RETRIES + 1):
                try:
                    status, body, headers = await self._ocr_http_post(url, data, filename, mime, timeout)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    raise OcrServiceError(f"文字识别服务超时（{timeout:g} 秒）")
                except Exception as e:
                    raise OcrServiceError(f"文字识别服务连接失败：{str(e) or type(e).__name__}")
                if status == 200:
                    break
                if status == 503 and attempt < self.OCR_SERVICE_RETRIES:
                    try:
                        retry_after = float((headers or {}).get("Retry-After", 1))
                    except (TypeError, ValueError):
                        retry_after = 1.0
                    await asyncio.sleep(max(0.5, min(retry_after, 3.0)))
                    continue
                if status == 415:
                    raise OcrSkipped("图片格式不支持文字识别，已跳过")
                if status == 413:
                    raise OcrSkipped("图片超过文字识别服务的文件 / 像素限制，已跳过")
                if status == 422:
                    raise OcrSkipped("图片损坏或无法解析，已跳过")
                if status == 503:
                    raise OcrServiceError("文字识别服务繁忙（等待队列已满）")
                detail = self._ocr_error_text(body)
                raise OcrServiceError(f"文字识别服务返回 HTTP {status}" + (f"：{detail}" if detail else ""))
        finally:
            gate.release()
        raw = body.get("texts")
        if not isinstance(raw, list):
            raw = str(body.get("text") or "").split("\n")
        texts = [str(t).strip() for t in raw if isinstance(t, str) and t.strip()]
        timing = body.get("timing_ms") if isinstance(body.get("timing_ms"), dict) else {}
        ms = timing.get("ocr") if isinstance(timing.get("ocr"), (int, float)) else None
        if len(cache) >= self.OCR_CACHE_MAX:
            cache.clear()
        cache[key] = (time.time(), tuple(texts))
        return {"texts": texts, "ms": ms, "cached": False}

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def _recognize_bytes(self, kind: str, driver: str, data: bytes, image_url: str = "",
                               group_id: str = "") -> Tuple[List[str], str, dict]:
        """识别一张已取到的图片。返回 (文字列表, 没识别成功的原因, 附加信息)；不抛异常（取消除外）。"""
        try:
            if driver == "rapidocr" and kind == "local_ocr":
                result = await self._ocr_service_recognize(data)
                return result["texts"], "", {"ms": result["ms"], "cached": result["cached"]}
            if driver == "http":
                return await self._recognize_http(kind, image_url, data, group_id), "", {}
            if driver == "python":
                return await self._recognize_python(kind, image_url, data), "", {}
        except asyncio.CancelledError:
            raise
        except OcrSkipped as e:
            return [], str(e), {"skipped": True}
        except OcrServiceError as e:
            self._ocr_warn(str(e))
            return [], f"{e}（已放行）", {"failed": True}
        except asyncio.TimeoutError:
            logger.warning(f"[GroupMgr] {kind} 识别超时: {image_url[:80]}")
            return [], "识别超时（已放行）", {"failed": True}
        except Exception as e:
            logger.warning(f"[GroupMgr] {kind} 识别失败: {e}")
            return [], f"识别失败：{str(e)[:120] or type(e).__name__}（已放行）", {"failed": True}
        return [], "", {}

    async def _recognize_fetch(self, image_url: str):
        """取图片字节：base64:// 直接解码，其余走受控下载。"""
        text = str(image_url or "")
        if text.startswith("base64://"):
            try:
                return base64.b64decode(text[len("base64://"):], validate=False) or None
            except Exception:
                return None
        return await self._download_bytes(text)

    async def _recognize_one(self, kind: str, driver: str, image_url: str, group_id: str) -> Tuple[List[str], str]:
        """返回 (文字列表, 没识别成功的原因)。"""
        try:
            if driver == "llbot":
                return await self._recognize_llbot_ocr(image_url), ""
            data = await self._recognize_fetch(image_url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[GroupMgr] {kind} 识别失败: {e}")
            return [], f"识别失败：{str(e)[:120] or type(e).__name__}（已放行）"
        if not data:
            return [], f"图片下载失败：{last_download_error() or '未知原因'}"
        texts, note, _info = await self._recognize_bytes(kind, driver, data, image_url, group_id)
        return texts, note

    def _recognize_last_notes(self) -> List[str]:
        """最近一次 _recognize_images 里没识别成功的图片的原因。"""
        return list(RECOGNIZE_NOTES.get())

    async def _recognize_images(self, image_urls: list, kind: str, group_id: str = "") -> List[str]:
        """识别多张图片，按图片顺序返回所有文字（已去重）。"""
        RECOGNIZE_NOTES.set(())
        driver = self._cfg_str(f"{kind}_driver", "off").strip().lower()
        if driver in ("", "off") or (driver in ("llbot", "rapidocr") and kind != "local_ocr"):
            return []
        urls = []
        for url in image_urls or []:
            if url and url not in urls:
                urls.append(url)
            if len(urls) >= self.RECOGNIZE_MAX_IMAGES:
                break
        semaphore = asyncio.Semaphore(self.RECOGNIZE_CONCURRENCY)

        async def run(url):
            async with semaphore:
                return await self._recognize_one(kind, driver, url, group_id)

        results = await asyncio.gather(*(run(u) for u in urls))
        texts: List[str] = []
        notes: List[str] = []
        multi = len(urls) > 1
        for index, (items, note) in enumerate(results, 1):
            for text in items:
                if text not in texts:
                    texts.append(text)
            if note:
                notes.append((f"图{index} " if multi else "") + note)
        RECOGNIZE_NOTES.set(tuple(notes))
        return texts
