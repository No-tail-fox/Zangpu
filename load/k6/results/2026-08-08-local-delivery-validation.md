# k6 本地交付验证结果

## 结论

状态：脚本交付验证通过；真实四服务性能验收待执行。

本记录证明签名模块、k6 主脚本语法、PowerShell 运行器与脱敏结果契约可交付。它没有运行 PostgreSQL、Valkey、Open WebUI、Bifrost 联合环境，因此不包含也不代表生产请求率、P95 或容量结论。

## 验证环境

| 项目 | 结果 |
| --- | --- |
| 日期 | 2026-08-08 |
| 基线 commit | `e44198c` |
| Node.js | `v20.19.5` |
| Python | 项目 `.venv` Python 3.12 |
| 官方最新 k6 Release 查询 | `v2.1.0`，发布于 2026-06-30 |
| k6 二进制 | 未安装；官方 30,613,429 字节压缩包下载两次超时/连接重置，未执行不可信替代文件 |

## 已执行证据

| 门禁 | 结果 |
| --- | --- |
| k6 交付聚焦测试 | `3 passed, 1 skipped` |
| 固定 HMAC v1 向量 | Node 执行 `load/k6/signing.js`，得到 `121118be99e3276c168066f7a10b12cd4d395a13ecbb843dbb545363595decfe` |
| k6 主脚本语法 | Node ES module parser 通过 |
| PowerShell 运行器语法 | PowerShell AST parser 通过 |
| 未确认聊天费用保护 | PowerShell 实际运行在解析 k6 前拒绝，输出不含 Secret |
| Python lint | `backend/tests/test_k6_delivery.py` Ruff 通过 |
| 完整 k6 HTTP 回环 | 跳过，原因仅为 k6 可执行文件不可用 |

## 待部署验收

状态：待执行。

目标环境应使用 [结果模板](../result-template.md) 记录 `models` 和经费用确认的 `chat` 档位，至少包含请求率、P50/P90/P95/P99、失败率、4xx/5xx、dropped iterations、服务端 request ID 完整性、版本与聚合结果 SHA-256。Secret、签名、nonce、提示词、回答和原始错误响应不得进入报告。
