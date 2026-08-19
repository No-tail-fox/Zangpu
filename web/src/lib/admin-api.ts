export const ADMIN_ENDPOINT_PERMISSIONS = ["chat.completions", "models.read", "usage.read", "health.read"] as const;

export type AdminEndpointPermission = (typeof ADMIN_ENDPOINT_PERMISSIONS)[number];

export interface AdminSession {
  actor_id: string;
  expires_at: number;
  csrf_token: string;
}

export interface CallerSummary {
  id: string;
  name: string;
  description: string | null;
  status: "active" | "disabled" | "archived";
  allowed_endpoints: string[];
  allowed_models: string[];
  group_ids: string[];
  qps_limit: number;
  concurrency_limit: number;
  daily_request_limit: number | null;
  daily_token_limit: number | null;
  total_request_limit: number | null;
  total_token_limit: number | null;
  max_output_tokens_per_request: number;
  version: number;
  created_at: number;
  updated_at: number;
}

export interface CredentialSummary {
  id: string;
  api_client_id: string;
  key_id: string;
  status: "active" | "revoked";
  expires_at: number | null;
  last_used_at: number | null;
  replaced_by_id: string | null;
  created_at: number;
  revoked_at: number | null;
}

export interface BindingSummary {
  id: string;
  api_client_id: string;
  zangpu_service_user_id: string | null;
  bifrost_virtual_key_id: string | null;
  bifrost_config_hash: string;
  sync_status: "pending" | "active" | "disabled" | "error";
  last_sync_error_code: string | null;
  version: number;
  created_at: number;
  updated_at: number;
}

export interface CallerDetail {
  client: CallerSummary;
  credentials: CredentialSummary[];
  binding: BindingSummary | null;
}

export interface CallerListResult {
  items: CallerSummary[];
  offset: number;
  limit: number;
}

export interface CallerConcurrencyState {
  api_client_id: string;
  configured_limit: number;
  occupied: number;
  available: number;
  state: "idle" | "available" | "saturated";
  observed_at_ms: number;
  next_lease_expires_at_ms: number;
  last_lease_expires_at_ms: number;
}

export type ModelCapacityState = "idle" | "available" | "saturated" | "queued";

export interface ModelPoolCapacity {
  pool_id: string;
  model_ids: string[];
  state: ModelCapacityState;
  active_count: number;
  active_limit: number;
  active_remaining: number;
  pool_queue_count: number;
  next_active_expires_at_ms: number;
  next_queue_expires_at_ms: number;
  observed_at_ms: number;
}

export interface ModelCapacitySnapshot {
  state: ModelCapacityState;
  pool_count: number;
  active_count: number;
  active_limit: number;
  active_remaining: number;
  global_queue_count: number;
  global_queue_limit: number;
  global_queue_remaining: number;
  observed_at_ms: number;
  pools: ModelPoolCapacity[];
}

export interface AdminCallerCreateInput {
  name: string;
  description: string | null;
  service_user_id: string;
  provider: string;
  model: string;
  allowed_endpoints: string[];
  allowed_models: string[];
  group_ids: string[];
  qps_limit: number;
  concurrency_limit: number;
  daily_request_limit: number | null;
  daily_token_limit: number | null;
  total_request_limit: number | null;
  total_token_limit: number | null;
  max_output_tokens_per_request: number;
}

export type AdminCallerPatchInput = Partial<
  Pick<
    AdminCallerCreateInput,
    | "name"
    | "description"
    | "allowed_endpoints"
    | "allowed_models"
    | "group_ids"
    | "qps_limit"
    | "concurrency_limit"
    | "daily_request_limit"
    | "daily_token_limit"
    | "total_request_limit"
    | "total_token_limit"
    | "max_output_tokens_per_request"
  >
> & { expected_version: number };

export interface CallerCreateResult {
  client: CallerSummary;
  credential: CredentialSummary;
  binding: BindingSummary;
  outbox_id: string;
  secret: string;
  display_once: true;
}

export interface CredentialIssueResult {
  credential: CredentialSummary;
  secret: string;
  display_once: true;
}

interface AdminErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    request_id?: string;
  };
}

export class AdminApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(status: number, code: string, message: string, requestId: string | null = null) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

interface RequestOptions extends RequestInit {
  csrf?: boolean;
  idempotencyKey?: string;
}

const ADMIN_ROOT = "/api/v1/admin";

export class AdminApiClient {
  private csrfToken: string | null;

  constructor(
    private readonly fetcher: typeof fetch = fetch,
    initialSession: AdminSession | null = null,
  ) {
    this.csrfToken = initialSession?.csrf_token ?? null;
  }

  async restoreSession(): Promise<AdminSession | null> {
    try {
      const restored = await this.request<AdminSession>("/session", { method: "GET" });
      this.csrfToken = restored.csrf_token;
      return restored;
    } catch (error) {
      if (error instanceof AdminApiError && error.status === 401) return null;
      throw error;
    }
  }

  async login(token: string): Promise<AdminSession> {
    const loggedIn = await this.request<AdminSession>("/session", {
      method: "POST",
      headers: { "X-Zangpu-Admin-Token": token },
    });
    this.csrfToken = loggedIn.csrf_token;
    return loggedIn;
  }

  async logout(): Promise<void> {
    await this.request<{ status: "signed_out" }>("/session/logout", { method: "POST", csrf: true });
    this.csrfToken = null;
  }

  listCallers(offset = 0, limit = 100): Promise<CallerListResult> {
    return this.request(`/callers?offset=${offset}&limit=${limit}`, { method: "GET" });
  }

  getCaller(clientId: string): Promise<CallerDetail> {
    return this.request(`/callers/${encodeURIComponent(clientId)}`, { method: "GET" });
  }

  getCallerConcurrency(clientId: string): Promise<CallerConcurrencyState> {
    return this.request(`/callers/${encodeURIComponent(clientId)}/concurrency`, { method: "GET" });
  }

  getModelCapacity(): Promise<ModelCapacitySnapshot> {
    return this.request("/capacity/model-pools", { method: "GET" });
  }

  createCaller(payload: AdminCallerCreateInput, idempotencyKey: string): Promise<CallerCreateResult> {
    return this.request("/callers", {
      method: "POST",
      body: JSON.stringify(payload),
      csrf: true,
      idempotencyKey,
    });
  }

  updateCaller(clientId: string, payload: AdminCallerPatchInput): Promise<CallerDetail> {
    return this.request(`/callers/${encodeURIComponent(clientId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
      csrf: true,
    });
  }

  rotateCredential(clientId: string): Promise<CredentialIssueResult> {
    return this.request(`/callers/${encodeURIComponent(clientId)}/credentials/rotate`, {
      method: "POST",
      csrf: true,
    });
  }

  revokeCredential(clientId: string, credentialId: string): Promise<CredentialSummary> {
    return this.request(
      `/callers/${encodeURIComponent(clientId)}/credentials/${encodeURIComponent(credentialId)}/revoke`,
      { method: "POST", csrf: true },
    );
  }

  disableCaller(clientId: string, idempotencyKey: string): Promise<CallerDetail> {
    return this.request(`/callers/${encodeURIComponent(clientId)}/disable`, {
      method: "POST",
      csrf: true,
      idempotencyKey,
    });
  }

  private async request<T>(path: string, options: RequestOptions): Promise<T> {
    const { csrf = false, idempotencyKey, ...init } = options;
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body !== undefined) headers.set("Content-Type", "application/json");
    if (csrf) {
      if (!this.csrfToken) {
        throw new AdminApiError(401, "ADMIN_AUTH_REQUIRED", "Administrator authentication is required.");
      }
      headers.set("X-Zangpu-CSRF", this.csrfToken);
    }
    if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);

    let response: Response;
    try {
      response = await this.fetcher(`${ADMIN_ROOT}${path}`, {
        ...init,
        headers,
        credentials: "include",
        cache: "no-store",
      });
    } catch {
      throw new AdminApiError(0, "ADMIN_NETWORK_ERROR", "Administrator service is unavailable.");
    }

    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("application/json")
      ? ((await response.json().catch(() => null)) as T | AdminErrorEnvelope | null)
      : null;

    if (!response.ok) {
      if (response.status === 401) this.csrfToken = null;
      const envelope = payload as AdminErrorEnvelope | null;
      throw new AdminApiError(
        response.status,
        envelope?.error?.code ?? "ADMIN_REQUEST_FAILED",
        envelope?.error?.message ?? "Administrator request failed.",
        envelope?.error?.request_id ?? null,
      );
    }
    if (payload === null) {
      throw new AdminApiError(response.status, "ADMIN_INVALID_RESPONSE", "Administrator response is invalid.");
    }
    return payload as T;
  }
}
