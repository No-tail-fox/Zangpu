import { check } from "k6";
import crypto from "k6/crypto";
import exec from "k6/execution";
import http from "k6/http";
import { Counter, Rate } from "k6/metrics";
import { createSignedHeaders } from "./signing.js";

const TARGET_PATHS = {
  models: "/api/v1/external/models",
  usage: "/api/v1/external/usage",
  chat: "/api/v1/external/chat/completions",
};

const apiSuccess = new Rate("api_success");
const apiRejected = new Rate("api_rejected");
const apiServerError = new Rate("api_server_error");
const missingRequestId = new Rate("missing_request_id");
const status200 = new Counter("status_200");
const status4xx = new Counter("status_4xx");
const status5xx = new Counter("status_5xx");

function envText(name, fallback = "") {
  const value = __ENV[name];
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

function boundedInteger(name, fallback, minimum, maximum) {
  const raw = envText(name, String(fallback));
  if (!/^[0-9]+$/.test(raw)) {
    throw new Error(`${name} must be an integer.`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} is outside its allowed range.`);
  }
  return value;
}

function boundedRate(name, fallback) {
  const raw = envText(name, String(fallback));
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 0.5) {
    throw new Error(`${name} must be between 0 and 0.5.`);
  }
  return value;
}

function enumValue(name, fallback, allowed) {
  const value = envText(name, fallback).toLowerCase();
  if (!allowed.includes(value)) {
    throw new Error(`${name} is invalid.`);
  }
  return value;
}

function normalizeOrigin(value) {
  const origin = value.trim().replace(/\/+$/, "");
  const httpsOrigin = /^https:\/\/(?:\[[0-9a-f:]+\]|[a-z0-9.-]+)(?::[0-9]{1,5})?$/i;
  const loopbackOrigin = /^http:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?$/i;
  if (!httpsOrigin.test(origin) && !loopbackOrigin.test(origin)) {
    throw new Error("ZANGPU_API_BASE_URL must be an HTTPS origin or a loopback HTTP origin.");
  }
  return origin;
}

const TARGET = enumValue("ZANGPU_LOAD_TARGET", "models", ["models", "usage", "chat"]);
const PROFILE = enumValue("ZANGPU_LOAD_PROFILE", "smoke", ["smoke", "steady", "burst"]);
const MAX_FAILURE_RATE = boundedRate("ZANGPU_LOAD_MAX_FAILURE_RATE", 0.01);
const P95_MILLISECONDS = boundedInteger("ZANGPU_LOAD_P95_MS", TARGET === "chat" ? 30000 : 1500, 1, 300000);

function scenarioOptions() {
  if (PROFILE === "smoke") {
    return {
      executor: "shared-iterations",
      vus: boundedInteger("ZANGPU_LOAD_SMOKE_VUS", 1, 1, 100),
      iterations: boundedInteger("ZANGPU_LOAD_SMOKE_ITERATIONS", 10, 1, 10000),
      maxDuration: `${boundedInteger("ZANGPU_LOAD_MAX_DURATION_SECONDS", 60, 1, 3600)}s`,
    };
  }
  if (PROFILE === "steady") {
    return {
      executor: "constant-arrival-rate",
      rate: boundedInteger("ZANGPU_LOAD_RATE", 5, 1, 100000),
      timeUnit: "1s",
      duration: `${boundedInteger("ZANGPU_LOAD_DURATION_SECONDS", 30, 1, 86400)}s`,
      preAllocatedVUs: boundedInteger("ZANGPU_LOAD_PREALLOCATED_VUS", 10, 1, 10000),
      maxVUs: boundedInteger("ZANGPU_LOAD_MAX_VUS", 50, 1, 100000),
    };
  }

  const duration = boundedInteger("ZANGPU_LOAD_DURATION_SECONDS", 30, 3, 86400);
  const stageSeconds = Math.max(1, Math.floor(duration / 3));
  const startRate = boundedInteger("ZANGPU_LOAD_START_RATE", 2, 1, 100000);
  const peakRate = boundedInteger("ZANGPU_LOAD_PEAK_RATE", 20, startRate, 100000);
  return {
    executor: "ramping-arrival-rate",
    startRate,
    timeUnit: "1s",
    preAllocatedVUs: boundedInteger("ZANGPU_LOAD_PREALLOCATED_VUS", 20, 1, 10000),
    maxVUs: boundedInteger("ZANGPU_LOAD_MAX_VUS", 100, 1, 100000),
    stages: [
      { target: peakRate, duration: `${stageSeconds}s` },
      { target: peakRate, duration: `${stageSeconds}s` },
      { target: startRate, duration: `${stageSeconds}s` },
    ],
  };
}

export const options = {
  discardResponseBodies: true,
  scenarios: { signed_api: scenarioOptions() },
  thresholds: {
    api_success: [MAX_FAILURE_RATE === 0 ? "rate==1" : `rate>${1 - MAX_FAILURE_RATE}`],
    checks: [MAX_FAILURE_RATE === 0 ? "rate==1" : `rate>${1 - MAX_FAILURE_RATE}`],
    http_req_duration: [`p(95)<${P95_MILLISECONDS}`],
    http_req_failed: [MAX_FAILURE_RATE === 0 ? "rate==0" : `rate<${MAX_FAILURE_RATE}`],
    missing_request_id: ["rate<0.001"],
  },
};

const BASE_URL = envText("ZANGPU_API_BASE_URL");
const KEY_ID = envText("ZANGPU_API_KEY_ID");
const API_SECRET = envText("ZANGPU_API_SECRET");
const CHAT_MODEL = envText("ZANGPU_LOAD_MODEL");
const CHAT_PROMPT = envText("ZANGPU_LOAD_PROMPT", "请用一句话说明服务状态。");
const CHAT_MAX_TOKENS = boundedInteger("ZANGPU_LOAD_MAX_TOKENS", 64, 1, 4096);

function validateConfiguration() {
  normalizeOrigin(BASE_URL);
  if (!/^zpk_[A-Za-z0-9_-]{4,76}$/.test(KEY_ID)) {
    throw new Error("ZANGPU_API_KEY_ID is invalid.");
  }
  if (API_SECRET.length < 1 || API_SECRET.length > 4096) {
    throw new Error("ZANGPU_API_SECRET is required and must be bounded.");
  }
  if (TARGET === "chat") {
    if (__ENV.ZANGPU_LOAD_CONFIRM_CHAT !== "YES") {
      throw new Error("Chat load requires ZANGPU_LOAD_CONFIRM_CHAT=YES because it can spend credit.");
    }
    if (!CHAT_MODEL || CHAT_MODEL.length > 200) {
      throw new Error("ZANGPU_LOAD_MODEL is required for chat load.");
    }
    if (CHAT_PROMPT.length < 1 || CHAT_PROMPT.length > 4000) {
      throw new Error("ZANGPU_LOAD_PROMPT is outside its allowed range.");
    }
  }
}

export function setup() {
  validateConfiguration();
  return null;
}

function uniqueValue(prefix, purpose) {
  const entropy = [purpose, exec.vu.idInTest, exec.scenario.iterationInTest, Date.now(), Math.random()].join(":");
  return `${prefix}_${crypto.sha256(entropy, "hex").slice(0, 32)}`;
}

function requestBody() {
  if (TARGET !== "chat") {
    return "";
  }
  return JSON.stringify({
    model: CHAT_MODEL,
    messages: [{ role: "user", content: CHAT_PROMPT }],
    stream: false,
    max_tokens: CHAT_MAX_TOKENS,
  });
}

function signedHeaders(method, path, body) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = uniqueValue("nonce", "nonce");
  const requestId = uniqueValue("req", "request");
  const headers = createSignedHeaders({
    method,
    path,
    body,
    keyId: KEY_ID,
    secret: API_SECRET,
    timestamp,
    nonce,
    requestId,
    sha256Hex: (value) => crypto.sha256(value, "hex"),
    hmacSha256Hex: (key, value) => crypto.hmac("sha256", key, value, "hex"),
  });
  headers.Accept = "application/json";
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

export default function () {
  const path = TARGET_PATHS[TARGET];
  const method = TARGET === "chat" ? "POST" : "GET";
  const body = requestBody();
  const response = http.request(method, `${normalizeOrigin(BASE_URL)}${path}`, body === "" ? null : body, {
    headers: signedHeaders(method, path, body),
    redirects: 0,
    responseType: "none",
    tags: { zangpu_profile: PROFILE, zangpu_target: TARGET },
    timeout: `${boundedInteger("ZANGPU_LOAD_REQUEST_TIMEOUT_SECONDS", TARGET === "chat" ? 120 : 10, 1, 300)}s`,
  });

  const statusOk = response.status === 200;
  const serverRequestId = response.headers["X-Zangpu-Request-Id"];
  const requestIdOk = typeof serverRequestId === "string" && serverRequestId.length >= 16;
  check(response, {
    "status is 200": () => statusOk,
    "server request id is present": () => requestIdOk,
  });
  apiSuccess.add(statusOk && requestIdOk);
  apiRejected.add(response.status >= 400 && response.status < 500);
  apiServerError.add(response.status >= 500);
  missingRequestId.add(!requestIdOk);
  if (response.status === 200) status200.add(1);
  if (response.status >= 400 && response.status < 500) status4xx.add(1);
  if (response.status >= 500) status5xx.add(1);
}

function selectedValues(data, metricName, names) {
  const metric = data.metrics[metricName];
  const values = metric && metric.values ? metric.values : {};
  const selected = {};
  for (const name of names) {
    if (values[name] !== undefined && Number.isFinite(values[name])) {
      selected[name] = values[name];
    }
  }
  return selected;
}

function allThresholdsPassed(data) {
  for (const metricName of Object.keys(data.metrics)) {
    const thresholds = data.metrics[metricName].thresholds || {};
    for (const thresholdName of Object.keys(thresholds)) {
      if (!thresholds[thresholdName].ok) return false;
    }
  }
  return true;
}

function sanitizedSummary(data) {
  return {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    target: TARGET,
    profile: PROFILE,
    thresholds_passed: allThresholdsPassed(data),
    configured_thresholds: {
      max_failure_rate: MAX_FAILURE_RATE,
      p95_milliseconds: P95_MILLISECONDS,
    },
    metrics: {
      iterations: selectedValues(data, "iterations", ["count", "rate"]),
      api_success: selectedValues(data, "api_success", ["rate", "passes", "fails"]),
      api_rejected: selectedValues(data, "api_rejected", ["rate", "passes", "fails"]),
      api_server_error: selectedValues(data, "api_server_error", ["rate", "passes", "fails"]),
      missing_request_id: selectedValues(data, "missing_request_id", ["rate", "passes", "fails"]),
      checks: selectedValues(data, "checks", ["rate", "passes", "fails"]),
      http_req_duration: selectedValues(data, "http_req_duration", ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"]),
      http_req_failed: selectedValues(data, "http_req_failed", ["rate", "passes", "fails"]),
      http_reqs: selectedValues(data, "http_reqs", ["count", "rate"]),
      status_200: selectedValues(data, "status_200", ["count", "rate"]),
      status_4xx: selectedValues(data, "status_4xx", ["count", "rate"]),
      status_5xx: selectedValues(data, "status_5xx", ["count", "rate"]),
      dropped_iterations: selectedValues(data, "dropped_iterations", ["count", "rate"]),
    },
  };
}

export function handleSummary(data) {
  const summary = sanitizedSummary(data);
  const jsonPath = envText("ZANGPU_LOAD_SUMMARY_JSON", "k6-summary.json");
  const textPath = envText("ZANGPU_LOAD_SUMMARY_TEXT", "k6-summary.txt");
  const duration = summary.metrics.http_req_duration;
  const text = [
    `target=${summary.target}`,
    `profile=${summary.profile}`,
    `thresholds_passed=${summary.thresholds_passed}`,
    `iterations=${summary.metrics.iterations.count === undefined ? 0 : summary.metrics.iterations.count}`,
    `success_rate=${summary.metrics.api_success.rate === undefined ? 0 : summary.metrics.api_success.rate}`,
    `http_failure_rate=${summary.metrics.http_req_failed.rate === undefined ? 0 : summary.metrics.http_req_failed.rate}`,
    `p95_ms=${duration["p(95)"] === undefined ? 0 : duration["p(95)"]}`,
    "",
  ].join("\n");
  return {
    stdout: text,
    [jsonPath]: `${JSON.stringify(summary, null, 2)}\n`,
    [textPath]: text,
  };
}
