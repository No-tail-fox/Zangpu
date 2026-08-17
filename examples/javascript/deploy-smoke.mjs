import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ZangpuClient } from "../../sdk/javascript/src/index.js";

const MAX_HEALTH_BYTES = 64 * 1024;

function requiredEnvironment(env, name) {
  const value = env[name];
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${name} is required`);
  }
  return value;
}

async function boundedJson(response, limit) {
  if (
    !response.ok ||
    (response.headers.get("content-type") ?? "").split(";", 1)[0].trim() !==
      "application/json"
  ) {
    throw new Error("DEPLOY_SMOKE_HEALTH_FAILED");
  }
  const reader = response.body?.getReader();
  if (reader === undefined) {
    throw new Error("DEPLOY_SMOKE_HEALTH_FAILED");
  }
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
        throw new Error("DEPLOY_SMOKE_HEALTH_FAILED");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const value = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    );
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("invalid health payload");
    }
    return value;
  } catch {
    throw new Error("DEPLOY_SMOKE_HEALTH_FAILED");
  }
}

export async function runDeploymentSmoke({
  env = process.env,
  fetch: fetchImpl = globalThis.fetch,
} = {}) {
  const baseUrl = requiredEnvironment(env, "ZANGPU_API_BASE_URL");
  const client = new ZangpuClient({
    baseUrl,
    keyId: requiredEnvironment(env, "ZANGPU_API_KEY_ID"),
    secret: requiredEnvironment(env, "ZANGPU_API_SECRET"),
    fetch: fetchImpl,
  });
  const healthResponse = await fetchImpl(
    new URL("/api/v1/external/health", baseUrl),
    {
      method: "GET",
      headers: {
        accept: "application/json",
        "user-agent": "zangpu-deploy-smoke/0.1.0",
      },
      redirect: "error",
      signal: AbortSignal.timeout(10_000),
    },
  );
  const health = await boundedJson(healthResponse, MAX_HEALTH_BYTES);
  const models = await client.listModels();
  const usage = await client.getUsage();

  if (
    health.status !== "ready" ||
    typeof health.version !== "string" ||
    !Array.isArray(models.data.data) ||
    !Number.isSafeInteger(usage.data.as_of)
  ) {
    throw new Error("DEPLOY_SMOKE_CONTRACT_FAILED");
  }
  return Object.freeze({
    status: health.status,
    serviceVersion: health.version,
    modelCount: models.data.data.length,
    usageAsOf: usage.data.as_of,
  });
}

const isMain =
  process.argv[1] !== undefined &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  runDeploymentSmoke()
    .then((summary) => console.log(JSON.stringify(summary)))
    .catch((error) => {
      const code =
        typeof error?.code === "string" ? error.code : "DEPLOY_SMOKE_FAILED";
      console.error(JSON.stringify({ ok: false, code }));
      process.exitCode = 1;
    });
}
