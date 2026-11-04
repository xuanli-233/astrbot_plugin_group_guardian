# 部署指南（AstrBot + LuckyLilliaBot）

本文介绍从零部署本插件的完整步骤。整体结构：

```
QQ ── LuckyLilliaBot（协议端）── 反向 WebSocket（OneBot 11）──▶ AstrBot ── 本插件
                                                                         │
                                         （可选）二维码 / 葵花码识别 ◀──┘
                                         定位服务 :8100 + 解码服务 :8000
```

| 组件 | 默认地址 | 说明 |
| --- | --- | --- |
| AstrBot 管理面板 | `http://<AstrBot 所在机器>:6185` | 本插件的管理页面也在这里 |
| AstrBot 的 OneBot 接入 | `ws://<AstrBot 所在机器>:6199/ws` | LuckyLilliaBot 主动连这个地址 |
| 定位服务 / 解码服务 | `http://127.0.0.1:8100` / `http://127.0.0.1:8000` | 只在使用二维码 / 葵花码识别时需要，地址可在插件里修改 |

> 下文中 AstrBot、LuckyLilliaBot 的界面名称以各自的官方文档为准，版本不同时菜单位置可能略有差异。

***

## 1. 准备

- **AstrBot** ≥ 4.24.2（本插件要求的最低版本）。
- **LuckyLilliaBot（LLBot）**，已登录作为机器人的 QQ 号。
- 机器人 QQ 在要管理的群里需要是**管理员**（撤回、踢人、改群名、发公告都需要）。机器人只是普通成员的群，插件会跳过并在结果里说明。
- 用于存放违规消息备份的**备份群**，机器人需要在群里（建议是管理员）。
- （可选）二维码 / 葵花码识别的两个服务，见 [service_api/](service_api/)。

## 2. 部署 AstrBot

按 [AstrBot 官方文档](https://docs.astrbot.app/) 部署即可。源码方式的参考步骤（LuckyLilliaBot 文档给出的方式，需要 Python 3.11+ 与 [uv](https://github.com/astral-sh/uv)）：

```bash
git clone https://github.com/AstrBotDevs/AstrBot.git
cd AstrBot
uv venv --python 3.11
uv pip install -r requirements.txt
uv run main.py
```

启动后浏览器打开 `http://localhost:6185` 进入管理面板（首次进入按提示设置账号密码）。**不要把 6185 端口直接暴露到公网**，插件的管理页面依赖 AstrBot 面板的登录鉴权。

LLBot Desktop 用户也可以在 LLBot 的「对接框架」页面一键安装 AstrBot。

## 3. 连接 LuckyLilliaBot

**AstrBot 侧**：在管理面板的「消息平台」（或「平台管理」/「机器人」）页面新建平台：

| 项 | 值 |
| --- | --- |
| 类型 | `aiocqhttp`（OneBot v11 反向 WebSocket） |
| 主机 | `0.0.0.0` |
| 端口 | `6199` |
| Token | 留空，或与 LLBot 填同一个值 |
| 启用 | 勾选 |

**LLBot 侧**：启用 OneBot 11，添加一个反向 WebSocket 连接（也可以在 LLBot WebUI 的 Bot 配置页面填写地址）：

```json
{
  "type": "ws-reverse",
  "enable": true,
  "url": "ws://127.0.0.1:6199/ws",
  "heartInterval": 60000,
  "token": "",
  "messageFormat": "array",
  "reportSelfMessage": false
}
```

注意：

- `messageFormat` 请用 **`array`**。插件备份消息、识别图片时读取的是数组格式的消息段。
- URL 里的端口要和 AstrBot 平台的端口一致（6199，不是面板的 6185），路径必须是 `/ws`。
- 两者不在同一台机器时，把 `127.0.0.1` 换成 AstrBot 所在机器的 IP，并在防火墙放行 6199。
- 连接成功后，在群里发一条 `/群统计` 之类的指令，机器人有回应即说明通路正常。

## 4. 安装本插件

### 方式一：命令行（推荐）

```bash
cd AstrBot/data/plugins
git clone https://github.com/xuanli-233/astrbot_plugin_group_guardian.git
```

然后在 **AstrBot 使用的 Python 环境**里安装依赖（AstrBot 通常会自动安装插件的 `requirements.txt`，没有自动安装时手动执行）：

```bash
# 源码 + uv 部署时，在 AstrBot 目录下执行
uv pip install -r data/plugins/astrbot_plugin_group_guardian/requirements.txt
```

依赖：`pyahocorasick`、`opencv-python-headless`、`numpy`、`Pillow`（AstrBot 本身已带 Pillow）。

最后重启 AstrBot，或在管理面板「插件管理」里找到本插件，点右上角「…」→「重载插件」。

### 方式二：管理面板

如果你的 AstrBot 版本的插件页面支持「从链接 / 仓库地址安装」，填入：

```
https://github.com/xuanli-233/astrbot_plugin_group_guardian
```

### 已经装过原版的情况

本插件的识别名与原版相同（`astrbot_plugin_group_guardian`），所以：

1. **先备份**：插件数据目录 `data/plugin_data/astrbot_plugin_group_guardian/`（数据库 `group_guardian.db`）以及 AstrBot 保存的插件配置（通常在 `data/config/` 下，文件名含 `astrbot_plugin_group_guardian`）。
2. 删除 `data/plugins/astrbot_plugin_group_guardian/` 这个目录，再按方式一克隆本仓库。数据目录和配置不受影响，黑名单、日志都会保留。
3. **原有配置不会被改成本版的默认值**。本版默认关闭了防刷屏、内置脏话 / 广告规则、外置词库等，升级过来的需要在「设置」里手动关闭不需要的功能。AI 功能无论配置如何都已强制关闭。
4. 以后**不要在插件市场里「更新」**，否则会被换回原版；更新方法见第 8 节。

## 5. 首次配置

打开 AstrBot 管理面板 →「插件管理」→ 本插件 → 管理页面：

1. **同意免责声明**：「设置」顶部。未同意时插件所有功能都不生效。
2. **插件管理员**：「设置」→「名单管理」→ 管理员。AstrBot 全局管理员会自动同步过来。跨群踢人、批量公告、群名任务等指令只有插件管理员能用。
3. **备份群与关键词**：「关键词与识别」→ 选择备份群 → 添加关键词规则（包含 / 完全相同 / 正则）→ 用测试框确认命中效果。
4. **群成员缓存**：「群成员缓存」→「全部刷新」。跨群踢人、重复成员检测都依赖它；之后有人进退群会自动更新。
5. **按需打开**：「踢人与黑名单」里的退群自动拉黑（默认关）；群白名单（「设置」→「名单管理」，留空表示机器人所在的所有群都管理）。

## 6. （可选）二维码 / 葵花码识别

1. 先启动两个服务，确认能访问：

   ```bash
   curl http://127.0.0.1:8100/health
   ```

   返回 `{"status":"ok","ready":true}` 即定位服务就绪。解码服务没有探活接口。

2. 「关键词与识别」→「二维码 / 葵花码识别」卡片：
   - 接入方式：**定位服务 + 解码服务（两阶段）**
   - 定位服务 / 解码服务：填 `http://IP:端口`（插件自动拼接路径）。服务和 AstrBot 不在同一台机器时填服务所在机器的 IP，并放行对应端口。
   - API Key：定位服务的 `X-API-Key`。只保存在插件配置里，页面不回显，不要写进代码或发到群里。
   - 解码哪些码、框扩大比例（默认 20%）、解不出时（默认放行）。
   - 点「保存」。
3. 点「**测试连接**」，确认定位服务正常、API Key 已设置。
4. 点「**上传测试图片**」，用几张真实的广告图检查：定位框、裁剪范围、解码内容、命中的规则、最终会不会撤回。测试图片不会触发撤回。
5. 添加「码内容正则规则」，打开卡片顶部的「启用葵花码识别」。

解码服务比较慢（单个码可能十几到几十秒），群里发图到撤回之间会有这段延迟；一张图多个码时命中即停。

## 7. 验证清单

在一个测试群（机器人是管理员）里逐项确认：

- [ ] 普通成员发一条含关键词的消息 → 被撤回，备份群收到合并转发。
- [ ] 「操作记录」里出现「关键词撤回」，原因是命中的规则。
- [ ] （启用识别时）发一张命中规则的葵花码 / 二维码图片 → 被撤回并转发，转发里有识别结果。
- [ ] 插件管理员发 `/全群公告 测试` → 机器人回预览 → `/确认发布` → 各群出现公告。
- [ ] 「踢人与黑名单」用一个测试小号执行「踢出并拉黑」，确认 LuckyLilliaBot 确实把它加进了 QQ 群黑名单（再次申请加群会被拒绝）。

## 8. 更新

```bash
cd AstrBot/data/plugins/astrbot_plugin_group_guardian
git pull
uv pip install -r requirements.txt   # 依赖有变化时
```

然后在「插件管理」里重载本插件或重启 AstrBot。不要用插件市场的「更新」。

## 9. 备份

需要备份的只有两处：

| 内容 | 位置 |
| --- | --- |
| 数据库：审核日志、操作记录、黑名单原因、群成员缓存、群名任务等 | `data/plugin_data/astrbot_plugin_group_guardian/group_guardian.db` |
| 插件配置：所有开关、规则、备份群、识别服务地址与 API Key | AstrBot 的插件配置文件（通常在 `data/config/` 下） |

配置文件里有 API Key，备份时注意保管。

## 10. 常见问题

| 现象 | 处理 |
| --- | --- |
| AstrBot 收不到消息 | 检查 AstrBot 平台是否启用、端口是否为 6199、LLBot 的 URL 是否以 `/ws` 结尾、两边 Token 是否一致、防火墙 |
| 插件加载失败 | 看 AstrBot 日志里的报错；通常是依赖没装到 AstrBot 的 Python 环境，按第 4 节重新安装 |
| 管理页面打不开 / 显示「API 不可用」 | 确认 AstrBot ≥ 4.24.2，并从 AstrBot 管理面板的插件页面进入 |
| 指令提示无权限 | 发送者需为插件管理员 / 群主 / 群管理员；跨群指令只允许插件管理员 |
| 撤回、踢人失败 | 机器人在该群不是管理员；目标是群主或管理员时机器人需要是群主 |
| 没有备份到备份群 | 备份群是否已选择、机器人是否在群里；备份群本身不参与审核 |
| 识别不工作 | 「测试连接」；API Key；「上传测试图片」看哪一步失败；服务不可用时插件会放行并在日志里记录 |
| 「解码服务连接失败」但地址没错 | AstrBot 所在机器能否访问该地址（跨机器时不要用 `127.0.0.1`） |
| 裁剪不生效、提示整图解码 | AstrBot 的 Python 环境缺少 Pillow：`uv pip install Pillow` |
| 群待办 | LuckyLilliaBot 没有群待办接口，插件用置顶 + 需确认的群公告替代 |

更多功能细节见 [FEATURES.md](FEATURES.md)，插件本身的说明见 [README.md](README.md)。

参考：[LuckyLilliaBot 对接 AstrBot](https://luckylillia.com/guide/install_astrbot)、[AstrBot 文档](https://docs.astrbot.app/)、[AstrBot 插件依赖说明](https://docs.astrbot.app/dev/star/plugin-new.html)。
