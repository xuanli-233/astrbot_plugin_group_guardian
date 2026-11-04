# -*- coding: utf-8 -*-
"""葵花码 / OCR 识别模块接入示例。

方式一（python）：把本文件复制到 AstrBot 的插件数据目录
    data/plugin_data/astrbot_plugin_group_guardian/extensions/wxcode.py
然后在插件配置中设置：
    wxcode_enabled = true
    wxcode_driver = python
    wxcode_python_module = wxcode.py
插件会对消息里的每张图片调用 recognize(image_bytes, image_url)，可以是普通函数或 async 函数。

方式二（http）：自己起一个本地服务，设置 wxcode_driver = http、
wxcode_service_url = http://127.0.0.1:8090/recognize。插件会 POST：
    {"kind": "wxcode", "image_url": "...", "image_base64": "...", "group_id": "..."}
服务返回下面任一 JSON 即可：
    ["识别内容1", "识别内容2"]
    {"texts": ["识别内容1"]}  或  {"texts": [{"text": "识别内容1"}]}

返回的每条内容会用 wxcode_regex_rules 中的正则逐条匹配（不区分大小写），命中则撤回原消息，
并把原消息和识别内容转发到 wxcode_forward_group_id（留空则用 backup_group_id）。
没有识别到葵花码时返回空列表即可。
"""


def recognize(image_bytes: bytes, image_url: str):
    """在这里调用你的葵花码识别模块，返回识别出的内容列表。"""
    # 示例：假设你的模块提供 decode(bytes) -> list[str]
    # from my_wxcode_lib import decode
    # return decode(image_bytes)
    return []
