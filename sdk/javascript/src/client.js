import {
  ZangpuAPIError,
  ZangpuError,
  ZangpuProtocolError,
  ZangpuTransportError,
} from "./errors.js";
import { ZangpuSigner } from "./signing.js";

export const MAX_REQUEST_BYTES = 1024 * 1024;
export const MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024;
export const MAX_SSE_EVENT_BYTES = 1024 * 1024;
export const SDK_VERSION = "0.1.0";

const SAFE_RESPONSE_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);
const EMPTY_BODY = new Uint8Array();
const encoder = new TextEncoder();

function contentType(response) {
  return (response.headers.get("content-type") ?? "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
}

function serverRequestId(response) {
  const value = response.headers.get("x-zangpu-request-id") ?? "";
  if (!SAFE_RESPONSE_ID_RE.test(value)) {
    throw new ZangpuProtocolError();
  }
  return value;
}

function rateLimit(response) {
  const limit = Number(response.headers.get("x-ratelimit-limit"));
  const remaining = Number(response.headers.get("x-ratelimit-remaining"));
  const resetAt = Number(response.headers.get("x-ratelimit-reset"));
  if (
    !Number.isSafeInteger(limit) ||
    !Number.isSafeInteger(remaining) ||
    !Number.isSafeInteger(resetAt) ||
    limit <= 0 ||
    remaining < 0 ||
    remaining > limit ||
    resetAt < 0
  ) {
    throw new ZangpuProtocolError();
  }
  return Object.freeze({ limit, remaining, resetAt });
}

function safeErrorText(value, fallback, maxLength) {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= maxLength &&
    !/[\u0000-\u001f\u007f]/.test(value)
    ? value
    : fallback;
}

function safeResponseId(value) {
  return typeof value === "string" && SAFE_RESPONSE_ID_RE.test(value)
    ? value
    : null;
}

function apiError(response, payload = null) {
  let code = "HTTP_ERROR";
  let message = "Request failed.";
  let requestId = safeResponseId(response.headers.get("x-zangpu-request-id"));
  let retryable = response.status === 429 || response.status >= 500;
  let operationId = null;
  const error =
    payload !== null && typeof payload === "object" && !Array.isArray(payload)
      ? payload.error
      : null;
  if (error !== null && typeof error === "object" && !Array.isArray(error)) {
    code = safeErrorText(error.code, code, 64);
    message = safeErrorText(error.message, message, 512);
    requestId = safeResponseId(error.request_id) ?? requestId;
    retryable =
      typeof error.retryable === "boolean" ? error.retryable : retryable;
    operationId = safeResponseId(error.operation_id);
  }
  return new ZangpuAPIError({
    statusCode: response.status,
    code,
    message,
    requestId,
    retryable,
    operationId,
  });
}

function mapResponseError(error, pending) {
  if (error instanceof ZangpuError) {
    return error;
  }
  return new ZangpuTransportError(
    pending.timedOut() ? "REQUEST_TIMEOUT" : "REQUEST_UNAVAILABLE",
  );
}

async function readBounded(response, limit) {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > limit) {
    throw new ZangpuProtocolError();
  }
  if (response.body === null) {
    return EMPTY_BODY;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      total += value.byteLength;
      if (total > limit) {
        await reader.cancel();
        throw new ZangpuProtocolError();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function parseJsonBytes(bytes) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    const value = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("JSON object required");
    }
    return value;
  } catch {
    throw new ZangpuProtocolError();
  }
}

function stableJsonValue(value, seen) {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (Array.isArray(value)) {
    if (seen.has(value)) {
      throw new TypeError("request payload is not JSON serializable");
    }
    seen.add(value);
    const result = value.map((item) => stableJsonValue(item, seen));
    seen.delete(value);
    return result;
  }
  if (typeof value === "object") {
    if (seen.has(value)) {
      throw new TypeError("request payload is not JSON serializable");
    }
    seen.add(value);
    const result = {};
    for (const key of Object.keys(value).sort()) {
      const item = value[key];
      if (
        item === undefined ||
        typeof item === "function" ||
        typeof item === "symbol" ||
        typeof item === "bigint"
      ) {
        throw new TypeError("request payload is not JSON serializable");
      }
      result[key] = stableJsonValue(item, seen);
    }
    seen.delete(value);
    return result;
  }
  throw new TypeError("request payload is not JSON serializable");
}

function jsonBody(payload) {
  let body;
  try {
    body = encoder.encode(JSON.stringify(stableJsonValue(payload, new Set())));
  } catch (error) {
    if (error instanceof TypeError) {
      throw error;
    }
    throw new TypeError("request payload is not JSON serializable");
  }
  if (body.byteLength > MAX_REQUEST_BYTES) {
    throw new TypeError("request payload exceeds 1 MiB");
  }
  return body;
}

function chatPayload({
  model,
  messages,
  stream,
  temperature,
  topP,
  maxTokens,
  maxCompletionTokens,
  stop,
}) {
  if (typeof model !== "string" || model.length === 0 || model.length > 255) {
    throw new TypeError("model must be bounded non-empty text");
  }
  if (
    !Array.isArray(messages) ||
    messages.length < 1 ||
    messages.length > 100
  ) {
    throw new TypeError("messages must contain between 1 and 100 items");
  }
  if (
    messages.some(
      (message) =>
        message === null ||
        typeof message !== "object" ||
        Array.isArray(message),
    )
  ) {
    throw new TypeError("each message must be an object");
  }
  if (maxTokens !== undefined && maxCompletionTokens !== undefined) {
    throw new TypeError(
      "maxTokens and maxCompletionTokens are mutually exclusive",
    );
  }
  const payload = {
    model,
    messages: messages.map((message) => ({ ...message })),
    stream,
  };
  const optional = {
    temperature,
    top_p: topP,
    max_tokens: maxTokens,
    max_completion_tokens: maxCompletionTokens,
    stop,
  };
  for (const [key, value] of Object.entries(optional)) {
    if (value !== undefined) {
      payload[key] = value;
    }
  }
  return payload;
}

export class ZangpuClient {
  #baseUrl;
  #fetch;
  #signer;
  #timeoutMs;

  constructor({
    baseUrl,
    keyId,
    secret,
    timeoutMs = 30_000,
    fetch: fetchImpl = globalThis.fetch,
    ...signerOptions
  }) {
    if (
      typeof globalThis.window !== "undefined" &&
      typeof globalThis.document !== "undefined"
    ) {
      throw new TypeError("the JavaScript SDK is server-side only");
    }
    let parsed;
    try {
      parsed = new URL(baseUrl);
    } catch {
      throw new TypeError(
        "base URL must be an HTTPS origin or a loopback HTTP origin",
      );
    }
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.pathname !== "/" ||
      parsed.search !== "" ||
      parsed.hash !== "" ||
      (parsed.protocol === "http:" && !LOOPBACK_HOSTS.has(parsed.hostname))
    ) {
      throw new TypeError(
        "base URL must be an HTTPS origin or a loopback HTTP origin",
      );
    }
    if (
      !Number.isInteger(timeoutMs) ||
      timeoutMs < 100 ||
      timeoutMs > 300_000
    ) {
      throw new TypeError("timeoutMs must be between 100 and 300000");
    }
    if (typeof fetchImpl !== "function") {
      throw new TypeError("fetch must be callable");
    }
    this.#baseUrl = parsed.origin;
    this.#fetch = fetchImpl;
    this.#timeoutMs = timeoutMs;
    this.#signer = new ZangpuSigner({ keyId, secret, ...signerOptions });
  }

  async #send(
    method,
    path,
    { body = EMPTY_BODY, requestId, accept = "application/json" } = {},
  ) {
    const signed = this.#signer.sign({ method, path, body, requestId });
    const headers = {
      ...signed.headers,
      accept,
      "user-agent": `zangpu-javascript/${SDK_VERSION}`,
    };
    if (body.byteLength > 0) {
      headers["content-type"] = "application/json";
    }
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.#timeoutMs);
    try {
      const response = await this.#fetch(new URL(path, this.#baseUrl), {
        method,
        headers,
        body: body.byteLength > 0 ? body : undefined,
        redirect: "error",
        signal: controller.signal,
      });
      return Object.freeze({
        response,
        timedOut: () => timedOut,
        finish: () => clearTimeout(timer),
      });
    } catch {
      clearTimeout(timer);
      throw new ZangpuTransportError(
        timedOut ? "REQUEST_TIMEOUT" : "REQUEST_UNAVAILABLE",
      );
    }
  }

  async #requestJson(method, path, { body = EMPTY_BODY, requestId } = {}) {
    const pending = await this.#send(method, path, { body, requestId });
    const { response } = pending;
    try {
      const bytes = await readBounded(response, MAX_JSON_RESPONSE_BYTES);
      if (response.status >= 400) {
        const payload =
          contentType(response) === "application/json"
            ? (() => {
                try {
                  return parseJsonBytes(bytes);
                } catch {
                  return null;
                }
              })()
            : null;
        throw apiError(response, payload);
      }
      if (contentType(response) !== "application/json") {
        throw new ZangpuProtocolError();
      }
      return Object.freeze({
        data: parseJsonBytes(bytes),
        requestId: serverRequestId(response),
        rateLimit: rateLimit(response),
      });
    } catch (error) {
      throw mapResponseError(error, pending);
    } finally {
      pending.finish();
    }
  }

  listModels({ requestId } = {}) {
    return this.#requestJson("GET", "/api/v1/external/models", { requestId });
  }

  getUsage({ requestId } = {}) {
    return this.#requestJson("GET", "/api/v1/external/usage", { requestId });
  }

  chatCompletions(options) {
    const { requestId, ...payloadOptions } = options;
    const body = jsonBody(chatPayload({ ...payloadOptions, stream: false }));
    return this.#requestJson("POST", "/api/v1/external/chat/completions", {
      body,
      requestId,
    });
  }

  async *streamChatCompletions(options) {
    const { requestId, ...payloadOptions } = options;
    const body = jsonBody(chatPayload({ ...payloadOptions, stream: true }));
    const pending = await this.#send(
      "POST",
      "/api/v1/external/chat/completions",
      {
        body,
        requestId,
        accept: "text/event-stream",
      },
    );
    const { response } = pending;
    let reader = null;
    let doneEvent = false;
    try {
      if (response.status >= 400) {
        const bytes = await readBounded(response, MAX_JSON_RESPONSE_BYTES);
        const payload =
          contentType(response) === "application/json"
            ? (() => {
                try {
                  return parseJsonBytes(bytes);
                } catch {
                  return null;
                }
              })()
            : null;
        throw apiError(response, payload);
      }
      if (
        contentType(response) !== "text/event-stream" ||
        response.body === null
      ) {
        throw new ZangpuProtocolError();
      }
      const responseRequestId = serverRequestId(response);
      reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let buffer = "";
      let dataLines = [];
      let eventBytes = 0;
      stream: while (true) {
        const item = await reader.read();
        try {
          buffer += decoder.decode(item.done ? undefined : item.value, {
            stream: !item.done,
          });
        } catch {
          throw new ZangpuProtocolError();
        }
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const rawLine = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          const rawLineBytes = Buffer.byteLength(rawLine, "utf8") + 1;
          if (rawLineBytes > MAX_SSE_EVENT_BYTES) {
            throw new ZangpuProtocolError();
          }
          let line = rawLine;
          if (line.endsWith("\r")) {
            line = line.slice(0, -1);
          }
          if (line === "") {
            if (dataLines.length === 0) {
              eventBytes = 0;
              continue;
            }
            const eventText = dataLines.join("\n");
            dataLines = [];
            eventBytes = 0;
            if (eventText === "[DONE]") {
              doneEvent = true;
              break stream;
            }
            let event;
            try {
              event = JSON.parse(eventText);
            } catch {
              throw new ZangpuProtocolError();
            }
            if (
              event === null ||
              typeof event !== "object" ||
              Array.isArray(event)
            ) {
              throw new ZangpuProtocolError();
            }
            if ("error" in event) {
              throw apiError(response, event);
            }
            yield Object.freeze({ data: event, requestId: responseRequestId });
            continue;
          }
          if (line.startsWith(":")) {
            if (dataLines.length > 0) {
              throw new ZangpuProtocolError();
            }
            continue;
          }
          if (!line.startsWith("data:")) {
            throw new ZangpuProtocolError();
          }
          eventBytes += rawLineBytes;
          if (eventBytes > MAX_SSE_EVENT_BYTES) {
            throw new ZangpuProtocolError();
          }
          const value = line.startsWith("data: ")
            ? line.slice(6)
            : line.slice(5);
          dataLines.push(value);
        }
        if (Buffer.byteLength(buffer, "utf8") > MAX_SSE_EVENT_BYTES) {
          throw new ZangpuProtocolError();
        }
        if (item.done) {
          break;
        }
      }
      if (!doneEvent || dataLines.length > 0 || buffer !== "") {
        throw new ZangpuProtocolError();
      }
    } catch (error) {
      throw mapResponseError(error, pending);
    } finally {
      if (reader !== null) {
        try {
          await reader.cancel();
        } catch {
          // The public error is already sanitized by the surrounding request boundary.
        }
        reader.releaseLock();
      }
      pending.finish();
    }
  }

  toJSON() {
    return { baseUrl: this.#baseUrl, signer: this.#signer };
  }
}
