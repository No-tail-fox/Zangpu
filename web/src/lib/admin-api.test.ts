import { describe, expect, it, vi } from "vitest";

import {
  AdminApiClient,
  AdminApiError,
  type AdminCallerCreateInput,
  type BindingSummary,
  type CallerCreateResult,
  type CallerConcurrencyState,
  type CallerDetail,
  type CallerSummary,
  type CredentialSummary,
} from "./admin-api";

const session = {
  actor_id: "admin",
  expires_at: 1_800_000_600,
  csrf_token: "csrf-only-in-memory",
};

const caller: CallerSummary = {
  id: "caller-1",
  name: "教学应用",
  description: "课堂问答接入",
  status: "active",
  allowed_endpoints: ["chat.completions", "models.read"],
  allowed_models: ["zangpu-chat"],
  group_ids: ["teaching"],
  qps_limit: 20,
  concurrency_limit: 4,
  daily_request_limit: 1000,
  daily_token_limit: null,
  total_request_limit: null,
  total_token_limit: null,
  max_output_tokens_per_request: 4096,
  version: 3,
  created_at: 1_800_000_000,
  updated_at: 1_800_000_100,
};

const credential: CredentialSummary = {
  id: "credential-1",
  api_client_id: caller.id,
  key_id: "zpk_example1",
  status: "active",
  expires_at: null,
  last_used_at: null,
  replaced_by_id: null,
  created_at: 1_800_000_000,
  revoked_at: null,
};

const binding: BindingSummary = {
  id: "binding-1",
  api_client_id: caller.id,
  zangpu_service_user_id: "service-user-1",
  bifrost_virtual_key_id: "vk-1",
  bifrost_config_hash: "hash-1",
  sync_status: "active",
  last_sync_error_code: null,
  version: 2,
  created_at: 1_800_000_000,
  updated_at: 1_800_000_100,
};

const detail: CallerDetail = {
  client: { ...caller },
  credentials: [{ ...credential }],
  binding: { ...binding },
};

const concurrency: CallerConcurrencyState = {
  api_client_id: caller.id,
  configured_limit: 4,
  occupied: 3,
  available: 1,
  state: "available",
  observed_at_ms: 1_800_000_200_000,
  next_lease_expires_at_ms: 1_800_000_230_000,
  last_lease_expires_at_ms: 1_800_000_250_000,
};

const createInput: AdminCallerCreateInput = {
  name: "教学应用",
  description: "课堂问答接入",
  service_user_id: "service-user-1",
  provider: "openai-compatible",
  model: "zangpu-chat",
  allowed_endpoints: ["chat.completions", "models.read"],
  allowed_models: ["zangpu-chat"],
  group_ids: ["teaching"],
  qps_limit: 20,
  concurrency_limit: 4,
  daily_request_limit: 1000,
  daily_token_limit: null,
  total_request_limit: null,
  total_token_limit: null,
  max_output_tokens_per_request: 4096,
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("administrator API client", () => {
  it("keeps the login token out of the body and restores 401 as a signed-out state", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ error: { code: "ADMIN_AUTH_REQUIRED" } }, 401))
      .mockResolvedValueOnce(jsonResponse(session));
    const api = new AdminApiClient(fetcher);

    await expect(api.restoreSession()).resolves.toBeNull();
    await expect(api.login("deployment-login-token")).resolves.toEqual(session);

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      "/api/v1/admin/session",
      expect.objectContaining({ method: "GET", credentials: "include", cache: "no-store" }),
    );
    const [, loginInit] = fetcher.mock.calls[1];
    const loginHeaders = new Headers(loginInit?.headers);
    expect(loginHeaders.get("X-Zangpu-Admin-Token")).toBe("deployment-login-token");
    expect(loginInit?.body).toBeUndefined();
    expect(JSON.stringify(loginInit)).not.toContain(session.csrf_token);
  });

  it("adds in-memory CSRF, idempotency and optimistic version data to mutations", async () => {
    const created: CallerCreateResult = {
      client: { ...caller },
      credential: { ...credential },
      binding: { ...binding },
      outbox_id: "outbox-1",
      secret: "zps_display_once",
      display_once: true,
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse(created, 201))
      .mockResolvedValueOnce(jsonResponse(detail))
      .mockResolvedValueOnce(jsonResponse(detail));
    const api = new AdminApiClient(fetcher);
    await api.login("deployment-login-token");

    await api.createCaller(createInput, "create-caller-1");
    await api.updateCaller(caller.id, { expected_version: caller.version, qps_limit: 25 });
    await api.disableCaller(caller.id, "disable-caller-1");

    const [, createInit] = fetcher.mock.calls[1];
    const createHeaders = new Headers(createInit?.headers);
    expect(createHeaders.get("X-Zangpu-CSRF")).toBe(session.csrf_token);
    expect(createHeaders.get("Idempotency-Key")).toBe("create-caller-1");
    expect(JSON.parse(String(createInit?.body))).toEqual(createInput);

    const [, updateInit] = fetcher.mock.calls[2];
    expect(JSON.parse(String(updateInit?.body))).toEqual({ expected_version: 3, qps_limit: 25 });
    expect(new Headers(updateInit?.headers).get("X-Zangpu-CSRF")).toBe(session.csrf_token);

    const [disableUrl, disableInit] = fetcher.mock.calls[3];
    expect(disableUrl).toBe("/api/v1/admin/callers/caller-1/disable");
    expect(new Headers(disableInit?.headers).get("Idempotency-Key")).toBe("disable-caller-1");
  });

  it("reads live caller concurrency without CSRF or mutation headers", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(concurrency));
    const api = new AdminApiClient(fetcher, session);

    await expect(api.getCallerConcurrency("caller/1")).resolves.toEqual(concurrency);

    const [url, init] = fetcher.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(url).toBe("/api/v1/admin/callers/caller%2F1/concurrency");
    expect(init?.method).toBe("GET");
    expect(headers.has("X-Zangpu-CSRF")).toBe(false);
    expect(headers.has("Idempotency-Key")).toBe(false);
  });

  it("surfaces bounded administrator errors without leaking response text", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(
      jsonResponse(
        {
          error: {
            code: "ADMIN_CALLER_CONFLICT",
            message: "Caller state has changed.",
            request_id: "adm_request_1",
          },
        },
        409,
      ),
    );
    const api = new AdminApiClient(fetcher, session);

    const error = await api.getCaller("caller-1").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(AdminApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "ADMIN_CALLER_CONFLICT",
      requestId: "adm_request_1",
      message: "Caller state has changed.",
    });
    expect(String(error)).not.toContain("csrf-only-in-memory");
  });
});
