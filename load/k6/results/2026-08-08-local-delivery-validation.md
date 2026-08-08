# k6 本地交付验证结果

## 结论

状态：官方 k6 可执行回环验证通过；真实四服务性能验收待执行。

本记录证明签名模块、k6 主脚本语法、PowerShell 运行器与脱敏结果契约可交付。它没有运行 PostgreSQL、Valkey、Open WebUI、Bifrost 联合环境，因此不包含也不代表生产请求率、P95 或容量结论。

## 验证环境

| 项目 | 结果 |
| --- | --- |
| 日期 | 2026-08-08 |
| 基线 commit | `e44198c` |
| Node.js | `v20.19.5` |
| Python | 项目 `.venv` Python 3.12 |
| 官方最新 k6 Release 查询 | `v2.1.0`，发布于 2026-06-30 |
| k6 获取方式 | WinGet exact v2.1.0 离线下载，管理提取到 Git 忽略的 `.tmp`，未安装到系统 |
| MSI SHA-256 | `3d3650a88a0a5d0027371071c27fe248e17ef0bc3a220b9d99055cb06bd8a86f`，匹配 WinGet manifest / GitHub Release checksums |
| k6.exe | `v2.1.0 (commit/83a87a41e2, go1.26.4, windows/amd64)` |
| k6.exe SHA-256 | `51ac387205675f70ff52bb6decd3416650433b9741a0c2765b1be574535f0087` |
| Authenticode | MSI 与 EXE 均为 `Valid`，签名者 `Grafana Labs` |

## 已执行证据

| 门禁 | 结果 |
| --- | --- |
| k6 交付聚焦测试 | `4 passed` |
| 固定 HMAC v1 向量 | Node 执行 `load/k6/signing.js`，得到 `121118be99e3276c168066f7a10b12cd4d395a13ecbb843dbb545363595decfe` |
| k6 主脚本语法 | Node ES module parser 通过 |
| PowerShell 运行器语法 | PowerShell AST parser 通过 |
| 未确认聊天费用保护 | PowerShell 实际运行在解析 k6 前拒绝，输出不含 Secret |
| Python lint | `backend/tests/test_k6_delivery.py` Ruff 通过 |
| `k6 inspect` | smoke 场景、10 iterations、1 VU、聚合阈值与 `discardResponseBodies=true` 均正确解析 |
| 完整 k6 HTTP 回环 | 官方 v2.1.0 实际发送 3 次请求；HMAC 全部有效，nonce/request ID 全部唯一，脱敏 JSON/text 汇总通过 |

## 待部署验收

状态：待执行。

目标环境应使用 [结果模板](../result-template.md) 记录 `models` 和经费用确认的 `chat` 档位，至少包含请求率、P50/P90/P95/P99、失败率、4xx/5xx、dropped iterations、服务端 request ID 完整性、版本与聚合结果 SHA-256。Secret、签名、nonce、提示词、回答和原始错误响应不得进入报告。
