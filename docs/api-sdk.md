# API SDK 与 cURL 接入说明

## 公共接口

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/external/models` | `models.read` | 查询当前调用方允许使用的模型 |
| `GET` | `/api/v1/external/usage` | `usage.read` | 查询当前调用方 UTC 日/累计请求与 Token 配额 |
| `POST` | `/api/v1/external/chat/completions` | `chat.completions` | 非流式或 SSE 流式问答 |

生产环境必须使用 HTTPS。SDK 和 cURL 脚本只允许在 `localhost`、`127.0.0.1`、`::1` 使用明文 HTTP，且都拒绝带用户名、密码、路径、查询串或片段的基础地址。

## Python SDK

安装仓库内 SDK：

```powershell
python -m pip install .\sdk\python
```

凭据只从部署 Secret 管理或进程环境读取，不要写入源代码：

```python
import os

from zangpu_sdk import ZangpuAPIError, ZangpuClient

with ZangpuClient(
    base_url=os.environ["ZANGPU_API_BASE_URL"],
    key_id=os.environ["ZANGPU_API_KEY_ID"],
    secret=os.environ["ZANGPU_API_SECRET"],
) as client:
    models = client.list_models()
    usage = client.get_usage()
    result = client.chat_completions(
        model=models.data["data"][0]["id"],
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=256,
        request_id="req_business_20260808_0001",
    )
    print(result.data)
    print(result.request_id)
```

SSE 流式调用：

```python
with ZangpuClient(
    base_url=os.environ["ZANGPU_API_BASE_URL"],
    key_id=os.environ["ZANGPU_API_KEY_ID"],
    secret=os.environ["ZANGPU_API_SECRET"],
) as client:
    for event in client.stream_chat_completions(
        model="replace-with-allowed-model",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=256,
    ):
        print(event)
```

SDK 忽略 SSE 心跳注释，仅返回经过 JSON 校验的 `data:` 事件；如果缺少 `[DONE]`、事件过大、内容类型错误或返回体不是合法协议，会抛出 `ZangpuProtocolError`。HTTP 错误抛出 `ZangpuAPIError`，可读取 `code`、`status_code`、`request_id`、`operation_id` 和 `retryable`，异常中不包含原始响应正文。

## PowerShell 与 cURL

脚本要求 PowerShell 7 和系统 `curl.exe`。它从环境变量读取 Secret，并使用 `curl.exe --data-binary` 发送与签名时完全相同的请求字节：

```powershell
$env:ZANGPU_API_BASE_URL = 'https://api.example.com'
$env:ZANGPU_API_KEY_ID = '<issued-key-id>'
$env:ZANGPU_API_SECRET = '<load-from-secret-manager>'

.\examples\curl\zangpu-curl.ps1 -Path /api/v1/external/models
.\examples\curl\zangpu-curl.ps1 -Path /api/v1/external/usage -IncludeResponseHeaders
.\examples\curl\zangpu-curl.ps1 `
  -Method POST `
  -Path /api/v1/external/chat/completions `
  -BodyFile .\examples\curl\chat.json `
  -RequestId req_business_20260808_0001
.\examples\curl\zangpu-curl.ps1 `
  -Method POST `
  -Path /api/v1/external/chat/completions `
  -BodyFile .\examples\curl\chat-stream.json
```

脚本不会输出 Secret、nonce、签名或 canonical request。需要查看 HTTP 状态和服务端 `X-Zangpu-Request-Id` 时使用 `-IncludeResponseHeaders`。`chat.json` 和 `chat-stream.json` 中的模型名必须替换为 `/models` 返回的允许模型。

## 签名与重试

签名版本 `1` 的 canonical request 固定为十行：算法、版本、HTTP 方法、规范化路径、规范化查询、原始 body SHA-256、Key ID、Unix 秒时间戳、nonce、调用方 request ID。签名为使用调用方 Secret 计算的 HMAC-SHA256 小写十六进制值。

每次网络尝试必须使用新的 nonce。聊天接口不会保存或重放回答，因此 SDK 不自动重试：

- 请求明确未发出时，可以重新生成 request ID 后调用。
- 请求是否到达服务端不确定时，应复用原 request ID、生成新 nonce 后重试。
- 服务端可能返回 `REQUEST_IN_PROGRESS` 或 `REQUEST_ALREADY_COMPLETED`；后者只证明原请求已终态，不包含原回答。
- 只有 `retryable=true` 且业务能够接受上述幂等语义时才重试，不要仅凭 HTTP 5xx 无限循环。

响应头 `X-Zangpu-Request-Id` 是服务端追踪 ID，与调用方签名中的 request ID 不同。排障时记录服务端追踪 ID 和稳定错误码，不记录 Secret、签名、nonce、prompt、回答或原始上游错误。
