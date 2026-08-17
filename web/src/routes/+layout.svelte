<script lang="ts">
  import {
    ChartNoAxesCombined,
    Gauge,
    KeyRound,
    LoaderCircle,
    LogOut,
    ShieldCheck,
    UserRound,
    UsersRound,
  } from "@lucide/svelte";
  import { setContext } from "svelte";
  import { writable } from "svelte/store";

  import type { AdminSession } from "$lib/admin-api";
  import { ADMIN_UI_CONTEXT, type AdminSection, type AdminUiContext } from "$lib/admin-context";
  import "../app.css";

  let { children } = $props();

  const navigation = [
    { id: "overview", label: "总览", icon: ChartNoAxesCombined },
    { id: "callers", label: "调用方", icon: UsersRound },
    { id: "credentials", label: "凭据", icon: KeyRound },
    { id: "policy", label: "权限与配额", icon: Gauge },
  ];

  const activeSection = writable<AdminSection>("overview");
  const session = writable<AdminSession | null>(null);
  const sessionPhase = writable<"checking" | "signed_out" | "ready">("checking");
  const logoutRequested = writable(0);

  setContext<AdminUiContext>(ADMIN_UI_CONTEXT, {
    activeSection,
    session,
    sessionPhase,
    logoutRequested,
  });

  function openSection(section: AdminSection) {
    if ($sessionPhase === "ready") activeSection.set(section);
  }

  function formatExpiry(expiresAt: number) {
    return new Date(expiresAt * 1000).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
</script>

<svelte:head>
  <title>藏普 API 控制台</title>
  <meta name="description" content="藏普外部 API 调用方与运行状态管理" />
  <link rel="icon" href="/favicon.svg" />
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
        <button
          class:active={$activeSection === item.id}
          class="nav-item"
          type="button"
          disabled={$sessionPhase !== "ready"}
          aria-current={$activeSection === item.id ? "page" : undefined}
          onclick={() => openSection(item.id as AdminSection)}
        >
          <Icon size={17} strokeWidth={2} />
          <span>{item.label}</span>
        </button>
      {/each}
    </nav>

    <div class="sidebar-footer">
      <span class="environment-dot" aria-hidden="true"></span>
      <span>本地验证环境</span>
    </div>
  </aside>

  <div class="workspace">
    <header class="topbar">
      {#if $sessionPhase === "checking"}
        <div class="topbar-status"><LoaderCircle class="spin" size={14} /><span>正在验证会话</span></div>
      {:else if $session}
        <div class="topbar-session">
          <span class="topbar-user"><UserRound size={14} /><strong>管理员</strong></span>
          <span class="topbar-expiry">会话至 {formatExpiry($session.expires_at)}</span>
          <button
            class="icon-button"
            type="button"
            title="退出登录"
            aria-label="退出登录"
            onclick={() => logoutRequested.update((value) => value + 1)}
          >
            <LogOut size={16} />
          </button>
        </div>
      {:else}
        <div class="topbar-status"><span>管理员会话</span><strong>未登录</strong></div>
      {/if}
    </header>
    <main class="workspace-content">{@render children()}</main>
  </div>
</div>
