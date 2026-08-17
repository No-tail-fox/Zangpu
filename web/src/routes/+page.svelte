<script lang="ts">
  import {
    Activity,
    Check,
    CheckCircle2,
    Clipboard,
    Copy,
    Gauge,
    KeyRound,
    LoaderCircle,
    Plus,
    RefreshCw,
    RotateCcw,
    Save,
    Search,
    ShieldCheck,
    Trash2,
    UserRound,
    UsersRound,
    X,
  } from "@lucide/svelte";
  import { getContext, onMount } from "svelte";

  import {
    ADMIN_ENDPOINT_PERMISSIONS,
    AdminApiClient,
    AdminApiError,
    type AdminCallerCreateInput,
    type AdminCallerPatchInput,
    type AdminEndpointPermission,
    type CallerDetail,
    type CallerSummary,
    type CredentialSummary,
  } from "$lib/admin-api";
  import { ADMIN_UI_CONTEXT, type AdminSection, type AdminUiContext } from "$lib/admin-context";

  type ConfirmKind = "rotate" | "revoke" | "disable";

  interface CreateForm {
    name: string;
    description: string;
    serviceUserId: string;
    provider: string;
    model: string;
    allowedEndpoints: string[];
    allowedModels: string;
    groupIds: string;
    qpsLimit: number;
    concurrencyLimit: number;
    dailyRequestLimit: string;
    dailyTokenLimit: string;
    totalRequestLimit: string;
    totalTokenLimit: string;
    maxOutputTokens: number;
  }

  interface PolicyForm {
    name: string;
    description: string;
    allowedEndpoints: string[];
    allowedModels: string;
    groupIds: string;
    qpsLimit: number;
    concurrencyLimit: number;
    dailyRequestLimit: string;
    dailyTokenLimit: string;
    totalRequestLimit: string;
    totalTokenLimit: string;
    maxOutputTokens: number;
  }

  interface Confirmation {
    kind: ConfirmKind;
    title: string;
    body: string;
    confirmLabel: string;
    credentialId?: string;
  }

  interface OneTimeSecret {
    secret: string;
    keyId: string;
    callerName: string;
  }

  const { activeSection, session, sessionPhase, logoutRequested } = getContext<AdminUiContext>(ADMIN_UI_CONTEXT);
  const api = new AdminApiClient();

  const endpointLabels: Record<AdminEndpointPermission, { title: string; detail: string }> = {
    "chat.completions": { title: "对话生成", detail: "POST /v1/chat/completions" },
    "models.read": { title: "模型列表", detail: "GET /v1/models" },
    "usage.read": { title: "用量查询", detail: "GET /v1/usage" },
    "health.read": { title: "健康检查", detail: "GET /health" },
  };

  let loginToken = $state("");
  let loginPending = $state(false);
  let loadingCallers = $state(false);
  let loadingDetail = $state(false);
  let actionPending = $state<string | null>(null);
  let uiError = $state<string | null>(null);
  let callers = $state<CallerSummary[]>([]);
  let selectedCallerId = $state<string | null>(null);
  let selectedDetail = $state<CallerDetail | null>(null);
  let searchQuery = $state("");
  let createOpen = $state(false);
  let createForm = $state<CreateForm>(newCreateForm());
  let policyForm = $state<PolicyForm | null>(null);
  let confirmation = $state<Confirmation | null>(null);
  let oneTimeSecret = $state<OneTimeSecret | null>(null);
  let secretAcknowledged = $state(false);
  let copyState = $state<"idle" | "copied" | "failed">("idle");
  let handledLogoutRequest = 0;

  onMount(() => {
    void restoreSession();
  });

  $effect(() => {
    if ($logoutRequested > handledLogoutRequest) {
      handledLogoutRequest = $logoutRequested;
      void signOut();
    }
  });

  function newCreateForm(): CreateForm {
    return {
      name: "",
      description: "",
      serviceUserId: "",
      provider: "openai-compatible",
      model: "",
      allowedEndpoints: ["chat.completions", "models.read"],
      allowedModels: "",
      groupIds: "",
      qpsLimit: 10,
      concurrencyLimit: 2,
      dailyRequestLimit: "",
      dailyTokenLimit: "",
      totalRequestLimit: "",
      totalTokenLimit: "",
      maxOutputTokens: 4096,
    };
  }

  function filteredCallers(): CallerSummary[] {
    const query = searchQuery.trim().toLocaleLowerCase("zh-CN");
    if (!query) return callers;
    return callers.filter(
      (caller) =>
        caller.name.toLocaleLowerCase("zh-CN").includes(query) ||
        caller.id.toLocaleLowerCase("zh-CN").includes(query) ||
        caller.allowed_models.some((model) => model.toLocaleLowerCase("zh-CN").includes(query)),
    );
  }

  function activeCallerCount(): number {
    return callers.filter((caller) => caller.status === "active").length;
  }

  function disabledCallerCount(): number {
    return callers.filter((caller) => caller.status === "disabled").length;
  }

  function limitedCallerCount(): number {
    return callers.filter(
      (caller) =>
        caller.daily_request_limit !== null ||
        caller.daily_token_limit !== null ||
        caller.total_request_limit !== null ||
        caller.total_token_limit !== null,
    ).length;
  }

  async function restoreSession() {
    $sessionPhase = "checking";
    uiError = null;
    try {
      const restored = await api.restoreSession();
      if (!restored) {
        $session = null;
        $sessionPhase = "signed_out";
        return;
      }
      $session = restored;
      $sessionPhase = "ready";
      await loadCallers();
    } catch (error) {
      $session = null;
      $sessionPhase = "signed_out";
      reportError(error);
    }
  }

  async function signIn(event: SubmitEvent) {
    event.preventDefault();
    const token = loginToken.trim();
    if (!token) return;
    loginPending = true;
    uiError = null;
    try {
      const loggedIn = await api.login(token);
      loginToken = "";
      $session = loggedIn;
      $sessionPhase = "ready";
      await loadCallers();
    } catch (error) {
      reportError(error);
    } finally {
      loginPending = false;
    }
  }

  async function signOut() {
    if ($sessionPhase !== "ready") return;
    actionPending = "logout";
    uiError = null;
    try {
      await api.logout();
    } catch (error) {
      if (!(error instanceof AdminApiError && error.status === 401)) {
        reportError(error);
        actionPending = null;
        return;
      }
    }
    callers = [];
    selectedCallerId = null;
    selectedDetail = null;
    policyForm = null;
    oneTimeSecret = null;
    $session = null;
    $sessionPhase = "signed_out";
    $activeSection = "overview";
    actionPending = null;
  }

  function validateCreatePayload(payload: AdminCallerCreateInput): boolean {
    if (payload.allowed_endpoints.length === 0) {
      uiError = "至少选择一个接口权限。";
      return false;
    }
    if (payload.allowed_models.length === 0) {
      uiError = "至少填写一个允许模型。";
      return false;
    }
    return true;
  }

  async function loadCallers() {
    loadingCallers = true;
    uiError = null;
    try {
      const result = await api.listCallers();
      callers = result.items;
      if (selectedCallerId && !callers.some((caller) => caller.id === selectedCallerId)) {
        selectedCallerId = null;
        selectedDetail = null;
        policyForm = null;
      }
    } catch (error) {
      reportError(error);
    } finally {
      loadingCallers = false;
    }
  }

  async function selectCaller(callerId: string, section?: AdminSection) {
    if (section) $activeSection = section;
    selectedCallerId = callerId;
    loadingDetail = true;
    uiError = null;
    try {
      const detail = await api.getCaller(callerId);
      selectedDetail = detail;
      policyForm = policyFromCaller(detail.client);
    } catch (error) {
      reportError(error);
    } finally {
      loadingDetail = false;
    }
  }

  function clearSelectedCaller() {
    selectedCallerId = null;
    selectedDetail = null;
    policyForm = null;
  }

  function openCreateDialog() {
    createForm = newCreateForm();
    createOpen = true;
    uiError = null;
  }

  async function createCaller(event: SubmitEvent) {
    event.preventDefault();
    const models = parseCsv(createForm.allowedModels);
    const bindingModel = createForm.model.trim();
    if (!models.includes(bindingModel)) models.unshift(bindingModel);
    const payload: AdminCallerCreateInput = {
      name: createForm.name.trim(),
      description: createForm.description.trim() || null,
      service_user_id: createForm.serviceUserId.trim(),
      provider: createForm.provider.trim(),
      model: bindingModel,
      allowed_endpoints: createForm.allowedEndpoints,
      allowed_models: models,
      group_ids: parseCsv(createForm.groupIds),
      qps_limit: createForm.qpsLimit,
      concurrency_limit: createForm.concurrencyLimit,
      daily_request_limit: nullableNumber(createForm.dailyRequestLimit),
      daily_token_limit: nullableNumber(createForm.dailyTokenLimit),
      total_request_limit: nullableNumber(createForm.totalRequestLimit),
      total_token_limit: nullableNumber(createForm.totalTokenLimit),
      max_output_tokens_per_request: createForm.maxOutputTokens,
    };
    if (!validateCreatePayload(payload)) return;
    actionPending = "create";
    uiError = null;
    try {
      const created = await api.createCaller(payload, makeIdempotencyKey("create"));
      createOpen = false;
      oneTimeSecret = {
        secret: created.secret,
        keyId: created.credential.key_id,
        callerName: created.client.name,
      };
      secretAcknowledged = false;
      copyState = "idle";
      await loadCallers();
      await selectCaller(created.client.id, "callers");
    } catch (error) {
      reportError(error);
    } finally {
      actionPending = null;
    }
  }

  async function savePolicy(event: SubmitEvent) {
    event.preventDefault();
    if (!selectedDetail || !policyForm) return;
    const source = selectedDetail.client;
    const patch: AdminCallerPatchInput = { expected_version: source.version };
    const models = parseCsv(policyForm.allowedModels);
    const groups = parseCsv(policyForm.groupIds);
    const description = policyForm.description.trim() || null;
    const nullableLimits = {
      daily_request_limit: nullableNumber(policyForm.dailyRequestLimit),
      daily_token_limit: nullableNumber(policyForm.dailyTokenLimit),
      total_request_limit: nullableNumber(policyForm.totalRequestLimit),
      total_token_limit: nullableNumber(policyForm.totalTokenLimit),
    };

    if (policyForm.name.trim() !== source.name) patch.name = policyForm.name.trim();
    if (description !== source.description) patch.description = description;
    if (!sameList(policyForm.allowedEndpoints, source.allowed_endpoints)) {
      patch.allowed_endpoints = policyForm.allowedEndpoints;
    }
    if (!sameList(models, source.allowed_models)) patch.allowed_models = models;
    if (!sameList(groups, source.group_ids)) patch.group_ids = groups;
    if (policyForm.qpsLimit !== source.qps_limit) patch.qps_limit = policyForm.qpsLimit;
    if (policyForm.concurrencyLimit !== source.concurrency_limit) {
      patch.concurrency_limit = policyForm.concurrencyLimit;
    }
    if (policyForm.maxOutputTokens !== source.max_output_tokens_per_request) {
      patch.max_output_tokens_per_request = policyForm.maxOutputTokens;
    }
    for (const [field, value] of Object.entries(nullableLimits) as [keyof typeof nullableLimits, number | null][]) {
      if (value !== source[field]) patch[field] = value;
    }

    if (Object.keys(patch).length === 1) {
      uiError = "没有需要保存的变更。";
      return;
    }
    if (models.length === 0 || policyForm.allowedEndpoints.length === 0) {
      uiError = "至少保留一个允许模型和一个接口权限。";
      return;
    }

    actionPending = "policy";
    uiError = null;
    try {
      const updated = await api.updateCaller(source.id, patch);
      selectedDetail = updated;
      policyForm = policyFromCaller(updated.client);
      await loadCallers();
    } catch (error) {
      reportError(error);
      if (error instanceof AdminApiError && error.code === "ADMIN_CALLER_CONFLICT") {
        await selectCaller(source.id);
      }
    } finally {
      actionPending = null;
    }
  }

  function askToRotate() {
    if (!selectedDetail) return;
    confirmation = {
      kind: "rotate",
      title: "签发新凭据",
      body: `将为“${selectedDetail.client.name}”签发一个新的调用方 Secret。旧 Secret 不会再次显示。`,
      confirmLabel: "签发新凭据",
    };
  }

  function askToRevoke(credential: CredentialSummary) {
    confirmation = {
      kind: "revoke",
      title: "撤销凭据",
      body: `撤销 ${credential.key_id} 后，使用该凭据的新请求会被拒绝。`,
      confirmLabel: "确认撤销",
      credentialId: credential.id,
    };
  }

  function askToDisable() {
    if (!selectedDetail) return;
    confirmation = {
      kind: "disable",
      title: "禁用调用方",
      body: `禁用“${selectedDetail.client.name}”会撤销其全部有效凭据，并排队同步 Bifrost 禁用状态。`,
      confirmLabel: "确认禁用",
    };
  }

  async function runConfirmedAction() {
    if (!confirmation || !selectedDetail) return;
    const action = confirmation;
    confirmation = null;
    actionPending = action.kind;
    uiError = null;
    try {
      if (action.kind === "rotate") {
        const issued = await api.rotateCredential(selectedDetail.client.id);
        oneTimeSecret = {
          secret: issued.secret,
          keyId: issued.credential.key_id,
          callerName: selectedDetail.client.name,
        };
        secretAcknowledged = false;
        copyState = "idle";
      } else if (action.kind === "revoke" && action.credentialId) {
        await api.revokeCredential(selectedDetail.client.id, action.credentialId);
      } else if (action.kind === "disable") {
        await api.disableCaller(selectedDetail.client.id, makeIdempotencyKey("disable"));
      }
      await loadCallers();
      await selectCaller(selectedDetail.client.id);
    } catch (error) {
      reportError(error);
    } finally {
      actionPending = null;
    }
  }

  async function copySecret() {
    if (!oneTimeSecret) return;
    try {
      await navigator.clipboard.writeText(oneTimeSecret.secret);
      copyState = "copied";
    } catch {
      copyState = "failed";
    }
  }

  function closeSecret() {
    if (!secretAcknowledged) return;
    oneTimeSecret = null;
    copyState = "idle";
    secretAcknowledged = false;
  }

  function policyFromCaller(caller: CallerSummary): PolicyForm {
    return {
      name: caller.name,
      description: caller.description ?? "",
      allowedEndpoints: [...caller.allowed_endpoints],
      allowedModels: caller.allowed_models.join(", "),
      groupIds: caller.group_ids.join(", "),
      qpsLimit: caller.qps_limit,
      concurrencyLimit: caller.concurrency_limit,
      dailyRequestLimit: numberInput(caller.daily_request_limit),
      dailyTokenLimit: numberInput(caller.daily_token_limit),
      totalRequestLimit: numberInput(caller.total_request_limit),
      totalTokenLimit: numberInput(caller.total_token_limit),
      maxOutputTokens: caller.max_output_tokens_per_request,
    };
  }

  function toggleEndpoint(target: "create" | "policy", permission: string) {
    const current = target === "create" ? createForm.allowedEndpoints : (policyForm?.allowedEndpoints ?? []);
    const next = current.includes(permission)
      ? current.filter((item) => item !== permission)
      : [...current, permission];
    if (target === "create") createForm.allowedEndpoints = next;
    else if (policyForm) policyForm.allowedEndpoints = next;
  }

  function parseCsv(value: string): string[] {
    return [
      ...new Set(
        value
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      ),
    ];
  }

  function nullableNumber(value: string): number | null {
    const trimmed = value.trim();
    return trimmed ? Number(trimmed) : null;
  }

  function numberInput(value: number | null): string {
    return value === null ? "" : String(value);
  }

  function sameList(left: string[], right: string[]): boolean {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function makeIdempotencyKey(action: string): string {
    return `${action}-${crypto.randomUUID()}`;
  }

  function trapFocus(node: HTMLElement) {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = () =>
      [
        ...node.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex='-1'])",
        ),
      ].filter((element) => element.offsetParent !== null);
    const frame = requestAnimationFrame(() => {
      const initial = node.querySelector<HTMLElement>("[data-autofocus]") ?? focusable()[0];
      initial?.focus();
    });
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = focusable();
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!node.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    node.addEventListener("keydown", onKeydown);
    return {
      destroy() {
        cancelAnimationFrame(frame);
        node.removeEventListener("keydown", onKeydown);
        previous?.focus();
      },
    };
  }

  function formatDate(value: number | null): string {
    if (value === null) return "--";
    return new Date(value * 1000).toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function statusLabel(status: string): string {
    return (
      {
        active: "正常",
        disabled: "已禁用",
        archived: "已归档",
        revoked: "已撤销",
        pending: "同步中",
        error: "同步失败",
      }[status] ?? status
    );
  }

  function reportError(error: unknown) {
    if (error instanceof AdminApiError) {
      if (error.status === 401) {
        callers = [];
        selectedCallerId = null;
        selectedDetail = null;
        policyForm = null;
        $session = null;
        $sessionPhase = "signed_out";
      }
      const messages: Record<string, string> = {
        ADMIN_AUTH_FAILED: "管理员登录令牌无效。",
        ADMIN_AUTH_REQUIRED: "管理员会话已失效，请重新登录。",
        ADMIN_CSRF_FAILED: "请求校验失败，请重新登录后再试。",
        ADMIN_CONFLICT: "名称或绑定已存在，请检查后重试。",
        ADMIN_CALLER_CONFLICT: "调用方已被其他操作更新，页面已重新载入最新状态。",
        ADMIN_CALLER_NOT_FOUND: "调用方不存在或已被移除。",
        ADMIN_CREDENTIAL_NOT_FOUND: "凭据不存在或已被撤销。",
        ADMIN_NETWORK_ERROR: "无法连接管理员服务。",
      };
      uiError = messages[error.code] ?? "管理员请求失败，请稍后重试。";
      if (error.requestId) uiError += ` 请求编号：${error.requestId}`;
      return;
    }
    uiError = "发生未预期错误，请稍后重试。";
  }
</script>

{#if $sessionPhase === "checking"}
  <section class="auth-shell" aria-live="polite">
    <LoaderCircle class="spin" size={24} />
    <p>正在验证管理员会话</p>
  </section>
{:else if $sessionPhase === "signed_out"}
  <section class="auth-shell">
    <form class="auth-panel" onsubmit={signIn}>
      <div class="auth-icon" aria-hidden="true"><ShieldCheck size={22} /></div>
      <div>
        <h1>管理员登录</h1>
        <p>使用部署环境配置的管理员登录令牌。</p>
      </div>
      {#if uiError}
        <div class="alert danger" role="alert">{uiError}</div>
      {/if}
      <label class="field full">
        <span>管理员登录令牌</span>
        <input
          type="password"
          name="admin-token"
          autocomplete="current-password"
          maxlength="512"
          required
          bind:value={loginToken}
          disabled={loginPending}
        />
      </label>
      <button class="primary-button full" type="submit" disabled={loginPending || !loginToken.trim()}>
        {#if loginPending}<LoaderCircle class="spin" size={16} />{:else}<UserRound size={16} />{/if}
        登录控制台
      </button>
    </form>
  </section>
{:else}
  <section class="page">
    {#if uiError}
      <div class="alert danger page-alert" role="alert">
        <span>{uiError}</span>
        <button
          class="icon-button"
          type="button"
          title="关闭提示"
          aria-label="关闭提示"
          onclick={() => (uiError = null)}
        >
          <X size={15} />
        </button>
      </div>
    {/if}

    {#if $activeSection === "overview"}
      <div class="page-header">
        <div>
          <h1>总览</h1>
          <p>调用方接入、状态与配额配置概况。</p>
        </div>
        <button class="secondary-button" type="button" onclick={loadCallers} disabled={loadingCallers}>
          <RefreshCw class={loadingCallers ? "spin" : ""} size={15} />
          刷新
        </button>
      </div>

      <div class="metrics-band" aria-label="调用方指标">
        <div class="metric">
          <span class="metric-label"><UsersRound size={15} />调用方</span>
          <strong class="metric-value">{callers.length}</strong>
          <span class="metric-note">当前已配置</span>
        </div>
        <div class="metric">
          <span class="metric-label"><CheckCircle2 size={15} />正常</span>
          <strong class="metric-value">{activeCallerCount()}</strong>
          <span class="metric-note">允许发起请求</span>
        </div>
        <div class="metric">
          <span class="metric-label"><Activity size={15} />已禁用</span>
          <strong class="metric-value">{disabledCallerCount()}</strong>
          <span class="metric-note">请求已停止</span>
        </div>
        <div class="metric">
          <span class="metric-label"><Gauge size={15} />设有用量上限</span>
          <strong class="metric-value">{limitedCallerCount()}</strong>
          <span class="metric-note">不含 QPS 与并发</span>
        </div>
      </div>

      <div class="overview-grid">
        <section class="section-block" aria-labelledby="recent-callers-heading">
          <div class="section-heading">
            <h2 id="recent-callers-heading">最近更新</h2>
            <button class="text-button" type="button" onclick={() => ($activeSection = "callers")}>查看全部</button>
          </div>
          {#if loadingCallers}
            <div class="loading-state"><LoaderCircle class="spin" size={20} />正在载入</div>
          {:else if callers.length === 0}
            <div class="empty-state compact">
              <UsersRound size={22} />
              <strong>尚无调用方</strong>
              <button class="primary-button" type="button" onclick={openCreateDialog}
                ><Plus size={15} />新建调用方</button
              >
            </div>
          {:else}
            <div class="table-scroll">
              <table class="data-table">
                <thead><tr><th>名称</th><th>状态</th><th>模型</th><th>更新时间</th></tr></thead>
                <tbody>
                  {#each [...callers].sort((a, b) => b.updated_at - a.updated_at).slice(0, 5) as caller (caller.id)}
                    <tr>
                      <td
                        ><button class="table-link" type="button" onclick={() => selectCaller(caller.id, "callers")}
                          >{caller.name}</button
                        ></td
                      >
                      <td
                        ><span class:danger={caller.status === "disabled"} class="status-badge"
                          >{statusLabel(caller.status)}</span
                        ></td
                      >
                      <td>{caller.allowed_models.join("、")}</td>
                      <td>{formatDate(caller.updated_at)}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </section>

        <section class="section-block" aria-labelledby="authority-heading">
          <div class="section-heading">
            <h2 id="authority-heading">系统边界</h2>
            <span class="section-meta">已连接</span>
          </div>
          <ul class="boundary-list">
            <li>
              <span class="boundary-icon"><ShieldCheck size={14} /></span><span
                ><span class="boundary-title">管理员 API</span><span class="boundary-copy"
                  >会话验证与 CSRF 校验已启用。</span
                ></span
              >
            </li>
            <li>
              <span class="boundary-icon"><KeyRound size={14} /></span><span
                ><span class="boundary-title">凭据隔离</span><span class="boundary-copy"
                  >调用方 Secret 与 Bifrost 虚拟密钥分离。</span
                ></span
              >
            </li>
            <li>
              <span class="boundary-icon"><Activity size={14} /></span><span
                ><span class="boundary-title">积分账本</span><span class="boundary-copy"
                  >余额与扣费仍以 Open WebUI 为唯一真值。</span
                ></span
              >
            </li>
          </ul>
        </section>
      </div>
    {:else if $activeSection === "callers"}
      <div class="page-header">
        <div>
          <h1>调用方</h1>
          <p>管理 API 接入主体、模型权限与运行状态。</p>
        </div>
        <button class="primary-button" type="button" onclick={openCreateDialog}><Plus size={15} />新建调用方</button>
      </div>

      <div class="toolbar">
        <label class="search-field">
          <Search size={15} aria-hidden="true" />
          <span class="sr-only">搜索调用方</span>
          <input type="search" placeholder="搜索名称、ID 或模型" bind:value={searchQuery} />
        </label>
        <span class="result-count">{filteredCallers().length} 个调用方</span>
      </div>

      <section class="section-block">
        {#if loadingCallers}
          <div class="loading-state"><LoaderCircle class="spin" size={20} />正在载入调用方</div>
        {:else if filteredCallers().length === 0}
          <div class="empty-state">
            <UsersRound size={24} /><strong>{searchQuery ? "没有匹配的调用方" : "尚无调用方"}</strong>
          </div>
        {:else}
          <div class="table-scroll">
            <table class="data-table caller-table">
              <thead
                ><tr><th>名称</th><th>状态</th><th>允许模型</th><th>QPS / 并发</th><th>版本</th><th>操作</th></tr
                ></thead
              >
              <tbody>
                {#each filteredCallers() as caller (caller.id)}
                  <tr class:selected={selectedCallerId === caller.id}>
                    <td
                      ><button class="table-link caller-name" type="button" onclick={() => selectCaller(caller.id)}
                        ><span>{caller.name}</span><small>{caller.id}</small></button
                      ></td
                    >
                    <td
                      ><span class:danger={caller.status === "disabled"} class="status-badge"
                        >{statusLabel(caller.status)}</span
                      ></td
                    >
                    <td>{caller.allowed_models.join("、")}</td>
                    <td>{caller.qps_limit} / {caller.concurrency_limit}</td>
                    <td>v{caller.version}</td>
                    <td
                      ><button class="secondary-button small" type="button" onclick={() => selectCaller(caller.id)}
                        >查看</button
                      ></td
                    >
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>

      {#if loadingDetail}
        <div class="loading-state detail-loading"><LoaderCircle class="spin" size={20} />正在载入详情</div>
      {:else if selectedDetail}
        <section class="detail-section" aria-labelledby="caller-detail-heading">
          <div class="detail-heading">
            <div>
              <span class="eyebrow">调用方详情</span>
              <h2 id="caller-detail-heading">{selectedDetail.client.name}</h2>
            </div>
            <div class="detail-actions">
              <button
                class="secondary-button"
                type="button"
                onclick={() => selectCaller(selectedDetail!.client.id, "policy")}
                ><Gauge size={15} />编辑权限与配额</button
              >
              <button
                class="secondary-button"
                type="button"
                onclick={() => selectCaller(selectedDetail!.client.id, "credentials")}
                ><KeyRound size={15} />管理凭据</button
              >
              <button
                class="icon-button"
                type="button"
                title="收起详情"
                aria-label="收起详情"
                onclick={clearSelectedCaller}><X size={16} /></button
              >
            </div>
          </div>
          <div class="detail-grid">
            <dl class="definition-list">
              <div>
                <dt>状态</dt>
                <dd>
                  <span class:danger={selectedDetail.client.status === "disabled"} class="status-badge"
                    >{statusLabel(selectedDetail.client.status)}</span
                  >
                </dd>
              </div>
              <div>
                <dt>说明</dt>
                <dd>{selectedDetail.client.description || "--"}</dd>
              </div>
              <div>
                <dt>允许接口</dt>
                <dd>{selectedDetail.client.allowed_endpoints.join("、")}</dd>
              </div>
              <div>
                <dt>允许模型</dt>
                <dd>{selectedDetail.client.allowed_models.join("、")}</dd>
              </div>
              <div>
                <dt>更新时间</dt>
                <dd>{formatDate(selectedDetail.client.updated_at)}</dd>
              </div>
            </dl>
            <div class="binding-panel">
              <div class="subsection-heading">
                <h3>绑定同步</h3>
                {#if selectedDetail.binding}<span
                    class:error={selectedDetail.binding.sync_status === "error"}
                    class="status-badge">{statusLabel(selectedDetail.binding.sync_status)}</span
                  >{/if}
              </div>
              {#if selectedDetail.binding}
                <dl class="compact-definition">
                  <div>
                    <dt>服务用户</dt>
                    <dd>{selectedDetail.binding.zangpu_service_user_id || "--"}</dd>
                  </div>
                  <div>
                    <dt>虚拟密钥 ID</dt>
                    <dd>{selectedDetail.binding.bifrost_virtual_key_id || "等待同步"}</dd>
                  </div>
                  <div>
                    <dt>同步版本</dt>
                    <dd>v{selectedDetail.binding.version}</dd>
                  </div>
                </dl>
              {:else}
                <p class="muted-copy">尚无绑定记录。</p>
              {/if}
              {#if selectedDetail.client.status === "active"}
                <button class="danger-button" type="button" onclick={askToDisable} disabled={actionPending !== null}
                  ><Trash2 size={15} />禁用调用方</button
                >
              {/if}
            </div>
          </div>
        </section>
      {/if}
    {:else if $activeSection === "credentials"}
      <div class="page-header">
        <div>
          <h1>凭据</h1>
          <p>签发、查看和撤销调用方凭据。</p>
        </div>
        {#if selectedDetail?.client.status === "active"}
          <button class="primary-button" type="button" onclick={askToRotate} disabled={actionPending !== null}
            ><RotateCcw size={15} />签发新凭据</button
          >
        {/if}
      </div>

      <div class="management-grid">
        <aside class="caller-picker" aria-label="选择调用方">
          <div class="picker-heading">调用方</div>
          {#each callers as caller (caller.id)}
            <button class:active={selectedCallerId === caller.id} type="button" onclick={() => selectCaller(caller.id)}>
              <span>{caller.name}</span><small>{statusLabel(caller.status)}</small>
            </button>
          {/each}
        </aside>
        <section class="management-panel">
          {#if loadingDetail}
            <div class="loading-state"><LoaderCircle class="spin" size={20} />正在载入凭据</div>
          {:else if !selectedDetail}
            <div class="empty-state"><KeyRound size={24} /><strong>选择调用方查看凭据</strong></div>
          {:else}
            <div class="panel-heading">
              <div>
                <span class="eyebrow">{selectedDetail.client.name}</span>
                <h2>凭据列表</h2>
              </div>
              <span class="result-count">{selectedDetail.credentials.length} 个</span>
            </div>
            <div class="table-scroll">
              <table class="data-table credential-table">
                <thead><tr><th>Key ID</th><th>状态</th><th>签发时间</th><th>最后使用</th><th>操作</th></tr></thead>
                <tbody>
                  {#each selectedDetail.credentials as credential (credential.id)}
                    <tr>
                      <td><code>{credential.key_id}</code></td>
                      <td
                        ><span class:danger={credential.status === "revoked"} class="status-badge"
                          >{statusLabel(credential.status)}</span
                        ></td
                      >
                      <td>{formatDate(credential.created_at)}</td>
                      <td>{formatDate(credential.last_used_at)}</td>
                      <td
                        >{#if credential.status === "active"}<button
                            class="danger-button small"
                            type="button"
                            onclick={() => askToRevoke(credential)}><Trash2 size={14} />撤销</button
                          >{/if}</td
                      >
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </section>
      </div>
    {:else if $activeSection === "policy"}
      <div class="page-header">
        <div>
          <h1>权限与配额</h1>
          <p>调整调用方的接口、模型和请求上限。</p>
        </div>
      </div>
      <div class="management-grid">
        <aside class="caller-picker" aria-label="选择调用方">
          <div class="picker-heading">调用方</div>
          {#each callers as caller (caller.id)}
            <button class:active={selectedCallerId === caller.id} type="button" onclick={() => selectCaller(caller.id)}>
              <span>{caller.name}</span><small>{statusLabel(caller.status)}</small>
            </button>
          {/each}
        </aside>
        <section class="management-panel">
          {#if loadingDetail}
            <div class="loading-state"><LoaderCircle class="spin" size={20} />正在载入配置</div>
          {:else if !selectedDetail || !policyForm}
            <div class="empty-state"><Gauge size={24} /><strong>选择调用方编辑权限与配额</strong></div>
          {:else}
            <form class="policy-form" onsubmit={savePolicy}>
              <div class="panel-heading">
                <div>
                  <span class="eyebrow">版本 v{selectedDetail.client.version}</span>
                  <h2>{selectedDetail.client.name}</h2>
                </div>
                <button class="primary-button" type="submit" disabled={actionPending !== null}
                  >{#if actionPending === "policy"}<LoaderCircle class="spin" size={15} />{:else}<Save
                      size={15}
                    />{/if}保存变更</button
                >
              </div>
              <div class="form-section">
                <h3>基本信息</h3>
                <div class="form-grid two">
                  <label class="field"
                    ><span>名称</span><input required maxlength="128" bind:value={policyForm.name} /></label
                  >
                  <label class="field"
                    ><span>说明</span><input maxlength="1024" bind:value={policyForm.description} /></label
                  >
                </div>
              </div>
              <div class="form-section">
                <h3>接口权限</h3>
                <div class="permission-grid">
                  {#each ADMIN_ENDPOINT_PERMISSIONS as permission}
                    <label class="permission-option">
                      <input
                        type="checkbox"
                        checked={policyForm.allowedEndpoints.includes(permission)}
                        onchange={() => toggleEndpoint("policy", permission)}
                      />
                      <span
                        ><strong>{endpointLabels[permission].title}</strong><small
                          >{endpointLabels[permission].detail}</small
                        ></span
                      >
                    </label>
                  {/each}
                </div>
                <div class="form-grid two top-gap">
                  <label class="field"
                    ><span>允许模型</span><input required bind:value={policyForm.allowedModels} /></label
                  >
                  <label class="field"><span>分组 ID</span><input bind:value={policyForm.groupIds} /></label>
                </div>
              </div>
              <div class="form-section">
                <h3>速率与用量</h3>
                <div class="form-grid four">
                  <label class="field"
                    ><span>QPS</span><input
                      type="number"
                      min="1"
                      max="100000"
                      required
                      bind:value={policyForm.qpsLimit}
                    /></label
                  >
                  <label class="field"
                    ><span>并发</span><input
                      type="number"
                      min="1"
                      max="10000"
                      required
                      bind:value={policyForm.concurrencyLimit}
                    /></label
                  >
                  <label class="field"
                    ><span>单次最大输出 Token</span><input
                      type="number"
                      min="1"
                      max="1000000"
                      required
                      bind:value={policyForm.maxOutputTokens}
                    /></label
                  >
                  <label class="field"
                    ><span>每日请求</span><input
                      type="number"
                      min="1"
                      placeholder="不限"
                      bind:value={policyForm.dailyRequestLimit}
                    /></label
                  >
                  <label class="field"
                    ><span>每日 Token</span><input
                      type="number"
                      min="1"
                      placeholder="不限"
                      bind:value={policyForm.dailyTokenLimit}
                    /></label
                  >
                  <label class="field"
                    ><span>累计请求</span><input
                      type="number"
                      min="1"
                      placeholder="不限"
                      bind:value={policyForm.totalRequestLimit}
                    /></label
                  >
                  <label class="field"
                    ><span>累计 Token</span><input
                      type="number"
                      min="1"
                      placeholder="不限"
                      bind:value={policyForm.totalTokenLimit}
                    /></label
                  >
                </div>
              </div>
            </form>
          {/if}
        </section>
      </div>
    {/if}
  </section>
{/if}

{#if createOpen}
  <div class="modal-backdrop" role="presentation">
    <div class="modal large" role="dialog" aria-modal="true" aria-labelledby="create-title" use:trapFocus>
      <div class="modal-heading">
        <div>
          <span class="eyebrow">API 接入</span>
          <h2 id="create-title">新建调用方</h2>
        </div>
        <button class="icon-button" type="button" title="关闭" aria-label="关闭" onclick={() => (createOpen = false)}
          ><X size={17} /></button
        >
      </div>
      <form onsubmit={createCaller}>
        <div class="modal-body">
          <div class="form-section first">
            <h3>基本信息</h3>
            <div class="form-grid two">
              <label class="field"
                ><span>名称</span><input data-autofocus required maxlength="128" bind:value={createForm.name} /></label
              >
              <label class="field"
                ><span>说明</span><input maxlength="1024" bind:value={createForm.description} /></label
              >
              <label class="field"
                ><span>服务用户 ID</span><input required maxlength="128" bind:value={createForm.serviceUserId} /></label
              >
              <label class="field"
                ><span>提供方</span><input required maxlength="128" bind:value={createForm.provider} /></label
              >
              <label class="field"
                ><span>绑定模型</span><input required maxlength="255" bind:value={createForm.model} /></label
              >
              <label class="field"
                ><span>允许模型</span><input
                  placeholder="多个模型用逗号分隔"
                  bind:value={createForm.allowedModels}
                /></label
              >
            </div>
          </div>
          <div class="form-section">
            <h3>接口权限</h3>
            <div class="permission-grid">
              {#each ADMIN_ENDPOINT_PERMISSIONS as permission}
                <label class="permission-option">
                  <input
                    type="checkbox"
                    checked={createForm.allowedEndpoints.includes(permission)}
                    onchange={() => toggleEndpoint("create", permission)}
                  />
                  <span
                    ><strong>{endpointLabels[permission].title}</strong><small
                      >{endpointLabels[permission].detail}</small
                    ></span
                  >
                </label>
              {/each}
            </div>
            <label class="field top-gap"
              ><span>分组 ID</span><input placeholder="多个分组用逗号分隔" bind:value={createForm.groupIds} /></label
            >
          </div>
          <div class="form-section">
            <h3>速率与用量</h3>
            <div class="form-grid four">
              <label class="field"
                ><span>QPS</span><input
                  type="number"
                  min="1"
                  max="100000"
                  required
                  bind:value={createForm.qpsLimit}
                /></label
              >
              <label class="field"
                ><span>并发</span><input
                  type="number"
                  min="1"
                  max="10000"
                  required
                  bind:value={createForm.concurrencyLimit}
                /></label
              >
              <label class="field"
                ><span>单次最大输出 Token</span><input
                  type="number"
                  min="1"
                  max="1000000"
                  required
                  bind:value={createForm.maxOutputTokens}
                /></label
              >
              <label class="field"
                ><span>每日请求</span><input
                  type="number"
                  min="1"
                  placeholder="不限"
                  bind:value={createForm.dailyRequestLimit}
                /></label
              >
              <label class="field"
                ><span>每日 Token</span><input
                  type="number"
                  min="1"
                  placeholder="不限"
                  bind:value={createForm.dailyTokenLimit}
                /></label
              >
              <label class="field"
                ><span>累计请求</span><input
                  type="number"
                  min="1"
                  placeholder="不限"
                  bind:value={createForm.totalRequestLimit}
                /></label
              >
              <label class="field"
                ><span>累计 Token</span><input
                  type="number"
                  min="1"
                  placeholder="不限"
                  bind:value={createForm.totalTokenLimit}
                /></label
              >
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="secondary-button" type="button" onclick={() => (createOpen = false)}>取消</button><button
            class="primary-button"
            type="submit"
            disabled={actionPending !== null}
            >{#if actionPending === "create"}<LoaderCircle class="spin" size={15} />{:else}<Plus
                size={15}
              />{/if}创建并签发凭据</button
          >
        </div>
      </form>
    </div>
  </div>
{/if}

{#if confirmation}
  <div class="modal-backdrop" role="presentation">
    <div
      class="modal confirm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
      aria-describedby="confirm-copy"
      use:trapFocus
    >
      <div class="modal-heading">
        <div>
          <span class="eyebrow">需要确认</span>
          <h2 id="confirm-title">{confirmation.title}</h2>
        </div>
        <button class="icon-button" type="button" title="关闭" aria-label="关闭" onclick={() => (confirmation = null)}
          ><X size={17} /></button
        >
      </div>
      <div class="modal-body"><p id="confirm-copy" class="confirm-copy">{confirmation.body}</p></div>
      <div class="modal-footer">
        <button class="secondary-button" data-autofocus type="button" onclick={() => (confirmation = null)}>取消</button
        ><button
          class:danger-action={confirmation.kind !== "rotate"}
          class="primary-button"
          type="button"
          onclick={runConfirmedAction}>{confirmation.confirmLabel}</button
        >
      </div>
    </div>
  </div>
{/if}

{#if oneTimeSecret}
  <div class="modal-backdrop secret-layer" role="presentation">
    <div
      class="modal secret-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="secret-title"
      aria-describedby="secret-warning"
      use:trapFocus
    >
      <div class="modal-heading">
        <div>
          <span class="eyebrow">仅显示一次</span>
          <h2 id="secret-title">保存调用方 Secret</h2>
        </div>
      </div>
      <div class="modal-body">
        <div id="secret-warning" class="secret-warning">
          <Clipboard size={18} />
          <p><strong>{oneTimeSecret.callerName}</strong> 的 Secret 关闭后无法再次查看。</p>
        </div>
        <label class="field"><span>Key ID</span><input readonly value={oneTimeSecret.keyId} /></label>
        <div class="field top-gap">
          <span>Secret</span>
          <div class="secret-value">
            <code>{oneTimeSecret.secret}</code><button
              class="icon-button inverse"
              data-autofocus
              type="button"
              title="复制 Secret"
              aria-label="复制 Secret"
              onclick={copySecret}
              >{#if copyState === "copied"}<Check size={16} />{:else}<Copy size={16} />{/if}</button
            >
          </div>
        </div>
        {#if copyState === "copied"}<p class="copy-feedback success" role="status">
            已复制到剪贴板
          </p>{:else if copyState === "failed"}<p class="copy-feedback danger-text" role="status">
            复制失败，请手动选中保存
          </p>{/if}
        <label class="acknowledgement"
          ><input type="checkbox" bind:checked={secretAcknowledged} /><span>我已安全保存此 Secret</span></label
        >
      </div>
      <div class="modal-footer">
        <button class="primary-button" type="button" disabled={!secretAcknowledged} onclick={closeSecret}>完成</button>
      </div>
    </div>
  </div>
{/if}
