import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";

import { runDeploymentSmoke } from "../../../examples/javascript/deploy-smoke.mjs";
import {
  MAX_JSON_RESPONSE_BYTES,
  MAX_SSE_EVENT_BYTES,
  ZangpuAPIError,
  ZangpuClient,
  ZangpuProtocolError,
  ZangpuSigner,
  ZangpuTransportError,
  canonicalizePath,
  canonicalizeQuery,
  createCanonicalRequest,
} from "../src/index.js";

const SECRET = "zps_javascript_test_material_0123456789";
const KEY_ID = "zpk_javascript_0123456789";
const FROZEN_BODY = new TextEncoder().encode(
  '{"model":"zangpu-test","messages":[{"role":"user","content":"hello"}],"stream":false,"max_tokens":64}',
);
const FROZEN_SIGNATURE =
  "121118be99e3276c168066f7a10b12cd4d395a13ecbb843dbb545363595decfe";

function responseHeaders(contentType = "application/json") {
  return {
    "content-type": contentType,
    "x-zangpu-request-id": "req_server_javascript_0123456789",
    "x-ratelimit-limit": "10",
    "x-ratelimit-remaining": "9",
    "x-ratelimit-reset": "1785420001",
  };
}

function client(fetchImpl) {
  let sequence = 0;
  return new ZangpuClient({
    baseUrl: "http://127.0.0.1:9000",
    keyId: KEY_ID,
    secret: SECRET,
    fetch: fetchImpl,
    clock: () => 1_785_420_000,
    nonceFactory: () => `nonce_${String(++sequence).padStart(16, "0")}`,
    requestIdFactory: () => `req_${String(sequence).padStart(16, "0")}`,
  });
}

test("signer matches the frozen backend vector and RFC3986 normalization", () => {
  const signer = new ZangpuSigner({
    keyId: "zpk_test_0123456789",
    secret: "zps_test_secret_0123456789",
    clock: () => 1_785_420_000,
    nonceFactory: () => "nonce_0123456789abcdef",
    requestIdFactory: () => "req_0123456789abcdef",
  });
  const signed = signer.sign({
    method: "POST",
    path: "/api/v1/external/chat/completions",
    body: FROZEN_BODY,
  });

  assert.equal(signed.signature, FROZEN_SIGNATURE);
  assert.equal(signed.headers["x-zangpu-signature-version"], "1");
  assert.equal(canonicalizePath("/v1/%e8%97%8f/%2f"), "/v1/%E8%97%8F/%2F");
  assert.equal(
    canonicalizeQuery("b=2&a=z&a=b&plus=hello+world"),
    "a=b&a=z&b=2&plus=hello%2Bworld",
  );
  assert.throws(() => canonicalizePath("/a/%252F/b"));
  assert.throws(() =>
    createCanonicalRequest({
      method: "GET",
      path: "/api/v1/external/models",
      bodyHash: "0".repeat(64),
      keyId: "zpk_test_0123456789",
      timestamp: 1_785_420_000,
      nonce: "nonce_0123456789abcdef",
      requestId: "req_0123456789abcdef",
    }),
  );
  assert.doesNotMatch(
    JSON.stringify(signer),
    /zps_test_secret|121118be|nonce_0123/,
  );
});

test("client calls models, usage and JSON chat with exact signed bytes and fresh nonces", async () => {
  const requests = [];
  const fetchImpl = async (url, init) => {
    const headers = new Headers(init.headers);
    const body =
      init.body === undefined ? new Uint8Array() : new Uint8Array(init.body);
    const requestUrl = new URL(url);
    const canonical = createCanonicalRequest({
      method: init.method,
      path: requestUrl.pathname,
      query: requestUrl.search.slice(1),
      bodyHash: await crypto.subtle
        .digest("SHA-256", body)
        .then((value) => Buffer.from(value).toString("hex")),
      keyId: headers.get("x-zangpu-key"),
      timestamp: headers.get("x-zangpu-timestamp"),
      nonce: headers.get("x-zangpu-nonce"),
      requestId: headers.get("x-zangpu-request-id"),
    });
    const expected = createHmac("sha256", SECRET)
      .update(canonical)
      .digest("hex");
    assert.equal(headers.get("x-zangpu-signature"), expected);
    requests.push({
      method: init.method,
      path: requestUrl.pathname,
      nonce: headers.get("x-zangpu-nonce"),
      body,
    });

    const data = requestUrl.pathname.endsWith("/models")
      ? { object: "list", data: [{ id: "model-1", object: "model" }] }
      : requestUrl.pathname.endsWith("/usage")
        ? { object: "usage", as_of: 1_785_420_000, daily: {}, lifetime: {} }
        : {
            id: "chatcmpl-js-1",
            model: "model-1",
            choices: [],
            usage: { total_tokens: 8 },
          };
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: responseHeaders(),
    });
  };
  const sdk = client(fetchImpl);

  const models = await sdk.listModels();
  const usage = await sdk.getUsage();
  const chat = await sdk.chatCompletions({
    model: "model-1",
    messages: [{ role: "user", content: "hello" }],
    maxTokens: 64,
    requestId: "req_javascript_chat_0001",
  });

  assert.equal(models.data.data[0].id, "model-1");
  assert.equal(usage.data.object, "usage");
  assert.equal(chat.data.usage.total_tokens, 8);
  assert.equal(chat.requestId, "req_server_javascript_0123456789");
  assert.equal(chat.rateLimit.remaining, 9);
  assert.deepEqual(
    requests.map(({ method, path }) => [method, path]),
    [
      ["GET", "/api/v1/external/models"],
      ["GET", "/api/v1/external/usage"],
      ["POST", "/api/v1/external/chat/completions"],
    ],
  );
  assert.equal(new Set(requests.map(({ nonce }) => nonce)).size, 3);
  assert.equal(
    new TextDecoder().decode(requests[2].body),
    '{"max_tokens":64,"messages":[{"content":"hello","role":"user"}],"model":"model-1","stream":false}',
  );
});

test("client parses SSE heartbeats and requires a final DONE event", async () => {
  let missingDone = false;
  const sdk = client(async () => {
    const suffix = missingDone ? "" : "data: [DONE]\n\n";
    return new Response(
      `: heartbeat\n\ndata: {"id":"chunk-1","choices":[{"delta":{"content":"answer"}}]}\n\n${suffix}`,
      {
        status: 200,
        headers: responseHeaders("text/event-stream; charset=utf-8"),
      },
    );
  });

  const events = [];
  for await (const event of sdk.streamChatCompletions({
    model: "model-1",
    messages: [{ role: "user", content: "hello" }],
  })) {
    events.push(event);
  }
  assert.equal(events.length, 1);
  assert.equal(events[0].data.id, "chunk-1");
  assert.equal(events[0].requestId, "req_server_javascript_0123456789");

  missingDone = true;
  await assert.rejects(async () => {
    for await (const _event of sdk.streamChatCompletions({
      model: "model-1",
      messages: [{ role: "user", content: "hello" }],
    })) {
      // Drain the stream to force terminal validation.
    }
  }, ZangpuProtocolError);
});

test("client bounds pending SSE lines before requesting more stream data", async () => {
  let pulls = 0;
  const stream = new ReadableStream(
    {
      pull(controller) {
        pulls += 1;
        if (pulls === 1) {
          controller.enqueue(
            new TextEncoder().encode(
              `data: ${"x".repeat(MAX_SSE_EVENT_BYTES)}`,
            ),
          );
          return;
        }
        controller.enqueue(new TextEncoder().encode("\n\n"));
        controller.close();
      },
    },
    { highWaterMark: 0 },
  );
  const sdk = client(
    async () =>
      new Response(stream, {
        status: 200,
        headers: responseHeaders("text/event-stream"),
      }),
  );

  await assert.rejects(async () => {
    for await (const _event of sdk.streamChatCompletions({
      model: "model-1",
      messages: [{ role: "user", content: "hello" }],
    })) {
      // Drain the stream to force bounded parsing.
    }
  }, ZangpuProtocolError);
  assert.equal(pulls, 1);
});

test("client counts raw multiline SSE frames against the event limit", async () => {
  const padding = "data:\n".repeat(Math.ceil(MAX_SSE_EVENT_BYTES / 6));
  const sdk = client(
    async () =>
      new Response(
        `data: {\n${padding}data: "id":"oversized"}\n\ndata: [DONE]\n\n`,
        {
          status: 200,
          headers: responseHeaders("text/event-stream"),
        },
      ),
  );

  await assert.rejects(async () => {
    for await (const _event of sdk.streamChatCompletions({
      model: "model-1",
      messages: [{ role: "user", content: "hello" }],
    })) {
      // Drain the stream to force bounded parsing.
    }
  }, ZangpuProtocolError);
});

test("client rejects an oversized SSE heartbeat line", async () => {
  const sdk = client(
    async () =>
      new Response(`:${"x".repeat(MAX_SSE_EVENT_BYTES)}\n\ndata: [DONE]\n\n`, {
        status: 200,
        headers: responseHeaders("text/event-stream"),
      }),
  );

  await assert.rejects(async () => {
    for await (const _event of sdk.streamChatCompletions({
      model: "model-1",
      messages: [{ role: "user", content: "hello" }],
    })) {
      // Drain the stream to force bounded parsing.
    }
  }, ZangpuProtocolError);
});

test("client errors are bounded and never expose raw bodies or signing material", async () => {
  const rawDetail = "private upstream detail";
  const sdk = client(
    async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "MODEL_UNAVAILABLE",
            message: "Model is temporarily unavailable.",
            request_id: "req_server_error_0123456789",
            retryable: true,
            raw_detail: rawDetail,
          },
        }),
        { status: 503, headers: responseHeaders() },
      ),
  );

  await assert.rejects(sdk.listModels(), (error) => {
    assert.ok(error instanceof ZangpuAPIError);
    assert.equal(error.code, "MODEL_UNAVAILABLE");
    assert.equal(error.retryable, true);
    assert.doesNotMatch(String(error), new RegExp(`${rawDetail}|${SECRET}`));
    return true;
  });

  const oversized = client(
    async () =>
      new Response(new Uint8Array(MAX_JSON_RESPONSE_BYTES + 1), {
        status: 200,
        headers: responseHeaders(),
      }),
  );
  await assert.rejects(oversized.listModels(), ZangpuProtocolError);
});

test("response stream failures are mapped to sanitized transport errors", async () => {
  const rawDetail = "private socket failure with upstream address";
  const sdk = client(
    async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.error(new Error(rawDetail));
          },
        }),
        { status: 200, headers: responseHeaders() },
      ),
  );

  await assert.rejects(sdk.listModels(), (error) => {
    assert.ok(error instanceof ZangpuTransportError);
    assert.equal(error.code, "REQUEST_UNAVAILABLE");
    assert.doesNotMatch(String(error), new RegExp(`${rawDetail}|${SECRET}`));
    return true;
  });
});

test("request timeout remains active while the response body is being consumed", async () => {
  const rawDetail = "private slow upstream body";
  const sdk = new ZangpuClient({
    baseUrl: "http://127.0.0.1:9000",
    keyId: KEY_ID,
    secret: SECRET,
    timeoutMs: 100,
    fetch: async (_url, init) =>
      new Response(
        new ReadableStream({
          start(controller) {
            init.signal.addEventListener(
              "abort",
              () => controller.error(new Error(rawDetail)),
              { once: true },
            );
          },
        }),
        { status: 200, headers: responseHeaders() },
      ),
  });

  await assert.rejects(sdk.listModels(), (error) => {
    assert.ok(error instanceof ZangpuTransportError);
    assert.equal(error.code, "REQUEST_TIMEOUT");
    assert.doesNotMatch(String(error), new RegExp(`${rawDetail}|${SECRET}`));
    return true;
  });
});

test("client rejects credentialed origins and public plain HTTP", () => {
  assert.throws(
    () =>
      new ZangpuClient({
        baseUrl: "https://user@example.com",
        keyId: KEY_ID,
        secret: SECRET,
      }),
  );
  assert.throws(
    () =>
      new ZangpuClient({
        baseUrl: "http://api.example.com",
        keyId: KEY_ID,
        secret: SECRET,
      }),
  );
});

test("deployment smoke calls only health, models and usage and returns sanitized evidence", async () => {
  const paths = [];
  const summary = await runDeploymentSmoke({
    env: {
      ZANGPU_API_BASE_URL: "http://127.0.0.1:9000",
      ZANGPU_API_KEY_ID: KEY_ID,
      ZANGPU_API_SECRET: SECRET,
    },
    fetch: async (url) => {
      const path = new URL(url).pathname;
      paths.push(path);
      if (path.endsWith("/health")) {
        return new Response(
          JSON.stringify({ status: "ready", version: "0.1.0" }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      }
      const payload = path.endsWith("/models")
        ? { object: "list", data: [{ id: "model-1", object: "model" }] }
        : { object: "usage", as_of: 1_785_420_000, daily: {}, lifetime: {} };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: responseHeaders(),
      });
    },
  });

  assert.deepEqual(paths, [
    "/api/v1/external/health",
    "/api/v1/external/models",
    "/api/v1/external/usage",
  ]);
  assert.deepEqual(summary, {
    status: "ready",
    serviceVersion: "0.1.0",
    modelCount: 1,
    usageAsOf: 1_785_420_000,
  });
  assert.doesNotMatch(JSON.stringify(summary), /zps_|signature|nonce/i);
});
