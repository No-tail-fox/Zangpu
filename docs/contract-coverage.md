# 合同功能覆盖矩阵

更新时间：2026-08-18
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
| 后台登录和基础管理 | 已验证（API + 本地 Web） | 独立管理员会话、CSRF、typed Web client、中文 caller 管理和实时并发界面 | Web `6/6`、Svelte 零诊断、生产构建及桌面/移动并发状态截图通过；浏览器使用 API mock，不替代真实网关部署 | 在部署环境通过管理站端口验证 Cookie/CSRF、真实数据库、Valkey 和反向代理 |
| 用户/角色/分类/标签/条目 CRUD | 部分实现（Open WebUI）/缺失（控制面） | 商业基线已有用户、组权限、知识区基础 | 合同角色语义、基础 CRUD、导入导出尚无逐项验收证据 | 以合同字段和角色语义写 acceptance；保持复杂 RAG 在合同外 |
| 用户积分账户、流水、问答扣减/结算/退款 | 待合并 | Open WebUI `b66102df9`；控制面客户端 `7735508` | 独立测试和本地 mock 通过；当前商业基线需确认合并关系 | 合并/发布两仓提交，做真实 Open WebUI + PostgreSQL 回归 |
| 外部 API 调用方、Key/Secret、启停、轮换、撤销 | 已验证（API + 本地 Web） | caller create/list/detail/update、一次性 Secret、rotation、revoke、disable、审计、Bifrost outbox 和中文管理流程 | 完整 mock-backed Web 生命周期与真实后端合同分别通过；尚无真实 Bifrost worker 部署证据 | 部署环境运行 outbox，验证 pending/active/error/disabled 远端状态闭环 |
| 签名、时间戳、nonce、防重放 | 已验证 | `843dcf9`、`2dc7bee` 及后续全量回归 | 固定向量、异常边界、nonce/QPS fakeredis、signed HTTP 通过 | 保持协议冻结；管理 API 使用独立会话，不复用 caller HMAC |
| QPS、并发、每日/总请求和 Token 配额 | 已验证（API + 本地 Web） | Redis 原子租约、模型池 FIFO/有界队列、非流式/流式绝对心跳、精确 owner 释放、管理员调用方与模型容量观察、版本化 quota update | 32 抢 3 无超发；失去租约会取消在途请求；官方 k6 限制 2/尝试 5 得到 2 个 200、3 个 429；浏览器覆盖调用方/模型容量可用、排队和 Valkey 不可用 | 在真实 PostgreSQL/Valkey/Open WebUI/Bifrost 环境复跑并形成容量结论 |
| 调用方模型/端点权限隔离 | 已验证（API + 本地 Web） | `ApiClient.allowed_models/allowed_endpoints`、metadata/chat policy、版本化 policy update 和权限复选界面 | caller isolation、signed `/models`/`/usage`、管理更新及浏览器操作通过；绑定提供方/初始模型仍只读 | 部署环境验证 Bifrost 单绑定同步及真实模型可见性 |
| JSON/SSE REST API、统一错误、版本化 | 已验证 | `c910df1`、`dca1b13`、`2dc7bee` | backend `155 passed` 基线、signed HTTP 和 SSE 合同通过 | 生成/补齐面向客户的 OpenAPI/API 文档和部署 smoke |
| Python/JavaScript SDK、PowerShell/cURL、示例 | 已验证（范围有限） | `e44198c`、`sdk/python`、`sdk/javascript`、`docs/api-sdk.md`、`examples/` | Python SDK/cURL 已验证；Node 20+ SDK `11/11`、类型声明、7 文件包内容和脱敏部署 smoke 合同通过，真实远端 smoke 未运行 | 在部署环境执行 health/models/usage smoke；不把正式长期 SDK 维护外推为已交付 |
| API 调用记录、异常、统计和监控趋势 | 已验证（API + 本地 Web） | `ApiCallEvent`、`AdminObservabilityService`、管理员 events/summary/export 路由、typed Web client 和中文调用记录页 | 筛选分页、精确 P50/P95/P99、UTC 趋势和 CSRF 受限 CSV 已通过服务/HTTP/Web/桌面移动截图；页面不展示请求正文；真实 PostgreSQL 数据量与索引计划未验证 | 部署环境验证真实事件量、查询计划和导出上限；保留期维护页仍待接入 |
| k6 基础压测脚本和结果记录 | 已验证工具/外部阻塞容量结论 | `load/k6/signed-api.js`、`run.ps1`、`docs/load-testing.md` | 官方 k6 v2.1.0 delivery `6/6`；metadata 签名和精确并发 200/429 回环通过 | 在部署环境跑 smoke/steady/burst/concurrency，形成 P50/P95/P99 与容量结论 |
| 模型部署、GPU、真实 Provider | 外部阻塞 | Bifrost typed client/preflight、内部模型路由 | loopback Bifrost v1.6.3 PoC 通过；无甲方 GPU/模型部署证据 | 甲方提供资源后做部署、超时、真实模型和容量验收 |
| 真实 Open WebUI/ PostgreSQL/ Valkey 集成 | 外部阻塞 | Compose 拓扑和 lifespan ownership 已实现 | Docker CLI 不可用；本地 SQLite/fakeredis/mock 不替代部署证据 | 在部署环境执行迁移、跨服务 chat/credit、故障恢复和性能门禁 |
| 备份、恢复、迁移演练 | 缺失/外部阻塞 | Alembic 单 head、SQLite round-trip 和离线 PostgreSQL SQL | 没有目标 PostgreSQL 备份恢复和客户数据演练 | 增加恢复脚本、RPO/RTO、迁移回滚和演练记录 |
| 部署/API/测试/压测/管理员/用户手册 | 部分实现 | `README.md`、`docs/architecture.md`、`docs/api-sdk.md`、`docs/load-testing.md` | 控制面边界文档已存在；合同交付文档清单未闭合 | 补部署、API、测试报告、管理员/用户手册、故障排查和版本哈希 |
| 软著基础材料和最终验收包 | 缺失 | 暂无可验收目录 | 尚无 12 条验收逐项证据、签名包和客户签收 | 最终阶段整理构建哈希、截图/JSON、报告、配置清单和已知限制 |

## 结论与执行顺序

当前仍不能宣称“合同核心功能完成”或“真实部署压测完成”。底层受控 API、签名安全、并发/配额控制、管理员调用方管理、调用记录/趋势 Web、SDK/cURL 和官方 k6 loopback 已达到可复核状态；真实四服务容量、模型/GPU、语音、签名 Windows 交付、保留期 Web、恢复演练和最终验收文档仍是外部或后续交付门禁。

本地下一步固定为：

1. 在部署环境通过管理站端口验证真实 Cookie/CSRF、PostgreSQL caller 数据、Valkey 实时占用和反向代理。
2. 使用专用低额度 caller 运行真实 `concurrency` 档，归档脱敏 JSON、版本哈希和 P50/P95/P99；本地回环不能替代该结论。
3. 将已完成的保留期预览/确认后端接入管理 Web，并做同级桌面/移动视觉验收。
4. 继续推进 Open WebUI 积分分支合并、真实 Bifrost outbox、GPU/模型、语音、恢复演练和最终交付包。
