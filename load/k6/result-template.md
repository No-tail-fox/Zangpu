# k6 压测结果记录

## 结论

状态：待执行

本报告只有在目标部署的 PostgreSQL、Valkey、Open WebUI、Bifrost 与控制平面均使用验收版本时，才能标记为通过。回环签名测试或单服务测试不得替代真实部署结论。

## 环境

| 项目 | 记录 |
| --- | --- |
| 执行时间（UTC） | 待填写 |
| 控制平面 commit | 待填写 |
| Open WebUI commit | 待填写 |
| Bifrost 版本与镜像摘要 | 待填写 |
| PostgreSQL / Valkey 版本 | 待填写 |
| k6 版本与二进制 SHA-256 | 待填写 |
| 测试目标 | `models` / `usage` / `chat` |
| Profile | `smoke` / `steady` / `burst` |
| 调用方 QPS / 并发 / Token 限额 | 待填写 |

## 配置

| 指标 | 验收值 |
| --- | ---: |
| 请求率与时长 | 待填写 |
| VU 配置 | 待填写 |
| 允许失败率 | 待填写 |
| P95 上限（ms） | 待填写 |
| 聊天模型与 max tokens | 不适用或待填写 |

## 聚合结果

| 指标 | 实测值 | 通过 |
| --- | ---: | --- |
| iterations | 待填写 | 待判断 |
| requests / second | 待填写 | 待判断 |
| API success rate | 待填写 | 待判断 |
| HTTP failure rate | 待填写 | 待判断 |
| duration P50 / P90 / P95 / P99 | 待填写 | 待判断 |
| HTTP 4xx / 5xx | 待填写 | 待判断 |
| dropped iterations | 待填写 | 待判断 |
| 缺失服务端 request ID | 待填写 | 待判断 |

## 证据

- 脱敏 JSON 汇总路径和 SHA-256：待填写
- 纯文本汇总路径和 SHA-256：待填写
- 监控时间窗或仪表盘引用：待填写
- 失败样本的服务端 request ID：待填写，不记录签名、nonce、请求体或响应体

## 备注

记录限流、额度、模型超时、依赖异常、资源瓶颈及复测动作。不得粘贴 Secret、签名、nonce、提示词、回答或原始错误响应。
