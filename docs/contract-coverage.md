# 合同功能覆盖矩阵

更新时间：2026-08-10
权威范围：`openwebui-commercial-fork/docs/plans/2026-07-13-contract-delivery-master-plan.md`。该文件说明其内容源自 2026-06-23 合同；签署版 DOCX/PDF 当前不在工作区，因此本矩阵不替代正式合同原件或最终验收签字。

## 状态定义

- **已实现**：当前代码已经覆盖合同语义，但本轮证据不足以称为完整交付。
- **已验证**：有当前代码、测试或运行证据，且范围与证据边界一致。
- **待合并**：能力存在于独立 Open WebUI worktree/提交，尚未进入当前商业基线或未完成跨仓库发布。
- **外部阻塞**：本地代码和门禁已准备，但需要 PostgreSQL/Valkey/Open WebUI/Bifrost、甲方模型/GPU/语音账号或客户环境。
- **缺失**：当前没有可供合同验收使用的实现或操作入口。

## 当前矩阵

| 合同能力 | 当前状态 | 代码/提交证据 | 本地证据边界 | 下一步 |
| --- | --- | --- | --- | --- |
| Web 用户端：中文/藏文聊天、历史和异常 | 已实现（商业基线） | `openwebui-commercial-fork` 商业分支及其既有 Web/PC 变更 | 本控制面仓库不重新证明 Open WebUI 全量用户流程 | 以商业分支做本轮截图审查，补齐合同首页、个人中心和最终 smoke |
| Windows 客户端 | 已实现（基础）/外部阻塞（交付） | 商业基线已有 Electron 壳、构建和冒烟记录 | 未在本轮重新验证签名安装包和客户环境 | 签名安装包、安装/升级/卸载及客户机验收 |
| 藏文输入与语音 | 部分实现/缺失 | 商业基线有语言策略、藏文输入和通用 STT 入口 | 没有甲方语音服务的真实识别与计费证据 | 接入供应商、失败回填、语音积分扣减和真实账号 smoke |
| 后台登录和基础管理 | 已验证 API/缺失 UI | 独立管理员会话、CSRF 和 caller 管理 API 已实现；`web/src/routes/+layout.svelte` 仍只有总览 | 后端管理闭环通过本地测试；暂无可操作管理页和截图审查证据 | 取得截图权限后审查现有壳，再接入登录、调用方、凭据和配额页面 |
| 用户/角色/分类/标签/条目 CRUD | 部分实现（Open WebUI）/缺失（控制面） | 商业基线已有用户、组权限、知识区基础 | 合同角色语义、基础 CRUD、导入导出尚无逐项验收证据 | 以合同字段和角色语义写 acceptance；保持复杂 RAG 在合同外 |
| 用户积分账户、流水、问答扣减/结算/退款 | 待合并 | Open WebUI `b66102df9`；控制面客户端 `7735508` | 独立测试和本地 mock 通过；当前商业基线需确认合并关系 | 合并/发布两仓提交，做真实 Open WebUI + PostgreSQL 回归 |
| 外部 API 调用方、Key/Secret、启停、轮换、撤销 | 已验证 API/缺失 UI | 管理员 caller create/list/detail/update、一次性 Secret、rotation、revoke、disable、审计和 Bifrost outbox 已实现 | HTTP/服务/配置契约和全量回归通过；尚无 UI 和真实 Bifrost worker 部署证据 | 经视觉审查后接入管理页；部署环境运行 outbox 和远端状态验收 |
| 签名、时间戳、nonce、防重放 | 已验证 | `843dcf9`、`2dc7bee` 及后续全量回归 | 固定向量、异常边界、nonce/QPS fakeredis、signed HTTP 通过 | 保持协议冻结；管理 API 使用独立会话，不复用 caller HMAC |
| QPS、并发、每日/总请求和 Token 配额 | 已验证 API/缺失 UI | `bdef47e`、`c910df1`、`dca1b13`；管理员版本化 quota update | 运行时、k6 loopback、显式 null 和版本冲突通过；无管理员配置页 | 在管理页提供限制值、无限制状态、校验和保存反馈 |
| 调用方模型/端点权限隔离 | 已验证 API/缺失 UI | `ApiClient.allowed_models/allowed_endpoints`、metadata/chat policy、管理员版本化 policy update | caller isolation、signed `/models`/`/usage` 和管理更新通过；无管理页 | 管理页提供端点/模型多选，并提示当前 Bifrost 单初始模型限制 |
| JSON/SSE REST API、统一错误、版本化 | 已验证 | `c910df1`、`dca1b13`、`2dc7bee` | backend `155 passed` 基线、signed HTTP 和 SSE 合同通过 | 生成/补齐面向客户的 OpenAPI/API 文档和部署 smoke |
| Python SDK、PowerShell/cURL、示例 | 已验证（范围有限） | `e44198c`、`docs/api-sdk.md`、`examples/` | SDK/cURL focused `11/11`，wheel import 和 loopback 签名通过 | 补 JavaScript SDK/示例与 CI smoke；不把正式长期 SDK 维护外推为已交付 |
| API 调用记录、异常、统计和监控趋势 | 已验证 API/缺失 UI | `ApiCallEvent`、`AdminObservabilityService`、`RetentionService`、管理员 events/summary/export/retention 路由及不可变审计 | 筛选分页、精确 P50/P95/P99、UTC 趋势、受限 CSV、固定策略预览和有界清理已通过服务/HTTP/迁移合同；无管理页面，真实 PostgreSQL 数据量、调度、vacuum 和备份联动未验证 | 经截图审查后接入记录/趋势/保留期页；部署环境验证索引计划、批处理调度和恢复流程 |
| k6 基础压测脚本和结果记录 | 已验证工具/外部阻塞容量结论 | `4011e62`、`f6df0e4`、`docs/load-testing.md` | 官方 k6 v2.1.0 loopback `4/4`；三次真实签名请求通过 | 在部署环境跑 PostgreSQL/Valkey/Open WebUI/Bifrost smoke/steady/burst，形成 P50/P95/P99 与容量结论 |
| 模型部署、GPU、真实 Provider | 外部阻塞 | Bifrost typed client/preflight、内部模型路由 | loopback Bifrost v1.6.3 PoC 通过；无甲方 GPU/模型部署证据 | 甲方提供资源后做部署、超时、真实模型和容量验收 |
| 真实 Open WebUI/ PostgreSQL/ Valkey 集成 | 外部阻塞 | Compose 拓扑和 lifespan ownership 已实现 | Docker CLI 不可用；本地 SQLite/fakeredis/mock 不替代部署证据 | 在部署环境执行迁移、跨服务 chat/credit、故障恢复和性能门禁 |
| 备份、恢复、迁移演练 | 缺失/外部阻塞 | Alembic 单 head、SQLite round-trip 和离线 PostgreSQL SQL | 没有目标 PostgreSQL 备份恢复和客户数据演练 | 增加恢复脚本、RPO/RTO、迁移回滚和演练记录 |
| 部署/API/测试/压测/管理员/用户手册 | 部分实现 | `README.md`、`docs/architecture.md`、`docs/api-sdk.md`、`docs/load-testing.md` | 控制面边界文档已存在；合同交付文档清单未闭合 | 补部署、API、测试报告、管理员/用户手册、故障排查和版本哈希 |
| 软著基础材料和最终验收包 | 缺失 | 暂无可验收目录 | 尚无 12 条验收逐项证据、签名包和客户签收 | 最终阶段整理构建哈希、截图/JSON、报告、配置清单和已知限制 |

## 结论与执行顺序

当前不能宣称“合同核心功能完成”或“API 与压测完成”。底层受控 API、签名安全、配额控制、SDK/cURL 和官方 k6 loopback 已达到可复核状态；最直接的本地产品缺口是管理员调用方管理入口。真实四服务容量、模型/GPU、语音、签名 Windows 交付、恢复演练和验收文档属于外部或最终交付门禁。

本地下一步固定为：

1. 在取得当前运行截图后完成 Web 壳的 UX/视觉/截图可见性审查；当前不能用旧截图替代本轮证据。
2. 管理员会话和调用方生命周期后端已完成；继续补管理 Web 的登录、调用方、凭据、权限和配额工作流。
3. 将 Web 的“调用方/凭据/配额”导航接到真实接口；桌面端和移动窄屏做同一套渲染验收。
4. 调用记录/趋势/导出后端切片已完成；管理 Web 经视觉审查接入后，转入部署环境的真实集成、压测和最终交付包。
