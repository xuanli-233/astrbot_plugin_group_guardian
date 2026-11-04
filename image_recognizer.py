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
  off     关闭。

图片下载复用 image_audit 的受控下载（大小上限、超时、禁止内网地址）。
每条消息最多识别 RECOGNIZE_MAX_IMAGES 张图片。
"""
import asyncio
import base64
import importlib.util
import inspect
import os
from pathlib import Path
from typing import List

from astrbot.api import logger


class ImageRecognizerMixin:
    RECOGNIZE_MAX_IMAGES = 4
    RECOGNIZE_CONCURRENCY = 2

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
    # 入口
    # ------------------------------------------------------------------
    async def _recognize_one(self, kind: str, driver: str, image_url: str, group_id: str) -> List[str]:
        try:
            if driver == "llbot":
                return await self._recognize_llbot_ocr(image_url)
            data = await self._download_bytes(image_url)
            if not data:
                return []
            if driver == "http":
                return await self._recognize_http(kind, image_url, data, group_id)
            if driver == "python":
                return await self._recognize_python(kind, image_url, data)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(f"[GroupMgr] {kind} 识别超时: {image_url[:80]}")
        except Exception as e:
            logger.warning(f"[GroupMgr] {kind} 识别失败: {e}")
        return []

    async def _recognize_images(self, image_urls: list, kind: str, group_id: str = "") -> List[str]:
        """识别多张图片，按图片顺序返回所有文字（已去重）。"""
        driver = self._cfg_str(f"{kind}_driver", "off").strip().lower()
        if driver in ("", "off") or (driver == "llbot" and kind != "local_ocr"):
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
        for items in results:
            for text in items:
                if text not in texts:
                    texts.append(text)
        return texts
