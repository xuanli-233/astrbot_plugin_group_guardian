# 解码服务 API（二维码 / 葵花码解码）

两阶段识别的第二步：上传一张图片（插件上传的是[定位服务](定位服务API.md)给出的框扩大后的裁剪图），返回一个解码结果。

| 项目 | 值 |
| --- | --- |
| 默认地址 | `http://127.0.0.1:8000`（插件配置 `code_decode_url`，可改 IP / 端口） |
| 接口 | `POST /api/decode` |
| 鉴权 | 无 |
| 并发 | 同一时刻只处理一个任务，忙时返回 `503 BUSY` |
| 结果数量 | 每次最多返回**一个**码 |

## 1. `POST /api/decode`

### 1.1 请求

`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `image` | 图片文件 | 是 | 包含普通二维码或葵花码的图片 |

- 支持静态 **PNG / JPEG / WebP**
- 图片文件最大 **16 MiB**，整个请求最大 **17 MiB**
- 宽 × 高不超过 **16,777,216 像素**（例如 4096×4096）
- 可以上传完整截图，不要求事先裁剪，但最多返回一个码
- 不接收图片网址、Base64 JSON 或框坐标

```bash
curl --max-time 120 -F "image=@test.png" http://127.0.0.1:8000/api/decode
```

### 1.2 成功响应（HTTP 200）

```json
{
  "success": true,
  "image": {"width": 370, "height": 370, "coordinate_space": "exif_oriented_pixels"},
  "result": {
    "code_type": "QR_CODE",
    "charset": "ISO8859-1",
    "payload_base64": "aHR0cHM6Ly9leGFtcGxlLmNvbS90ZXN0P3E9MTIz",
    "payload_length": 30,
    "text": "https://example.com/test?q=123"
  },
  "elapsed_ms": 20639.4
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `success` | boolean | 成功时为 `true` |
| `image.width` / `height` | integer | 应用 EXIF 方向后的图片宽高 |
| `image.coordinate_space` | string | 固定为 `exif_oriented_pixels` |
| `result.code_type` | string | `QR_CODE`：普通二维码；`WX_CODE`：葵花码 |
| `result.charset` | string | 原生解码器报告的字符集 |
| `result.payload_base64` | string | 原始载荷字节的 Base64，无损保存 |
| `result.payload_length` | integer | 原始载荷字节数 |
| `result.text` | string 或 null | 按已知字符集严格转换的文本；无法转换时为 `null` |
| `elapsed_ms` | number | 图片预处理与解码耗时（毫秒），不含网络上传 |

通常读 `result.code_type` 和 `result.text` 即可；需要精确保留内容时保存 `payload_base64`。葵花码的 `text` 可能是不透明字符串，不一定是网址。

当前**没有**框、置信度、多个码的结果列表，也不会返回小程序 AppID 或页面路径。

### 1.3 失败响应

失败时使用不同的 HTTP 状态码，响应体只有 `error` 对象（**不包含** `success: false`）：

```json
{"error": {"code": "NO_CODE", "message": "未能解出支持的二维码或葵花码"}}
```

| HTTP | `error.code` | 含义 |
| --- | --- | --- |
| 400 | `INVALID_IMAGE` | 缺少图片、图片损坏或格式不支持 |
| 413 | `IMAGE_TOO_LARGE` | 图片文件或总像素超限 |
| 413 | `PAYLOAD_TOO_LARGE` | 整个请求体超限 |
| 422 | `NO_CODE` | 本次未能解出码，**不代表图片里一定没有码** |
| 503 | `BUSY` | 正在处理其他任务，可稍后重试 |
| 503 | `RUNTIME_ERROR` | 运行库、进程或原生输出异常 |
| 504 | `DECODE_TIMEOUT` | 原生解码超过 90 秒 |

## 2. 插件如何调用（`code_pipeline.py`）

| 项目 | 插件的做法 |
| --- | --- |
| 地址 | `code_decode_url`，只写 `http://IP:端口` 即可，自动拼接 `/api/decode` |
| 请求 | multipart 字段 `image`，文件名 `crop.png`；内容是定位框扩大后的 PNG 裁剪图（没有 Pillow 时为整张原图） |
| 一张图多个码 | 按定位服务的 score 从高到低，**逐个**送解码，等上一个返回再送下一个 |
| 命中即停 | 每解出一个码立即匹配「码内容正则规则」，命中后不再解码这张图剩下的码，也不再识别这条消息后面的图，立刻撤回 |
| 并发 | 插件对本服务同时只发一个请求（排队）；与定位服务各排各的队，互不阻塞 |
| 重试 | `503 BUSY` 等 2 秒后最多重试 2 次；`503 RUNTIME_ERROR`、`504` 等直接视为失败 |
| 超时 | `code_decode_timeout`，默认 120 秒（服务内部最长 90 秒） |
| 成功的判断 | HTTP 200 且 `success=true` 且有 `result` 对象 |
| 读取的字段 | `result.code_type`、`result.text`、`result.payload_base64` |
| 用于匹配的内容 | `text`；`text` 为 `null` 时用 `payload_base64` 参与正则匹配 |
| `422 NO_CODE` | 记为「未能解出」，按 `code_undecoded_action` 处理（默认放行，可选撤回） |
| 其它失败 | 记录原因；该码不参与判断，不影响同一张图的其它码；不会因此误撤回 |
