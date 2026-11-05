"""单独运行某个测试文件时，为 onebot.py 等模块提供最小的 astrbot 替身（已有真实模块时不覆盖）。"""
import os
import sys
import types


def install():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None, error=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    if not hasattr(event_module, "AstrMessageEvent"):
        event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api
    for name in ("astrbot.core", "astrbot.core.platform", "astrbot.core.platform.sources",
                 "astrbot.core.platform.sources.aiocqhttp"):
        sys.modules.setdefault(name, types.ModuleType(name))
    aio = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"),
    )
    if not hasattr(aio, "AiocqhttpMessageEvent"):
        aio.AiocqhttpMessageEvent = object
