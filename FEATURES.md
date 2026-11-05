# 功能地图（二次开发参考）

本插件功能、入口、开关与代码位置一览，用于判断哪些功能可以屏蔽、怎么屏蔽。
状态说明：**启用**＝默认开启；**默认关**＝有开关但默认关闭；**已停用**＝AI 总闸已强制关闭。

屏蔽一个功能有三种方式，按改动从小到大：

1. **改配置**：插件 WebUI「设置」或 AstrBot 配置面板里关掉对应开关（可在「群配置」按群覆盖）。不改代码，随时恢复。
2. **加入 AI 总闸那样的强制关闭**：把开关名加进 `constants.py` 的 `AI_FEATURE_KEYS`（或仿照它新建一个集合），`UtilitiesMixin._cfg` 会让它永远返回 False。适合“不想让任何人再打开”的功能。
3. **删除注册入口**：删掉 `main.py` 里对应的 `@filter.command` / 事件监听方法。指令或事件从此不再触发，业务代码可以留着也可以一起删。

---

## 零、最小化插件（当前默认配置）

对接后端：LuckyLilliaBot（LLBot，OneBot 11）。默认只开启下面这些功能；内置脏话 / 广告规则、外置词库、防刷屏、组合消息检测、Base 解码、QQ 收藏撤回都已改为默认关闭，群管指令全部保留。

> 默认值只对新安装生效。已经装过插件的，AstrBot 会保留已保存的配置，需要在设置里手动关闭这些项，或删除插件配置后重载。

### WebUI 入口

管理页面左侧导航的「常用功能」分组：

| 页面 | 内容 |
| --- | --- |
| 关键词与识别 | 自己的关键词规则（逐条添加，选择包含 / 完全相同 / 正则，保存前校验）、规则测试、备份群与命中处理方式、葵花码识别接入与正则、OCR 接入 |
| 批量公告 | 勾选群发布群公告 / 待办公告，查看待办确认情况 |
| 群名任务 | 各群群名一览，勾选群改后缀（立即 / 定时）、设置 / 停止 / 刷新倒计时 |
| 踢人与黑名单 | 跨群踢人 / 踢出并拉黑（拉黑必须填原因）、自动拉黑开关、插件用户黑名单（显示拉黑原因、来源、时间） |
| 重复成员 | 同一 QQ 号在 2 个及以上管理的群里：列出所在的群与入群时间，可「只保留一个群」（默认最早加入的群，可选被踢群 QQ 拉黑）、「全部踢出」（可选加入插件黑名单）、「忽略」；处理原因必填 |
| 操作记录 | 插件执行的所有写操作，可按操作类型、来源、QQ 号 / 群号 / 原因筛选 |
| 群成员缓存 | 各群缓存状态、手动刷新、缓存有效期与接口间隔设置、查找某个 QQ 号在哪些群 |

插件自带的规则库在「更多（内置功能，默认关闭）→ 内置规则库」，与自己的规则互不影响。新功能的配置项不在「设置 → 全量配置」中显示（列表在那里按逗号拆分保存，会拆坏正则里的逗号）。接口见 `web_minimal.py`。

### 快速配置

1. 同意免责声明（`disclaimer_agreed`）。
2. 填写备份群 `backup_group_id`，机器人需在该群内。
3. 在 `keyword_rules` 里写关键词，每行一条：`加微信`（包含即命中）、`=1`（整条消息就是“1”才命中）、`re:vx\s*\d{5,}`（正则）。
4. 葵花码：`wxcode_enabled` 打开，按 [examples/wxcode_recognizer_example.py](examples/wxcode_recognizer_example.py) 接入识别模块，在 `wxcode_regex_rules` 写正则，可选填写 `wxcode_forward_group_id`。
5. OCR（以后再开）：`local_ocr_driver` 选 `llbot`（LLBot 自带的 `ocr_image`），或选 `http` / `python` 接自己的服务。识别出的文字按 `keyword_rules` 过滤。

### 命中后的处理顺序

先把原消息以合并转发的形式备份到指定群（引用原消息 ID，图片完整保留；失败时依次回退为自定义节点、普通消息），再撤回。可选：`hit_mute_enabled` 同时禁言（默认关）、`hit_notice_enabled` 群内提示（默认关）。备份群 / 转发群本身不参与审核。

### 新增指令（均仅插件管理员）

| 指令 | 作用 |
| --- | --- |
| `/全群踢人` `/全群踢黑` `/批量踢黑` | 跨群踢人 / 踢出并 QQ 拉黑（`group_blacklist.py`） |
| `/全群公告 [群号,群号] [-置顶] [-确认] [-新人] [-弹窗] 内容` | 批量发群公告；消息里带图片时第一张作为配图 |
| `/全群待办 [群号,群号] 内容` | 批量发“待办公告”：置顶 + 需群成员确认 + 弹窗（LLBot 没有群待办接口，用公告替代） |
| `/确认发布` `/取消发布` | 上面两条只先回预览，确认后才真正发出（5 分钟内有效） |
| `/待办确认情况` | 最近一次待办公告在各群的已确认 / 未确认人数 |
| `/群名后缀 <后缀> [群号,群号]` | 批量改群名后缀，群名 = 基础名 + 后缀，不会叠加 |
| `/群名后缀 清除 [群号,群号]` | 去掉后缀 |
| `/群名后缀 <后缀> 定时 YYYY-MM-DD HH:MM [群号]` | 到点再改（一次性） |
| `/群名倒计时 YYYY-MM-DD ｜距考试{days}天 [群号,群号]` | 群名倒计时，每天 `countdown_update_time`（默认 00:05）自动更新；到期当天显示“今天”，过期后去掉后缀 |
| `/群名倒计时 列表` / `停止 [群号]` / `立即更新` | 管理倒计时 |

不写群号时作用于机器人所在、插件允许管理的所有群。群之间间隔 `broadcast_interval_seconds`（默认 2 秒）。群名长度上限 `group_name_max_length`（默认 30，超出时缩短基础名）。每次改群名 QQ 都会在群里显示系统提示，插件无法屏蔽。

### 群列表与群成员从哪来（本地缓存）

群列表来自协议端 `get_group_list`，群成员来自 `get_group_member_list`。两者都缓存在本地 SQLite（`group_directory.py`，表 `dir_groups` / `dir_members`），重启不丢：

- **读**：缓存未过期直接用，过期才调用接口。群列表默认 1 小时、成员默认 6 小时（`directory_group_ttl` / `directory_member_ttl`）。接口失败时退回旧缓存。
- **限流**：所有列表类调用（群列表、成员列表、改名前读取群名）排队执行，相邻两次至少间隔 `directory_min_interval_ms`（默认 1500 毫秒）；同一个群同时只刷新一次。
- **实时更新**：入群、退群、被踢、管理员任免、改名片的通知直接更新缓存；机器人踢人成功、改群名成功后也会同步。
- **后台补齐**：每 5 分钟最多刷新 2 个过期的群（`directory_background_refresh_enabled`）。
- **使用方**：跨群踢人（机器人自己的身份也从成员缓存读取，不再单独查询）、批量公告和群名任务的目标群、管理页面的群列表、判断发言人是否管理员。

插件原有的「更多 → 群管理」页面和名片监控仍有各自的短时缓存，没有接入这套缓存。

### 二维码 / 葵花码两阶段识别（`code_pipeline.py`）

接入方式 `wxcode_driver = two_stage`（默认）：

1. 下载图片，送**定位服务**：`POST {code_detect_url}/v1/detect`，字段 `file`，请求头 `X-API-Key`（`code_detect_api_key`）。超出 10 MiB / 1200 万像素 / 最长边 12000 时先缩小，坐标换算回原图。
2. 取类别为葵花码（class 2，`code_decode_kuihua`）和普通二维码（class 3，`code_decode_qrcode`）的框，每张图最多 `code_max_per_image` 个，按 `code_box_expand_ratio`（默认 20%，至少 16 像素）向四周扩大，先做 EXIF 方向纠正再裁剪成 PNG。
3. 裁剪图送**解码服务**：`POST {code_decode_url}/api/decode`，字段 `image`。`422 NO_CODE` 视为「解不出」。
4. 解出的 `text`（无法转文本时用 `payload_base64`）按「码内容正则规则」`wxcode_regex_rules` 匹配，命中则撤回并转发；定位到但解不出时按 `code_undecoded_action`（默认放行，可选撤回）。

**命中即停**：一张图里的多个码按置信度从高到低逐个解码，每解出一个就匹配规则，命中后不再解码剩下的码，也不再识别这条消息后面的图，立即撤回（解码服务慢，可以减少延迟）。缓存里「命中即停」的结果没有解完所有码，规则改动后不再命中时会重新完整识别。

两个服务的接口文档：[service_api/定位服务API.md](service_api/定位服务API.md)、[service_api/解码服务API.md](service_api/解码服务API.md)（各含「插件如何调用」一节）。

两个服务地址只写 `http://IP:端口` 即可。两个服务都是串行处理，插件对每个服务同时只发一个请求；定位 `503` 按 `Retry-After` 最多重试 3 次，解码 `503 BUSY` 最多重试 2 次。同一图片内容 10 分钟内不重复识别。服务不可用时放行并写日志。没有 Pillow 时不裁剪，整图交给解码服务。管理页面「关键词与识别」里可以测试连接、上传测试图片查看每一步的结果（不会撤回）。API Key 只存在插件配置里，管理页面不回显，操作记录里只记「已设置」。

### 重复成员检测

同一个 QQ 号同时在 2 个及以上「管理的群」里即为重复成员。只读本地成员缓存，不额外调用接口；页面会显示多少个群已缓存成员，没缓存的群不参与统计，可一键「刷新所有群的成员缓存」（后台逐个限速）。备份群 / 转发群、机器人自己不参与统计；插件管理员、用户白名单、在任一群是群主 / 管理员的人默认不列出，且插件管理员和白名单用户永远不会被处理。忽略名单存在 SQLite meta 表（键 `duplicate_member_ignore`）。

### 操作记录与拉黑原因

`operation_log.py`（表 `op_logs` / `blacklist_meta`）。每条记录：时间、来源（指令 / WebUI / 自动）、操作人（指令为发送者 QQ；WebUI 不带面板用户，留空）、操作、群、对象、原因、结果、详情。保留最近 10 万条。

记录的操作：踢人、踢出并拉黑、加入 / 移出黑名单、加入 / 移出审核白名单、关键词撤回、葵花码撤回、退群自动拉黑、黑名单入群踢出、黑名单发言踢出、通过 / 拒绝加群申请、发布群公告 / 待办公告、修改群名后缀（旧名 → 新名）、定时改群名的登记 / 取消 / 执行、群名倒计时的设置 / 更新 / 停止 / 结束、修改关键词 / 葵花码规则、修改配置（旧值 → 新值）、刷新群成员缓存。

拉黑原因：指令里写在「原因：」或「#」后面，例如 `/全群踢黑 111 222 原因：发广告`（原因里的数字不会被当成 QQ 号）；WebUI 拉黑时必填；自动拉黑由插件写明原因（如「主动退出群 xxx（退群自动拉黑）」）。早于本功能加入黑名单的人原因为空。

### 新增模块

| 文件 | 内容 |
| --- | --- |
| `keyword_guard.py` | 关键词规则解析与匹配、命中处理（备份 → 撤回 → 可选禁言 / 提示 → 日志），审核流水线入口 `_run_hit_guards` |
| `message_backup.py` | 备份 / 转发到指定群 |
| `image_recognizer.py` | 葵花码 / OCR 识别器接口（http / python / llbot 驱动） |
| `group_broadcast.py` | 批量公告、待办公告、确认情况 |
| `group_name_task.py` | 群名后缀、定时改名、倒计时（任务存在 SQLite meta 表） |
| `group_directory.py` | 群 / 群成员本地缓存、限流、通知事件实时更新 |
| `operation_log.py` | 操作记录、黑名单原因 |
| `duplicate_members.py` | 重复群成员检测（只读本地成员缓存）与处理、忽略名单 |
| `code_pipeline.py` | 二维码 / 葵花码两阶段识别：定位服务 → 扩大框裁剪 → 解码服务 |
| `web_minimal.py` | 以上新功能的 WebUI 接口（`/minimal/*`） |

---

## 一、整体结构

```
main.py               入口：继承所有 Mixin，向 AstrBot 注册指令 / 事件监听 / WebUI 路由
├─ moderation.py      消息审核主流程（_handle_message）+ 规则处罚
│  ├─ moderation_context.py   审核上下文、组合消息检测的消息缓存
│  ├─ image_audit.py          图片：二维码解码（本地）、OCR（AI，已停用）
│  ├─ encoded_content.py / text_obfuscation.py   Base 编码解码、混淆网址还原
│  ├─ automaton.py            AC 自动机 + 正则回退匹配器
│  ├─ link_whitelist.py       链接白名单
│  └─ ai_relay.py             「AI 中转」特殊数字检测（正则，不调用 AI）
├─ anti_flood.py      防刷屏（频率 / 重复 / 长文本 / 夜间限速）
├─ membership.py      加群申请审核
├─ group_blacklist.py 跨群踢人 / 踢黑 / 退群自动拉黑（二次开发新增）
├─ card_monitor.py    名片监控、名片保护、管理员任免记录
├─ appeal.py          刷屏申诉（AI 裁决，已停用）
├─ lexicon_learn.py   词库自学习（AI，已停用）
├─ moderation_review*.py  AI 误判复盘（已停用）
├─ llm_stream.py      大模型流式调用工具（只被 AI 代码使用）
├─ commands.py        群管指令的业务实现
├─ remote.py          WebUI 远程执行群管操作（批量禁言 / 踢人 / 改名片…）
├─ scheduler.py       后台任务：定时解禁、申诉超时、名片同步（+ 学习 / 复盘，已停用）
├─ onebot.py          OneBot 接口封装、权限判定（_is_admin）、踢人 / 禁言底层
├─ web.py + pages/dashboard/index.html   WebUI 管理面板（API + 前端）
├─ storage.py / storage_group.py / lexicon_migration.py   SQLite 存储、按群配置、词库迁移
└─ utils.py / constants.py / single_flight.py   配置读取（_cfg）、AI 总闸、并发合并
```

所有功能共用的前置门槛：`enabled`（插件总开关）、`disclaimer_agreed`（同意免责声明），以及群白名单 / 群黑名单（`group_white_list` / `group_black_list`，在 WebUI「名单管理」维护）。

---

## 二、消息审核流水线（每条群消息）

入口 `main.py:_handle_message` → `moderation.py:_handle_message`。按顺序执行，前一步命中就结束：

| # | 步骤 | 开关 | 默认 | 命中后 |
| --- | --- | --- | --- | --- |
| 1 | 预检：用户白名单、群黑白名单、插件总开关 | `user_white_list` 等 | — | 白名单用户 / 非管理群直接跳过 |
| 2 | QQ 开放平台机器人豁免 | `official_bot_exempt_enabled` | 默认关 | 跳过 |
| 3 | 机器人不是管理员时跳过 | `moderation_require_bot_admin` | 启用 | 跳过（无法撤回 / 禁言） |
| 4 | **防刷屏**（见第四节） | `anti_flood_enabled` | 默认关 | 禁言 / 撤回 |
| 5 | 管理员内容豁免 | `moderation_admin_exempt` | 默认关 | 跳过 |
| 6 | **用户黑名单**：发言即踢出并 QQ 拉黑 | `user_black_list`、`blacklist_native_block_enabled` | 启用 | 踢出 + 禁言 |
| 7 | 自动审核总开关（可按群） | `auto_moderate_enabled` | 启用 | 关闭则后面全部不做 |
| 8 | 解析消息链、合并转发内容 | `scan_forward_msg` | 启用 | — |
| 8.5 | **关键词撤回 → 葵花码识别 → OCR 文字**（`keyword_guard.py`，见第零节） | `keyword_guard_enabled` / `wxcode_enabled` / `local_ocr_driver` | 启用 / 默认关 / 关 | 备份到指定群 + 撤回 |
| 9 | QQ 收藏转发撤回 | `recall_qq_favorite_enabled` | 默认关 | 撤回 |
| 10 | AI 中转特殊数字检测（正则） | `ai_relay_mode_enabled` + 三步风险确认 | 默认关 | 只撤回 |
| 11 | 图片二维码解码（本地 OpenCV / pyzbar） | `qrcode_decode_enabled` | 默认关 | 解出的文本参与后续规则 |
| 12 | 混淆网址还原（随广告检测）/ Base 编码解码 | `scan_ad` / `base_decode_enabled` | 默认关 | 混淆网址直接处罚 |
| 13 | **内置规则初筛**：脏话、广告、自定义词、外置词库 8 类、链接白名单（词库中的脏话 / 广告分类跟随 `scan_swear` / `scan_ad`） | `scan_swear`、`scan_ad`、`custom_*_keywords`、`lexicon_*_enabled`、`link_whitelist` | 默认关 | 进入处罚 |
| 14 | 组合消息检测（防把违禁词拆成多条发） | `combine_detect_enabled` | 默认关 | 合并命中则一起撤回 |
| 15 | **规则处罚**：撤回 + 禁言 + 群内提示 + 登记定时解禁 | `moderation_ban_duration`(1800 秒)、`auto_moderate_notice`、`ban_notice` | 启用 | — |

> **注意**：第 12–15 步现在默认关闭。如果手动打开，因为 AI 已停用，命中内置规则会**直接撤回并禁言 30 分钟**，不再经过 AI 复核。外置词库约 6.7 万词，误伤较多，建议只开需要的分类，并先在一个群观察审核日志。

---

## 三、群管指令

所有指令都需要“群操作权限”（插件管理员、群超管，或群主 / 群管理员，见第八节）。注册位置均在 `main.py`，业务在 `commands.py`（标注了的除外）。

| 指令 | 作用 | 功能开关 |
| --- | --- | --- |
| `/禁言` `/解禁` | 禁言 / 解除禁言某人 | `ban_enabled` / `unban_enabled` |
| `/踢人` | 踢出某人 | `kick_enabled` |
| `/批量禁言` `/批量踢人` | 当前群批量操作 | `ban_enabled` / `kick_enabled` |
| `/批量踢黑` | 当前群批量踢出并 QQ 拉黑（`group_blacklist.py`） | `kick_enabled` |
| `/全群踢人` `/全群踢黑` | 所有管理的群踢人 / 踢黑，仅插件管理员（`group_blacklist.py`） | `kick_enabled` |
| `/全体禁言` | 开关全员禁言 | `whole_ban_enabled` |
| `/撤回最新消息` `/批量撤回` `/回复撤回` | 撤回消息 | `recall_enabled` |
| `/设置名片` `/头衔` | 改群名片 / 专属头衔 | `set_card_enabled` / `set_title_enabled` |
| `/设置管理` `/取消管理` | 任免群管理员（需机器人是群主） | `set_admin_enabled` |
| `/群名` | 改群名 | `set_group_name_enabled` |
| `/加群方式` | 设置加群验证方式 | `join_verify_enabled` |
| `/发公告` `/删公告` `/公告列表` | 群公告 | `send_` / `delete_` / `list_announcements_enabled` |
| `/文件列表` `/删文件` | 群文件 | `group_files_enabled` |
| `/设精华` `/取消精华` | 精华消息 | `essence_enabled` |
| `/成员列表` `/搜索成员` `/禁言列表` | 查询 | `member_list_enabled` / `banned_list_enabled` |
| `/群统计` `/字数统计` | 统计（字数统计默认关） | `group_stats_enabled` / `word_count_enabled` |
| `/自动审核` | 开关本群自动审核 | `auto_moderate_enabled` |
| `/添加违禁词` `/删除违禁词` `/查看违禁词` | 维护自定义违禁词 | — |
| `/设置管理插件` | 管理插件管理员名单（仅插件管理员） | — |
| `/群管理授权` `/移除群管权限` `/恢复群管权限` | 权限管理（见第八节） | `group_admin_grant_enabled` |

> 空开关：`group_honor_enabled`、`at_all_remain_enabled`、`ignore_requests_enabled`、`group_msg_history_enabled`、`group_portrait_enabled`、`group_sign_enabled` 在配置里存在，但代码中没有任何功能使用它们（原版就如此），可以无视或从 `_conf_schema.json` 删除。

---

## 四、防刷屏（`anti_flood.py`）

总开关 `anti_flood_enabled`（**默认关**，下面的子功能默认值只在打开总开关后生效；重复消息、长文本、刷屏撤回也已默认关），在审核流水线第 4 步执行，管理员始终豁免。

| 子功能 | 开关 / 参数 | 默认 |
| --- | --- | --- |
| 频率限制（滑动窗口：1 秒 / 1 分钟 / 1 小时） | `anti_flood_rate_per_second/minute/hour` | 5 / 20 / 60 条 |
| 夜间独立限速 | `anti_flood_night_enabled` + 时段与阈值 | 默认关 |
| 重复消息检测 | `repeat_detect_enabled`、窗口 / 次数 | 启用，120 秒内 3 次 |
| 长文本检测 | `long_text_detect_enabled`、`long_text_threshold` | 启用，500 字 |
| 处罚：禁言 | `anti_flood_mute_duration` | 300 秒 |
| 处罚：批量撤回 | `anti_flood_recall_enabled`、`anti_flood_recall_threshold` | 启用，20 条 |
| 刷屏申诉 | `appeal_enabled` | **已停用**（AI 裁决） |

---

## 五、入群与黑名单

| 功能 | 位置 | 开关 | 默认 |
| --- | --- | --- | --- |
| 加群申请自动审核：通过词 / 拒绝词（`=` 开头为精确匹配）/ 词库过滤 / 默认动作 | `membership.py`，规则在 WebUI「入群审核」页按群配置 | `join_audit_enabled`、`join_reject_use_lexicon`、`join_accept_overrides_lexicon`、`join_default_action` | 默认关 |
| 审核结果群内通知 | 同上 | `join_audit_notify` | 启用 |
| 入群 AI 语义审核 / 搜索增强 | 同上 | `join_llm_*` | **已停用** |
| 用户黑名单：申请自动拒绝（无需开入群审核） | `membership.py` | `blacklist_reject_join_enabled` | 启用 |
| 黑名单用户入群即踢 | `group_blacklist.py` | `blacklist_kick_on_join` | 启用 |
| 踢黑名单时 QQ 原生拉黑 | `onebot.py:_kick_member` | `blacklist_native_block_enabled` | 启用 |
| 退群自动拉黑（全群通用） | `group_blacklist.py` | `leave_auto_blacklist_enabled` | 默认关 |

---

## 六、名片监控（`card_monitor.py`，总开关默认关）

总开关 `card_monitor_enabled`。依赖协议端上报 `group_card` / `group_increase` / `group_admin` 通知。

| 子功能 | 开关 | 默认 |
| --- | --- | --- |
| 名片变更 / 管理员任免记日志 | `card_log_enabled` | 启用 |
| 周期同步兜底（协议端不上报时轮询） | `card_sync_enabled`、`card_sync_interval` | 启用，120 秒 |
| 违规名片审核（规则初筛，命中即还原） | `card_audit_enabled`、`card_audit_link_only`、`card_audit_admin_exempt` | 默认关 |
| 名片保护名单（被改就还原） | `card_protect_enabled` | 默认关 |
| 名片还原群内通知 | `card_monitor_notify` | 启用 |
| 管理员任免群内通知 | `admin_change_notify_enabled` | 默认关 |
| 名片 AI 判定 | `card_audit_llm_always` | **已停用**（只按规则判定） |

---

## 七、后台任务（`scheduler.py`）

| 任务 | 开关 | 默认 |
| --- | --- | --- |
| 定时自动解禁（插件发起的禁言到期解除，失败退避重试） | `auto_unban_enabled`、`auto_unban_scan_interval`、`auto_unban_permanent_hours` | 默认关 |
| 申诉超时清理 | 随申诉 | 已停用 |
| 名片周期同步 | `card_sync_enabled` | 随名片监控 |
| 词库自学习 / AI 误判复盘 | — | **已停用**（AI 总闸关闭时不启动） |

---

## 八、权限体系（`onebot.py:_is_admin`）

判定顺序：① 群级 bot 权限黑名单（`/移除群管权限`，最高优先）→ ② 插件管理员（`admin_list` + AstrBot 全局管理员）→ ③ 群超管（WebUI「权限」页按群设置）→ ④ 群主 / 群管理员（受 `group_admin_grant_enabled` 动态授权、`legacy_role_admin_enabled` 与 `strict_permission_enabled` 控制）。

**严格权限 `strict_permission_enabled`（默认开）**：第 ④ 步只放行群主，QQ 群管理员不再自动获得指令权限，需要在 WebUI 设为插件管理员 / 群超管，或用 F5 动态授权为该群显式授予。关闭后恢复为群主和群管理员都自动拥有本群权限。

另外两个严格模式：`member_action_require_group_role`（群管指令只允许本群群主 / 管理员）、`set_admin_require_owner`（任免管理员只允许群主）。默认都关。

---

## 九、WebUI 管理面板（`web.py` + `pages/dashboard/index.html`）

| 页面 | 内容 | 状态 |
| --- | --- | --- |
| 总览 | 拦截统计、趋势、分布、排行 | 启用 |
| 群管理 | 群列表、成员列表、远程禁言 / 踢人 / 改名片等（`remote.py`） | 启用 |
| 违规记录 | 被撤回用户、审核日志 | 启用；其中「AI 误判复盘」卡片已隐藏 |
| 防刷屏 | 实时追踪状态 | 启用 |
| 规则中心 | 脏话 / 广告规则、外置词库维护（热更新） | 启用 |
| 入群审核 | 按群配置通过 / 拒绝词 | 启用 |
| 权限 | 群超管、动态授权、群权限黑名单 | 启用 |
| 群配置 | 按群覆盖任意配置项 | 启用（AI 配置项已不显示） |
| 申诉 / 解禁 | 申诉记录、定时解禁计划 | 申诉记录已隐藏 |
| 名片监控 | 开关、保护名单、变更记录 | 启用 |
| 词库学习 | 自学习候选词审批 | **已隐藏** |
| 设置 | 全部开关、名单管理、SQLite 迁移助手 | OCR 分区与 AI 配置项已隐藏 |

---

## 十、AI 功能（已停用）

由 `constants.py` 的 `AI_FEATURES_ENABLED = False` 统一关闭：

- **强制关闭的开关**（`AI_FEATURE_KEYS`）：配置里打开也无效。
- **界面隐藏**（`AI_CONFIG_KEYS`）：AstrBot 配置面板（schema 中标记为 `invisible`，说明前加了“[AI 已停用]”）、插件 WebUI 和多群配置都不显示。已保存的值保留不动。
- **接口停用**：`/providers`、`/moderation_review/run`、`/moderation_review/suggestions/apply`、`/lexicon_learn/run`、`/lexicon_learn/config/set` 直接返回“AI 功能已停用”。
- **已删除**：24 个大模型群管工具（`llm_tools.py`）和提示词注入（`on_llm_request`）。

| AI 功能 | 原开关 | 代码（仍在，但不会执行） |
| --- | --- | --- |
| 消息 AI 二审 / 全量 AI 审核 | `llm_moderation_enabled`、`llm_moderation_always` | `moderation.py` 中 `_call_llm*`、`_execute_llm_penalty` |
| OCR 识图 / 表情包识别 | `ocr_enabled`、`scan_sticker_enabled` | `image_audit.py`（二维码解码部分仍在用） |
| 入群 AI 审核 | `join_llm_moderation_enabled`、`join_llm_web_search_enabled` | `membership.py` 中 `_call_llm_for_join_request` |
| 名片 AI 判定 | `card_audit_llm_always` | `card_monitor.py:_card_llm_violation`（已回退为规则判定） |
| 刷屏申诉 | `appeal_enabled` | `appeal.py` |
| 词库自学习 | `lexicon_learn_*` | `lexicon_learn.py` |
| AI 误判复盘 | `moderation_review_*` | `moderation_review.py`、`moderation_review_storage.py` |

恢复 AI：把 `AI_FEATURES_ENABLED` 改为 True，并从 git 历史恢复 `llm_tools.py` 与 `main.py` 中的工具注册。

如果以后要把这些代码彻底删掉，耦合点主要在：`ModerationMixin` 继承 `ImageAuditMixin`（二维码解码在里面，不能整个删）、`web.py` 中复盘 / 学习 / 申诉相关的路由与处理函数、`storage.py` 中的申诉 / 复盘 / 学习表、`scheduler.py` 中对应的 loop、前端对应页面，以及 `tests/` 里的相关测试。
