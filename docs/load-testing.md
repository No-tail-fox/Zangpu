# k6 基础压测

本交付用于验证签名 API 的可用性、失败率、吞吐和延迟分位数。它不预设合同之外的生产 SLA，也不把本地回环结果当作 PostgreSQL、Valkey、Open WebUI 与 Bifrost 联合部署的性能结论。

## 测试目标

`models` 是默认目标，只执行签名、nonce、调用方策略与 QPS 链路，不调用模型或扣减积分。`usage` 读取当前调用方的 SQL 用量。`chat` 会执行完整推理和积分结算，可能产生实际费用，必须显式确认。

脚本提供四个可配置档位：

| 档位 | 默认行为 | 用途 |
| --- | --- | --- |
| `smoke` | 1 VU、10 次请求 | 上线前签名和响应契约检查 |
| `steady` | 5 请求/秒、30 秒 | 持续吞吐和 P95 检查 |
| `burst` | 2 到 20 请求/秒、30 秒 | 短时突发和 VU 容量检查 |
| `concurrency` | 每 VU 单次、同时启动 | 精确核对已准入数量和超额 429 |

这些默认值只用于安全起步。正式验收前，应由项目负责人填写目标请求率、时长、P95 上限和允许失败率，并确保调用方 QPS、并发、Token 与积分额度覆盖测试规模。

## 环境准备

使用经过校验的 k6 可执行文件，并通过进程环境提供凭据。Secret 不得写进脚本、命令参数、结果文件或 CI 日志。

```powershell
$env:K6_EXE = "I:\tools\k6\k6.exe"
$env:ZANGPU_API_BASE_URL = "https://api.example.invalid"
$env:ZANGPU_API_KEY_ID = "<调用方 Key ID>"
$env:ZANGPU_API_SECRET = "<仅在当前安全进程中设置>"
```

非回环地址只接受 HTTPS。每次迭代都会生成新的 nonce 和调用方 request ID，聊天请求不会自动重试。脚本设置 `responseType: none`，不会保留或输出提示词、回答或原始错误响应。

## 执行

低成本 smoke：

```powershell
.\load\k6\run.ps1 -Profile smoke -Target models -P95Milliseconds 1500
```

持续压测可通过环境覆盖请求率、时长和 VU：

```powershell
$env:ZANGPU_LOAD_RATE = "20"
$env:ZANGPU_LOAD_DURATION_SECONDS = "120"
$env:ZANGPU_LOAD_PREALLOCATED_VUS = "40"
$env:ZANGPU_LOAD_MAX_VUS = "200"
.\load\k6\run.ps1 -Profile steady -Target models -P95Milliseconds 1500 -MaxFailureRate 0.01
```

聊天压测需要模型名和显式费用确认：

```powershell
$env:ZANGPU_LOAD_MODEL = "<允许的模型 ID>"
$env:ZANGPU_LOAD_MAX_TOKENS = "64"
.\load\k6\run.ps1 -Profile smoke -Target chat -P95Milliseconds 30000 -ConfirmChatSpend
```

并发验收必须填写与该调用方当前配置一致的上限，并让尝试数大于上限：

```powershell
$env:ZANGPU_LOAD_MODEL = "<允许的模型 ID>"
$env:ZANGPU_LOAD_MAX_TOKENS = "16"
.\load\k6\run.ps1 -Profile concurrency -Target chat -ExpectedConcurrencyLimit 2 -ConcurrencyAttempts 5 -P95Milliseconds 30000 -ConfirmChatSpend
```

该档位要求恰好 `ExpectedConcurrencyLimit` 个 200，其余请求恰好为 429；所有响应必须带正确的 `X-Concurrency-Limit`、数字型 `X-Concurrency-Remaining`/`X-Concurrency-Reset`，429 还必须为零剩余并带正整数 `Retry-After`。脚本仍使用 `responseType: none`，不会为了检查 429 而保留模型回答或原始错误正文。为形成有效重叠，目标模型响应时间应足以让全部 VU 到达准入点；若调用方 QPS 小于尝试数，QPS 会先拒绝请求，不能作为并发验收结果。

运行器将聚合 JSON 和纯文本摘要写入 `.tmp/k6-results`。阈值失败时 k6 返回非零退出码，但仍保留可用于排查的脱敏汇总。

## 验收与留证

每次真实验收都应从 [result-template.md](../load/k6/result-template.md) 复制一份报告，填写环境版本、请求档位、调用方限制、P95、失败率、4xx/5xx、dropped iterations 和结论，并引用同次运行的 JSON 文件校验值。

当前仓库已使用官方签名的 k6 v2.1.0 完成真实 HTTP 回环：同一签名模块匹配固定 HMAC 向量，三次 metadata 请求的 nonce/request ID 均唯一；并发回环在限制 2、尝试 5 时得到精确的 2 个已准入、3 个稳定 429 和服务端峰值 2，脱敏 JSON/text 汇总通过。该并发回环使用本地受控服务，不调用真实模型或扣费。当前二进制版本、哈希和基础证据记录在 [2026-08-08-local-delivery-validation.md](../load/k6/results/2026-08-08-local-delivery-validation.md)。真实四服务结果仍需在目标部署环境执行，状态必须保留为“待执行”，直到对应报告和聚合结果完成归档。
