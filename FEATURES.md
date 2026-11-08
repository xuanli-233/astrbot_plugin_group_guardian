# 功能地图（二次开发参考）

本插件功能、入口、开关与代码位置一览，用于判断哪些功能可以屏蔽、怎么屏蔽。
状态说明：**启用**＝默认开启；**默认关**＝有开关但默认关闭。

屏蔽一个功能有两种方式：

1. **改配置**：插件 WebUI「设置」或 AstrBot 配置面板里关掉对应开关（可在「多群配置」按群覆盖）。不改代码，随时恢复。
2. **删除注册入口**：删掉 `main.py` 里对应的 `@filter.command` / 事件监听方法。指令或事件从此不再触发。

原版插件的 AI 审核、防刷屏、内置规则库 / 外置词库、名片监控、刷屏申诉、词库自学习、远程群管等功能已从代码中删除，见第十节。

---

## 零、最小化插件（当前默认配置）

对接后端：LuckyLilliaBot（LLBot，OneBot 11）。默认只开启下面这些功能；原版群管指令全部保留但默认关闭。

> 默认值只对新安装生效。已经装过插件的，AstrBot 会保留已保存的配置，需要在设置里手动关闭这些项，或删除插件配置后重载。

### WebUI 入口

管理页面左侧导航按用途分为「消息审核」「成员管理」「群运营」「系统」四组。主要页面：

| 页面 | 内容 |
| --- | --- |
| 关键词与识别 | **撤回 / 禁言规则组**（每组：关键词、码内容正则、小程序（整个小程序，或 `小程序 | 标题条件` / `* | 标题条件` 按标题），命中后撤回 + 可选禁言 N 分钟 + 可选群内提示，可只对部分群生效；从上往下检查，默认组最后）、规则组测试、备份群、葵花码识别接入、OCR 接入 |
| 批量公告 | 勾选群发布群公告 / 待办公告，查看待办确认情况 |
| 群名任务 | 各群群名一览，勾选群改后缀（立即 / 定时）、设置 / 停止 / 刷新倒计时；「刷新群列表」从 QQ 重新拉取所有群的最新群名；每行「改名」直接修改当前群名 |
| 踢人与黑名单 | 跨群踢人 / 踢出并拉黑（拉黑必须填原因）、自动拉黑开关、插件用户黑名单（显示拉黑原因、来源、时间） |
| 重复成员 | 同一 QQ 号在 2 个及以上勾选的群里：显示头像、所在的群与入群时间。默认逐个「处理」：先选「只保留一个群」（默认最早加入的群，可选被踢群 QQ 拉黑）或「全部踢出」（可选加入插件黑名单）并填原因，再确认一次要踢的群；也可打开「批量处理」一次处理多人（同样二次确认）；「忽略」 |
| 退群记录 | 每次退群 / 被踢：时间、群、方式、退群前的消息（含图片缩略图，点开看大图）；点 QQ 号看此人一共退过几次、退过哪些群 |
| 码管理 | 码统计（按码 / 按码 + 人）、**按人统计**（长期累计：每个人发过哪些码、各几次、在哪些群，不随码记录清理）、白名单 / 黑名单、发送次数限制 |
| 小程序 | 群里出现的小程序（卡片 + `#小程序://` 链接）：按小程序 / 小程序 + 人统计次数、内容、群，详情里看每个内容的次数和预览图、最近记录；加白名单 / 拉黑（拉黑 = 加进默认规则组的小程序列表）；详情里「按这个标题拉黑」只拦这个小程序里标题含某些字的内容（条目 `小程序 | 标题条件`，适合「微店买买」这类平台小程序），标「部分拉黑」；小程序看板（最近出现，每 3 秒更新）。码大屏底部也有「出现最多的小程序」 |
| 码大屏 | 深色全屏监控页：时段内发码次数 / 撤回次数 / 不同的码 / 发码人数、全部历史累计、每小时走势、发得最多的码 / 人 / 群、实时识别流（每 3 秒）；「全屏」隐藏侧边栏 |
| 操作记录 | 插件执行的所有写操作，可按操作类型、来源、QQ 号 / 群号 / 原因筛选 |
| 群成员缓存 | 各群缓存状态、手动刷新、缓存有效期与接口间隔设置、查找某个 QQ 号在哪些群 |

新功能的配置项不在「设置 → 全量配置」中显示（列表在那里按逗号拆分保存，会拆坏正则里的逗号）。接口见 `web_minimal.py`。

### 快速配置

1. 同意免责声明（`disclaimer_agreed`）。
2. 填写备份群 `backup_group_id`，机器人需在该群内。
3. 在「关键词与识别 → 撤回 / 禁言规则组」里写规则（第一次启动时原 `keyword_rules`、`wxcode_regex_rules` 已迁移成「默认组」）。关键词每行一条：`加微信`（包含即命中）、`=1`（整条消息就是“1”才命中）、`re:vx\s*\d{5,}`（正则）。
4. 葵花码：`wxcode_enabled` 打开，按 [examples/wxcode_recognizer_example.py](examples/wxcode_recognizer_example.py) 接入识别模块，在 `wxcode_regex_rules` 写正则，可选填写 `wxcode_forward_group_id`。
5. OCR（以后再开）：`local_ocr_driver` 选 `llbot`（LLBot 自带的 `ocr_image`），或选 `http` / `python` 接自己的服务。识别出的文字按 `keyword_rules` 过滤。

### 命中后的处理顺序

先把原消息以合并转发的形式备份到指定群（引用原消息 ID，图片完整保留；失败时依次回退为自定义节点、普通消息），再撤回。可选：`hit_mute_enabled` 同时禁言（默认关）、`hit_notice_enabled` 群内提示（默认关）。备份群 / 转发群本身不参与审核。

**其他撤回也先备份**（`backup_enabled`，发到 `backup_group_id`）：`/撤回最新消息`、`/批量撤回`、`/回复撤回`，以及退群撤回（主动退群、被踢，含机器人自己踢人 / 黑名单踢出触发的）。一次撤回多条时合并成一条转发（每 50 条一条），说明里写来源群、发送者、操作人、原因和条数；按消息 ID 引用失败时用已知的消息内容回退为自定义节点。退群撤回的操作记录里会注明「已备份 / 备份失败」。

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
| `/群名倒计时 YYYY-MM-DD ｜距考试{days}天 [群号,群号]` | 群名倒计时，每天 `countdown_update_time`（默认 00:05）自动更新；到期当天显示“今天”，过期后去掉后缀；更新时发现群名和插件上次改出的不一致（这期间被别人改过），不再改名，自动终止并删除该群的倒计时（写操作记录「群名倒计时终止」）。在 WebUI 里「改名」不算被别人改过 |
| `/群名倒计时 列表` / `停止 [群号]` / `立即更新` | 管理倒计时 |

不写群号时作用于机器人所在、插件允许管理的所有群。群之间间隔 `broadcast_interval_seconds`（默认 2 秒）。群名长度上限 `group_name_max_length`（默认 30，超出时缩短基础名）。每次改群名 QQ 都会在群里显示系统提示，插件无法屏蔽。

### 群列表与群成员从哪来（本地缓存）

群列表来自协议端 `get_group_list`，群成员来自 `get_group_member_list`。两者都缓存在本地 SQLite（`group_directory.py`，表 `dir_groups` / `dir_members`），重启不丢：

- **读**：缓存未过期直接用，过期才调用接口。群列表默认 1 小时、成员默认 6 小时（`directory_group_ttl` / `directory_member_ttl`）。接口失败时退回旧缓存。
- **限流**：所有列表类调用（群列表、成员列表、改名前读取群名）排队执行，相邻两次至少间隔 `directory_min_interval_ms`（默认 1500 毫秒）；同一个群同时只刷新一次。
- **实时更新**：入群、退群、被踢、管理员任免、改名片的通知直接更新缓存；机器人踢人成功、改群名成功后也会同步。
- **后台补齐**：每 5 分钟最多刷新 2 个过期的群（`directory_background_refresh_enabled`）。
- **使用方**：跨群踢人（机器人自己的身份也从成员缓存读取，不再单独查询）、批量公告和群名任务的目标群、管理页面的群列表、判断发言人是否管理员。

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

同一个 QQ 号同时在 2 个及以上「管理的群」里即为重复成员。检测范围可以在页面上勾选（meta 表键 `duplicate_member_scope`，没勾选 = 所有管理的群）。头像直接用 QQ 头像地址，不调用接口。只读本地成员缓存，不额外调用接口；页面会显示多少个群已缓存成员，没缓存的群不参与统计，可一键「刷新所有群的成员缓存」（后台逐个限速）。备份群 / 转发群、机器人自己不参与统计；插件管理员、用户白名单、在任一群是群主 / 管理员的人默认不列出，且插件管理员和白名单用户永远不会被处理。忽略名单存在 SQLite meta 表（键 `duplicate_member_ignore`）。

### 操作记录与拉黑原因

`operation_log.py`（表 `op_logs` / `blacklist_meta`）。每条记录：时间、来源（指令 / WebUI / 自动）、操作人（指令为发送者 QQ；WebUI 不带面板用户，留空）、操作、群、对象、原因、结果、详情。保留最近 10 万条。

记录的操作：踢人、踢出并拉黑、加入 / 移出黑名单、加入 / 移出审核白名单、关键词撤回、葵花码撤回、退群自动拉黑、退群撤回（撤回条数与失败原因）、黑名单入群踢出、黑名单发言踢出、通过 / 拒绝加群申请、发布群公告 / 待办公告、修改群名后缀（旧名 → 新名）、定时改群名的登记 / 取消 / 执行、群名倒计时的设置 / 更新 / 停止 / 结束 / 终止（群名被他人修改）、手动修改群名（旧名 → 新名）、修改关键词 / 葵花码规则、修改配置（旧值 → 新值）、刷新群成员缓存。

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
├─ moderation.py      消息审核主流程（_handle_message）：预检、去重、解析消息链与合并转发
│  └─ image_audit.py          图片下载：公网地址校验、本地临时文件读取、共享 HTTP 会话
├─ keyword_guard.py   关键词撤回 → 葵花码 / 二维码识别 → OCR 文字（_run_hit_guards）
├─ code_pipeline.py   二维码 / 葵花码两阶段识别（定位服务 → 裁剪 → 解码服务）
├─ code_board.py      二维码看板 + 码内容拉黑
├─ code_limit.py      码记录（统计 + 保存图片）、同一个码的发送次数限制（按码 / 按人，滑动窗口）
├─ card_parse.py      小程序 / 分享卡片解析（文字、appid、链接、封面图）
├─ image_recognizer.py / message_backup.py   识别器接口、备份到指定群
├─ membership.py      加群申请审核（通过词 / 拒绝词 / 默认动作）
├─ group_blacklist.py 跨群踢人 / 踢黑 / 退群自动拉黑
├─ leave_recall.py    退群 / 被踢后撤回近期消息
├─ leave_log.py       退群记录（退群前的消息、次数），退群拉黑按它计次
├─ auto_accept.py     自动同意好友申请 / 进群邀请
├─ live_log.py        实时日志：内存环形缓冲 + 处理步骤
├─ batch_queue.py     批量任务队列：逐项执行、间隔 + 抖动、自动 / 手动
├─ group_broadcast.py / group_name_task.py   批量公告 / 待办、群名后缀 / 倒计时
├─ group_directory.py / duplicate_members.py 群与成员本地缓存、重复成员
├─ operation_log.py   操作记录、黑名单原因
├─ commands.py        原版群管指令的业务实现
├─ scheduler.py       后台任务：定时解禁、群名任务、成员缓存补齐、批量任务执行器
├─ onebot.py          OneBot 接口封装、权限判定（_is_admin）、踢人 / 禁言底层
├─ web.py + web_minimal.py + pages/dashboard/index.html   WebUI 管理面板（API + 前端）
├─ storage.py / storage_group.py   SQLite 存储、按群配置
└─ utils.py / constants.py / single_flight.py   配置读取（_cfg）、常量、并发合并
```

所有功能共用的前置门槛：`enabled`（插件总开关）、`disclaimer_agreed`（同意免责声明），以及群白名单 / 群黑名单（`group_white_list` / `group_black_list`，在 WebUI「名单管理」维护）。

---

## 二、消息审核流水线（每条群消息）

入口 `main.py:_handle_message` → `moderation.py:_handle_message`。按顺序执行，前一步命中就结束：

| # | 步骤 | 开关 | 默认 | 命中后 |
| --- | --- | --- | --- | --- |
| 1 | 预检：插件总开关、免责声明、用户白名单、群黑白名单 | `enabled`、`user_white_list` 等 | — | 白名单用户 / 非管理群直接跳过 |
| 2 | 备份群 / 转发群本身不审核 | `backup_group_id`、`wxcode_forward_group_id` | — | 跳过 |
| 3 | QQ 开放平台机器人豁免 | `official_bot_exempt_enabled` | 默认关 | 跳过 |
| 4 | 机器人不是管理员时跳过 | `moderation_require_bot_admin` | 启用 | 跳过（无法撤回 / 禁言） |
| 5 | 管理员内容豁免 | `moderation_admin_exempt` | 默认关 | 跳过 |
| 6 | **用户黑名单**：发言即踢出并 QQ 拉黑 | `user_black_list`、`blacklist_native_block_enabled` | 启用 | 踢出 |
| 7 | 自动审核总开关（可按群） | `auto_moderate_enabled` | 启用 | 关闭则后面全部不做 |
| 8 | 同一条消息 60 秒内只处理一次 | — | — | 跳过重复事件 |
| 9 | 解析消息链、合并转发内容（含图片） | `scan_forward_msg` | 启用 | — |
| 9.5 | **小程序 / 分享卡片**（`card_parse.py`）：微信小程序 / QQ 小程序 / 链接分享等卡片拆出小程序名、标题、appid、跳转链接，拼成一行文字并入消息；封面图加入待识别图片 | `card_parse_enabled`、`card_scan_preview` | 启用 | — |
| 9.6 | **小程序识别与记录**（`miniapp.py`）：卡片（按 appid 区分）和文字里的 `#小程序://名称/内容` 链接（只有名字；已知卡片小程序里恰好一个名字以它开头时归并到它）。白名单里的小程序：卡片文字不参与关键词匹配。每次出现记一行 `mp_sends`（保留 `miniapp_store_days` 天），小程序名称 / 缩略图记在 `mp_apps`、`mp_contents` | `miniapp_store_enabled`、`miniapp_store_days` | 开；90 天 | — |
| 10 | **规则组：小程序 → 关键词 → 葵花码 / 二维码识别 → OCR 文字**（`keyword_guard.py` + `rule_groups.py`，见第零节）。第一个命中的规则组决定处理方式；码黑名单、码发送超限、解不出的码用默认组的处理方式 | `keyword_guard_enabled` / `wxcode_enabled` / `local_ocr_driver` | 启用 / 默认关 / 关 | 备份到指定群 + 撤回，按规则组禁言（分钟）、群内提示 |
| 10.5 | **码白名单**：`wxcode_allow_codes` 里的码（内容完全一致）一律放行，不按正则撤回、不计次；与黑名单 `wxcode_block_codes` 互斥 | — | 空 | 放行 |
| 10.6 | **同一个码的发送次数**：解出的码没命中规则时计次，`code_limit_window_hours` 小时内超过 `code_limit_max` 次撤回（`code_limit.py`） | `code_limit_enabled`、`code_limit_scope`（global 所有群合计 / group 每群单独）、`code_limit_count_by`（code 按码 / user 按人）、`code_limit_warn_enabled` + `code_limit_warn_template` | 默认关；5 小时 3 次，按码，警告开 | 同上，另发警告 |
| 10.7 | **码记录**：每个解出的码记一行（SQLite `code_sends`：时间、群、发送者、处理结果 放行 / 超限 / 规则 / 白名单），原图和裁剪图存到数据目录 `code_images/`（超过 `code_image_max_kb` 压缩，同一张图一份），保留 `code_store_ttl_hours` 小时后自动删除（0 = `code_limit_window_hours × 2`，不短于统计窗口）；WebUI「码管理」查看。同时按 (人, 码, 群) 长期累计到 `code_user_stats`（不随上面的保留时长清理），每个码在 `code_thumbs` 留一张小缩略图，供「按人统计」使用；升级后第一次启动会把还没清理的 `code_sends` 补进累计 | `code_store_enabled`、`code_image_max_kb`、`code_store_ttl_hours` | 开；500KB；0 | — |

每一步都会写入实时日志（`live_log.py`）。

---

## 三、群管指令

所有指令都需要“群操作权限”（插件管理员、群超管，或群主 / 群管理员，见第六节）。注册位置均在 `main.py`，业务在 `commands.py`（标注了的除外）。

> 下表的原有群管指令开关**默认全部关闭**，在 WebUI「设置 → 功能开关」里按需打开。二次开发新增的功能（`/全群踢人` `/全群踢黑` `/批量踢黑`、批量公告 / 待办、群名后缀 / 倒计时、WebUI 踢人与重复成员处理）不受这些开关影响，只检查插件总开关、免责声明和权限。已经安装过的实例保留原来保存的开关值。

| 指令 | 作用 | 功能开关 |
| --- | --- | --- |
| `/禁言` `/解禁` | 禁言 / 解除禁言某人 | `ban_enabled` / `unban_enabled` |
| `/踢人` | 踢出某人 | `kick_enabled` |
| `/批量禁言` `/批量踢人` | 当前群批量操作 | `ban_enabled` / `kick_enabled` |
| `/批量踢黑` | 当前群批量踢出并 QQ 拉黑（`group_blacklist.py`，新增） | 不受原生开关影响 |
| `/全群踢人` `/全群踢黑` | 所有管理的群踢人 / 踢黑，仅插件管理员（`group_blacklist.py`，新增） | 不受原生开关影响 |
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
| `/设置管理插件` | 管理插件管理员名单（仅插件管理员） | — |
| `/群管理授权` `/移除群管权限` `/恢复群管权限` | 权限管理（见第六节） | `group_admin_grant_enabled` |

---

## 四、入群与黑名单

| 功能 | 位置 | 开关 | 默认 |
| --- | --- | --- | --- |
| 加群申请自动审核：通过词 / 拒绝词（`=` 开头为精确匹配）/ 默认动作 | `membership.py`，规则在 WebUI「入群审核」页按群配置 | `join_audit_enabled`、`join_accept_overrides_lexicon`（精确通过词优先于拒绝词）、`join_default_action` | 默认关 |
| 审核结果群内通知 | 同上 | `join_audit_notify` | 启用 |
| 用户黑名单：申请自动拒绝（无需开入群审核） | `membership.py` | `blacklist_reject_join_enabled` | 启用 |
| 黑名单用户入群即踢 | `group_blacklist.py` | `blacklist_kick_on_join` | 启用 |
| 踢黑名单时 QQ 原生拉黑 | `onebot.py:_kick_member` | `blacklist_native_block_enabled` | 启用 |
| 退群自动拉黑（全群通用）：`leave_auto_blacklist_days` 天内主动退群达到 `leave_auto_blacklist_times` 次才拉黑（按退群记录计次，被踢不算；次数 1 = 一退群就拉黑。协议端上报为主动退群但操作人是别人时按被踢算；同一次退群的重复通知——同群、同人、同操作人、同一秒——只记一次） | `group_blacklist.py` | `leave_auto_blacklist_enabled`、`_times`、`_days` | 默认关；30 天 3 次 |
| 退群记录：主动退群 / 被踢时记录时间、群、方式、操作人和退群前最近 N 条消息（摘要只存内存，不足时群历史兜底），WebUI「退群记录」按 QQ / 群查询、看某人汇总 | `leave_log.py`（表 `leave_logs`，保留 5 万条） | `leave_log_enabled`、`leave_log_msg_count`、`leave_log_use_history` | 默认开，10 条 |
| 退群记录图片：记录消息时顺带记下图片地址（每条最多 4 张，只在内存）；退群时后台下载、压缩到 500KB 以内存到数据目录 `leave_images/`，链接失效时用 `get_image` 向协议端取本地缓存；WebUI 显示缩略图、点开看大图；保留 `leave_log_image_days` 天后只删图片 | `leave_log.py`（表 `leave_images`） | `leave_log_save_images`、`leave_log_image_days` | 默认开；30 天 |
| 自动同意好友申请 / 自动接受进群邀请 | `auto_accept.py`（main `_on_friend_request`、`_on_group_request` 先于入群审核处理 invite） | `auto_accept_friend_enabled` / `_friend_scope`（all / admin / whitelist）、`auto_accept_invite_enabled` / `_invite_scope`（admin / all / whitelist）、白名单 `auto_accept_{friend,invite}_wl_users` / `_wl_groups`、`auto_accept_delay_seconds` | 都默认关；邀请默认仅接受插件管理员的；黑名单用户好友申请拒绝、邀请不接受；不在范围内的留给人工。白名单 = 插件管理员 + 白名单 QQ + 成员来源群（`_wl_groups`，从这些群提取群成员）的群成员，好友申请和邀请各一套，不在白名单的不处理；只决定是否自动通过，与管理范围的群白名单 / 群黑名单无关；群成员先查本地缓存，没找到时把超过 60 秒没拉取的成员来源群重新拉一次成员列表再比对（每群 60 秒最多一次）。WebUI「踢人与黑名单」可测试某个 QQ 能否通过（接口 `/minimal/accept/check`） |
| 二维码看板：原图 + 每个码的裁剪图、识别结果；复制、拉黑 / 解除拉黑该码 | `code_board.py`、`web_minimal.py:_web_minimal_codeboard*` | `code_board_enabled`、`code_board_size`、`wxcode_block_codes`（已拉黑的码，完全一致即命中，优先于正则规则） | 看板默认开（只存内存，30 张），名单默认空 |
| 实时日志：WebUI 查看近期消息处理进度与耗时 | `live_log.py`、`web_minimal.py:_web_minimal_livelog` | `live_log_enabled`、`live_log_show_text`、`live_log_size` | 默认开（只存内存，300 条） |
| 机器人进群 / 加好友推送到备份群：进群（group_increase 里 user_id 是机器人）发群名、群号、人数、邀请人；加好友（friend_add）发昵称、QQ、验证消息、是否自动同意 | `bot_notify.py` | `bot_notify_join_enabled`、`bot_notify_friend_enabled` | 都默认开；没填备份群不发 |
| 进群后自动设置机器人的群消息接收方式（LLBot `set_group_msg_mask`，默认 4 = 接收消息但不提醒）：进群通知触发，群成员缓存后台刷新时补漏；第一次开启时已在的群记为基线不改动；已有的群在「踢人与黑名单」批量设置（批量任务） | `group_msg_mask.py`（meta 表 `msg_mask_state`） | `auto_msg_mask_enabled`、`auto_msg_mask_value` | 默认关 |
| 退群撤回：主动退群 / 被踢后撤回其最近 N 条、最近 M 分钟的消息 | `leave_recall.py` | `leave_recall_enabled`（`_on_leave` / `_on_kick` / `_count` / `_minutes` / `_use_history`） | 默认关 |

---

## 五、后台任务（`scheduler.py`）

| 任务 | 开关 | 默认 |
| --- | --- | --- |
| 定时自动解禁（插件发起的禁言到期解除，失败退避重试） | `auto_unban_enabled`、`auto_unban_scan_interval`、`auto_unban_permanent_hours` | 默认关 |
| 群名定时改名 / 倒计时每日更新 | 随群名任务 | 每分钟检查 |
| 群成员缓存后台补齐 | `directory_background_refresh_enabled` | 启用 |
| 批量任务执行器 | `batch_*` | 常驻 |

---

## 六、权限体系（`onebot.py:_is_admin`）

判定顺序：① 群级 bot 权限黑名单（`/移除群管权限`，最高优先）→ ② 插件管理员（`admin_list` + AstrBot 全局管理员）→ ③ 群超管（WebUI「权限」页按群设置）→ ④ 群主 / 群管理员（受 `group_admin_grant_enabled` 动态授权、`legacy_role_admin_enabled` 与 `strict_permission_enabled` 控制）。

**严格权限 `strict_permission_enabled`（默认开）**：第 ④ 步只放行群主，QQ 群管理员不再自动获得指令权限，需要在 WebUI 设为插件管理员 / 群超管，或用 F5 动态授权为该群显式授予。关闭后恢复为群主和群管理员都自动拥有本群权限。

另外两个严格模式：`member_action_require_group_role`（群管指令只允许本群群主 / 管理员）、`set_admin_require_owner`（任免管理员只允许群主）。默认都关。

---

## 七、批量任务（`batch_queue.py`）

所有「多个群 / 多个人」的写操作都先创建批量任务，每一项 = 群 + 行为 + 对象，由后台执行器逐项执行：

| 来源 | 每一项 |
| --- | --- |
| `/全群踢人` `/全群踢黑`、WebUI 批量踢人 | 群 × QQ（本地缓存确认不在群里的不建项）；拉黑在创建任务时立即生效 |
| `/批量踢黑` | 当前群 × QQ（只做 QQ 原生拉黑） |
| WebUI 重复成员处理 | 人 × 要踢的群 |
| `/全群公告` `/全群待办`（确认后）、WebUI 批量公告 | 每个群一项；待办全部执行完后记录「最近一次待办」 |
| `/群名后缀`、`/群名倒计时`、立即更新、WebUI 群名任务 | 每个群一项 |
| 定时改群名、倒计时每日更新（后台每分钟检查） | 每个群一项，始终自动执行；已在队列里的倒计时不会重复建任务 |

- 同一时间只执行一项；两项之间等待 `batch_interval_seconds` +（0~`batch_jitter_seconds` 的随机值），单个任务可单独设置。
- `batch_default_mode`：auto 自动执行 / manual 等待手动（执行下一项、执行某一项、全部执行）。
- 任务可暂停、继续、取消剩余项、重试失败项；单项可执行或跳过。
- 群指令创建的任务完成后在该群发汇总（`batch_notify_enabled`）。
- 存在 SQLite `batch_jobs` / `batch_items`，保留最近 300 个已结束的任务；插件重启时「执行中」的项记为失败（可重试），不会自动重做。
- QQ 未连接时项保持待执行，连接后继续。
- `broadcast_interval_seconds` 已停用（由上面的间隔取代）。
- WebUI 右侧固定一列「批量任务」栏（所有页面可见，可折叠，折叠状态记在浏览器里；宽度不足 1280 时默认折叠，不足 900 时隐藏）：执行器状态、最近 20 个任务的进度和快捷操作，点任务打开「批量任务」页的明细。刷新频率：自动任务执行中 2 秒，有待手动 / 暂停的任务 8 秒，空闲或折叠 20 秒，页面在后台时不刷新。

## 八、WebUI 管理面板（`web.py` + `web_minimal.py` + `pages/dashboard/index.html`）

侧边栏分组：消息审核（关键词与识别、码管理、实时日志、码大屏、违规记录）、成员管理（踢人与黑名单、重复成员、退群记录、入群审核、定时解禁）、群运营（批量公告、群名任务、批量任务）、系统（群成员缓存、操作记录、权限管理、多群配置、设置）。第零节以外的页面：

| 页面 | 内容 |
| --- | --- |
| 违规记录 | 被撤回用户、审核日志、导出 CSV。日志里的图片写入后在后台保存（`media_store.py`：AstrBot 临时目录里的文件直接读，QQ 链接下载，压缩到 500KB 以内存到 `log_images/`），日志里的地址换成 `saved:<sha1>`，详情里看缩略图、点开看大图，不再显示本机路径；读不到的本机路径记为「未能保存」，读不到的链接原样保留。开关 `log_image_save_enabled`（默认开），保留 `log_image_days` 天（默认 30） |
| 设置 | 全部开关、名单管理、SQLite 迁移助手（导入旧审核日志 JSON） |
| 入群审核 | 按群配置通过 / 拒绝词 |
| 权限管理 | 插件管理员、群超管、动态授权、群权限黑名单 |
| 多群配置 | 按群覆盖任意配置项 |
| 定时解禁 | 定时解禁计划 |

插件页面运行在 AstrBot 的沙箱 iframe 里，原生 `confirm` / `alert` / `prompt` 不可用，统一用页面内的弹窗。

---

## 九、接口调用频率（按几十个 2000 人群设计）

普通群消息不调用协议端接口：发送者角色、黑名单、关键词、码计次都读本地缓存 / SQLite；图片只从 QQ 图床下载，识别走自己部署的服务。会调接口的地方：

| 场景 | 接口 | 频率 / 限制 |
| --- | --- | --- |
| 群列表 | `get_group_list` | 缓存 1 小时 |
| 群成员（2000 人一次拉完） | `get_group_member_list` | 每群缓存 6 小时；后台每 5 分钟最多补 2 个过期的群；所有列表类调用排队，间隔 ≥ 1.5 秒；进退群 / 管理员任免 / 改名片靠通知实时更新，不重拉 |
| 机器人在某群的角色 | 优先读成员缓存，没有才 `get_group_member_info` | 每群缓存 5 分钟，同群并发合并 |
| 合并转发内容 | `get_forward_msg` | 每条合并转发一次（有总时限） |
| 命中后备份 / 撤回 / 禁言 / 提示 | `send_group_forward_msg`、`delete_msg`、`set_group_ban`、`send_group_msg` | 撤回每条都做；同一个人连续命中时，禁言、群内提示、码超限警告 60 秒内只做一次 |
| 退群撤回 / 退群记录的群历史兜底 | `get_group_msg_history` | 只在插件启动后 6 小时内拉取；同一个群 60 秒内只拉一次，两个功能共用（批量踢 200 人也只拉一次） |
| 退群撤回 | `delete_msg` | 所有人的撤回排队执行，每条间隔 0.3 秒 |
| 批量任务（踢人、公告、改群名、消息设置） | 各自接口 | 全局一次一项，间隔 + 随机抖动 |
| 进群后的消息提醒 | `set_group_msg_mask` | 每个新群一次；漏报兜底每轮最多 10 个群 |

WebUI 页面的轮询（实时日志、批量任务栏）只读插件内存 / SQLite，不调协议端。

## 十、已删除的原版功能

以下原版功能与现行功能冲突或不再需要，代码、配置项、WebUI 页面和接口都已删除（需要时可从 git 历史找回）：

| 功能 | 原代码 |
| --- | --- |
| AI 消息审核 / 全量 AI 审核、OCR 识图、入群 AI 审核、名片 AI 判定、AI 误判复盘、大模型群管工具与提示词注入 | `moderation.py` 中的 LLM 调用、`llm_stream.py`、`moderation_review*.py`、`membership.py` 中的入群 LLM |
| 防刷屏（频率 / 夜间限速 / 重复消息 / 长文本）、刷屏申诉 | `anti_flood.py`、`appeal.py` |
| 内置脏话 / 广告规则、外置词库（`lexicon.db`）、自定义违禁词指令、组合消息检测、Base 解码、混淆网址还原、链接白名单、QQ 收藏撤回、AI 中转数字检测 | `automaton.py`、`lexicon_migration.py`、`encoded_content.py`、`text_obfuscation.py`、`link_whitelist.py`、`ai_relay.py`、`moderation_context.py` |
| 本地二维码解码（OpenCV / pyzbar） | `image_audit.py`（已由 `code_pipeline.py` 两阶段识别取代） |
| 名片监控 / 名片保护 / 管理员任免通知、词库自学习 | `card_monitor.py`、`lexicon_learn.py` |
| 「群管理」页与远程群管（WebUI 里直接禁言 / 踢人 / 改名片） | `remote.py`（跨群踢人、重复成员等改走批量任务） |
| 踢人时撤回近期消息（`kick_recall_*`） | 由「退群撤回」（`leave_recall.py`）取代 |
| AI 总闸（`AI_FEATURES_ENABLED`） | `constants.py`（没有 AI 代码后不再需要） |

保留：定时自动解禁（原生 `/禁言` 会用到）、入群审核的通过词 / 拒绝词、原版群管指令（默认关闭）。旧版本留在 `group_guardian.db` 里的词库 / 规则 / 申诉 / 名片 / 学习表不再创建也不读取，原样保留。
