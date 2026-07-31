<script lang="ts">
  import { Activity, Box, Coins, Database, RefreshCw, Route, ServerCog, ShieldCheck, Waypoints } from "@lucide/svelte";

  let lastCheckedAt = $state<Date | null>(null);

  const metrics = [
    { label: "今日请求", note: "等待事件数据", icon: Activity },
    { label: "成功率", note: "等待终态统计", icon: ShieldCheck },
    { label: "Token 用量", note: "等待结算数据", icon: Waypoints },
    { label: "积分消耗", note: "以 Open WebUI 账本为准", icon: Coins },
  ];

  const services = [
    { name: "Control API", role: "调用方认证与配额", icon: ServerCog },
    { name: "Bifrost", role: "内部模型网关", icon: Route },
    { name: "PostgreSQL", role: "调用与事件真值", icon: Database },
    { name: "Redis / Valkey", role: "nonce、QPS 与并发", icon: Box },
    { name: "Open WebUI", role: "积分账本真值", icon: Coins },
  ];

  const boundaries = [
    { title: "公开边界", copy: "仅 caller API 与管理站反向代理端口可发布。", icon: Route },
    { title: "凭据边界", copy: "调用方 Secret 与 Bifrost 虚拟密钥相互独立。", icon: ShieldCheck },
    { title: "积分边界", copy: "控制平面不直接写入积分表，也不建立第二套余额。", icon: Coins },
  ];

  function refreshStatus() {
    lastCheckedAt = new Date();
  }
</script>

<section id="overview" class="page">
  <div class="page-header">
    <div>
      <h1>总览</h1>
      <p>调用方、配额和内部依赖的运行概况。数据接入前仅显示配置状态，不生成模拟业务指标。</p>
    </div>
    <button class="refresh-button" type="button" onclick={refreshStatus}>
      <RefreshCw size={15} strokeWidth={2.2} />
      刷新状态
    </button>
  </div>

  <div class="metrics-band" aria-label="关键指标">
    {#each metrics as metric}
      {@const Icon = metric.icon}
      <div class="metric">
        <span class="metric-label"><Icon size={15} strokeWidth={2} />{metric.label}</span>
        <strong class="metric-value">--</strong>
        <span class="metric-note">{metric.note}</span>
      </div>
    {/each}
  </div>

  <div class="dashboard-grid">
    <section class="section-block" aria-labelledby="health-heading">
      <div class="section-heading">
        <h2 id="health-heading">系统状态</h2>
        <span class="section-meta"
          >{lastCheckedAt ? `检查于 ${lastCheckedAt.toLocaleTimeString("zh-CN")}` : "尚未检查"}</span
        >
      </div>
      <table class="health-table">
        <thead>
          <tr><th>组件</th><th>职责</th><th>状态</th></tr>
        </thead>
        <tbody>
          {#each services as service}
            {@const Icon = service.icon}
            <tr>
              <td><span class="service-name"><Icon size={15} strokeWidth={2} />{service.name}</span></td>
              <td>{service.role}</td>
              <td><span class="state-text"><span class="state-dot"></span>待配置</span></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>

    <section class="section-block" aria-labelledby="boundary-heading">
      <div class="section-heading">
        <h2 id="boundary-heading">安全边界</h2>
        <span class="section-meta">Task 0</span>
      </div>
      <ul class="boundary-list">
        {#each boundaries as boundary}
          {@const Icon = boundary.icon}
          <li>
            <span class="boundary-icon"><Icon size={14} strokeWidth={2.1} /></span>
            <span>
              <span class="boundary-title">{boundary.title}</span>
              <span class="boundary-copy">{boundary.copy}</span>
            </span>
          </li>
        {/each}
      </ul>
    </section>
  </div>
</section>
