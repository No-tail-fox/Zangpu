<script lang="ts">
  import { Activity, ChartNoAxesCombined, Gauge, KeyRound, ScrollText, ShieldCheck, UsersRound } from "@lucide/svelte";
  import "../app.css";

  let { children } = $props();

  const navigation = [
    { label: "总览", icon: ChartNoAxesCombined, active: true },
    { label: "调用方", icon: UsersRound, active: false },
    { label: "凭据", icon: KeyRound, active: false },
    { label: "配额", icon: Gauge, active: false },
    { label: "调用记录", icon: ScrollText, active: false },
    { label: "系统状态", icon: Activity, active: false },
  ];
</script>

<svelte:head>
  <title>藏普 API 控制台</title>
  <meta name="description" content="藏普外部 API 调用方与运行状态管理" />
</svelte:head>

<div class="app-shell">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true"><ShieldCheck size={19} strokeWidth={2.2} /></div>
      <div class="brand-copy">
        <p class="brand-title">藏普 API 控制台</p>
        <p class="brand-version">Control Plane 0.1.0</p>
      </div>
    </div>

    <nav class="primary-nav" aria-label="主导航">
      {#each navigation as item}
        {@const Icon = item.icon}
        {#if item.active}
          <a class="nav-item active" href="#overview" aria-current="page">
            <Icon size={17} strokeWidth={2} />
            <span>{item.label}</span>
          </a>
        {:else}
          <span class="nav-item disabled" aria-disabled="true">
            <Icon size={17} strokeWidth={2} />
            <span>{item.label}</span>
            <span class="nav-state">待接入</span>
          </span>
        {/if}
      {/each}
    </nav>

    <div class="sidebar-footer">
      <span class="environment-dot" aria-hidden="true"></span>
      <span>本地验证环境</span>
    </div>
  </aside>

  <div class="workspace">
    <header class="topbar">
      <div class="topbar-status"><span>配置状态</span><strong>等待服务连接</strong></div>
    </header>
    <main class="workspace-content">{@render children()}</main>
  </div>
</div>
