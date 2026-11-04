# 定位服务 API（YuNet 广告码检测）

两阶段识别的第一步：上传整张图片，返回图中各类码的位置框。插件根据框裁剪后再交给[解码服务](解码服务API.md)。

| 项目 | 值 |
| --- | --- |
| 默认地址 | `http://127.0.0.1:8100`（插件配置 `code_detect_url`，可改 IP / 端口） |
| 模型 | `yunet_n_640_640_newest`（640×640，六类） |
| 计算后端 | ONNX Runtime CPU，单进程，单推理线程 |
| 鉴权 | 请求头 `X-API-Key` |
| 并发 | 上传 → 推理全程串行，忙时返回 `503` 并带 `Retry-After: 1` |

> 本文不包含真实密钥。密钥只填在插件配置 `code_detect_api_key` 里（管理页面不回显），不要写进代码、文档或发到群聊。

## 1. 鉴权

所有 `POST /v1/detect` 请求必须带请求头：

```
X-API-Key: <API_KEY>
```

- 服务端密钥来自部署目录的 `config.json`（`api_key`，至少 32 字符）；设置了环境变量 `YUNET_API_KEY` 时以环境变量为准。
- 密钥缺失或错误返回 `401`，响应体 `{"detail":"Invalid X-API-Key"}`。
- 轮换密钥：改 `config.json` 或环境变量后重启服务，并在插件管理页面填入新密钥。

## 2. `GET /health`

探活，无需鉴权。

```bash
curl http://127.0.0.1:8100/health
```

```json
{"status": "ok", "ready": true}
```

`ready: true` 表示模型已加载并完成预热。启动中或模型校验失败时为 `false` 或不可达。

## 3. `POST /v1/detect`

### 3.1 请求

- `Content-Type: multipart/form-data`，不要手动写 boundary
- 只接受**一个**名为 `file` 的文件字段
- 不接受图片 URL 或服务器磁盘路径，必须上传字节
- 支持单帧 **JPEG / PNG / WebP / BMP**；GIF、动图、损坏文件会被拒绝

```bash
curl -X POST "http://127.0.0.1:8100/v1/detect" \
  -H "X-API-Key: $YUNET_API_KEY" \
  -F "file=@poster.jpg"
```

### 3.2 成功响应（HTTP 200）

```json
{
  "request_id": "2e8c2063-1f1c-4290-82be-2d8445d200a5",
  "model": "yunet_n_640_640_newest",
  "model_sha256": "af50cabce9d24bd27de55af8b7baedcc3e6b7be2e9fecb3294916495c64e3886",
  "image": {"width": 1200, "height": 900, "coordinate_space": "exif_oriented_pixels"},
  "thresholds": {"score": 0.5, "nms_iou": 0.5},
  "has_code": true,
  "has_kuihua": false,
  "detections": [
    {"class_id": 3, "class_name": "qrcode", "score": 0.826221, "bbox": [49, 31, 569, 576]}
  ],
  "timings": {"decode_ms": 6.518, "preprocess_ms": 2.179, "inference_ms": 17.871,
              "postprocess_ms": 0.799, "processing_ms": 27.701}
}
```

未检出时仍是 **200**：`has_code=false`、`has_kuihua=false`、`detections=[]`。请求失败一定返回非 200，不会用空结果伪装失败。

### 3.3 字段

| 字段 | 含义 |
| --- | --- |
| `has_code` | 至少保留下一个检测框（六类任意） |
| `has_kuihua` | 至少有一个 `class_id=2`（葵花 / 小程序码类） |
| `detections` | 按 `score` 降序的检测框列表 |
| `score` | 类别分数 × objectness，**不是**校准概率 |
| `bbox` | `[x1, y1, x2, y2]`，左上 / 右下角像素坐标 |
| `image.width` / `height` | **纠正 EXIF 方向后**的整图宽高 |
| `processing_ms` | 解码 + 预处理 + 推理 + 后处理，不含上传与 HTTP 往返 |
| `model_sha256` | 权重版本指纹，用于确认模型未被替换 |

坐标相对整张原图（不是 640×640 输入，也不是中心裁剪）：原点左上，x 向右、y 向下，按 `[x1, x2)`、`[y1, y2)` 使用，宽 `x2 - x1`，高 `y2 - y1`。显示未做 EXIF 纠正的像素时需先统一方向。

### 3.4 类别

| class_id | class_name | 说明 |
| --- | --- | --- |
| 0 | `aruco` | ArUco 标记 |
| 1 | `barcode` | 一维条形码 |
| 2 | `kuihua` | 葵花 / 小程序码类，不保证只来自微信 |
| 3 | `qrcode` | 普通二维码 |
| 4 | `pdf417` | PDF417 |
| 5 | `datamatrix` | Data Matrix |

### 3.5 输入限制

| 限制 | 值 |
| --- | --- |
| 单文件大小 | 10 MiB |
| 总像素 | 12,000,000 |
| 最长边 | 12,000 px |
| 上传超时 | 30 s |

### 3.6 状态码

| 码 | 含义 | 调用方处理 |
| --- | --- | --- |
| 200 | 成功（含空列表） | 按 `detections` / `has_kuihua` / `has_code` 判断 |
| 401 | 密钥错误或缺失 | 检查 `X-API-Key` 与环境变量覆盖 |
| 400 / 422 | 表单字段错误、文件不是恰好一个、空文件 | 修正请求体 |
| 408 | 上传超时 | 重试 |
| 413 | 文件过大 / 像素或边长超限 | 压缩或缩放后再传 |
| 415 | 格式不支持、文件损坏、非 multipart | 换图或改 `Content-Type` |
| 500 | 服务异常 | 查服务日志 `logs\service.log` |
| 503 | 服务忙（同一时刻只处理一个请求） | 约 1 秒后**有限次**重试，不能当作「无码」 |

## 4. 插件如何调用（`code_pipeline.py`）

| 项目 | 插件的做法 |
| --- | --- |
| 地址 | `code_detect_url`，只写 `http://IP:端口` 即可，自动拼接 `/v1/detect`；探活用 `/health` |
| 请求 | multipart 字段 `file`，文件名按图片格式为 `image.png` / `.jpg` / `.webp` / `.bmp`，请求头 `X-API-Key` |
| 超限图片 | 超过 10 MiB / 1200 万像素 / 最长边 12000 时，先缩小并转 JPEG（质量 90）再上传，返回的框按比例换算回原图 |
| 并发 | 插件对本服务同时只发一个请求（排队），不并发 |
| 重试 | `503` 按 `Retry-After`（1～5 秒）最多重试 3 次；其它非 200 直接视为失败 |
| 超时 | `code_detect_timeout`，默认 30 秒 |
| 读取的字段 | `detections[].class_id`、`score`、`bbox`；`has_code` 仅用于测试页展示 |
| 使用哪些框 | 只取 `class_id` 为 2（葵花码，`code_decode_kuihua`）和 3（二维码，`code_decode_qrcode`）的框，按返回顺序（score 降序）最多 `code_max_per_image` 个（默认 3） |
| 框的处理 | 按框宽高向四周各扩大 `code_box_expand_ratio`%（默认 20%，至少 16 像素），限制在图内；先 EXIF 方向纠正再裁剪成 PNG，交给解码服务 |
| 失败时 | 连接失败、超时、401、重试用尽：本条消息**放行**并写日志，不会误撤回 |
