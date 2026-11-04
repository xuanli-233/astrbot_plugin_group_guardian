# 本地模拟环境

不需要 AstrBot、QQ 或 Python：在 Node 里用 Pyodide 运行**真实的插件代码**，只把 AstrBot 和 QQ 协议端换成模拟的。

```bash
cd devtools/mock_webui
npm install
node server.mjs
```

然后打开 <http://localhost:8765/>：

- **管理页面**：插件自带的 WebUI，所有接口都由真实的 `web.py` 处理。
- **模拟群聊**：模拟群成员发消息、管理员发指令、成员退群 / 入群、推进时间触发群名倒计时，并显示机器人调用的 QQ 接口和插件日志。

内置 4 个模拟群：一群、二群（机器人是管理员）、三群（机器人只是普通成员，用来观察失败处理）、违规消息备份群。
插件管理员是 `10000`（管理员小李），只有用他发消息才能使用管理指令。

预置配置：备份群 `900000001`，关键词 `加微信`、`代刷`、`=1`、`re:v[x信]...`，葵花码正则 `invite`、`pages/.*share`。
葵花码和 OCR 用模拟识别模块：在“模拟群聊”里填写“葵花码识别内容”或“图中文字”，就相当于发了一张能识别出这些内容的图片。

数据保存在内存里，重启服务即恢复初始状态。文件说明：

| 文件 | 作用 |
| --- | --- |
| `server.mjs` | HTTP 服务，加载 Pyodide 与插件 |
| `host.py` | AstrBot / Quart 替身、模拟 QQ 协议端（FakeOneBot）、模拟群聊逻辑 |
| `bridge-sdk.js` | 替代 AstrBot 的插件页面 Bridge SDK |
| `index.html` / `sim.html` | 入口页 / 模拟群聊页 |

限制：没有真实网络请求（图片下载、识别服务都是模拟的）；后台定时任务不会自动运行，要在“时间推进”里手动触发；Pyodide 没有 pyahocorasick，词库匹配用较慢的纯 Python 实现。
