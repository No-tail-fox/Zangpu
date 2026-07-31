import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const buildRoot = fileURLToPath(new URL("../build", import.meta.url));
if (!existsSync(buildRoot)) throw new Error("Web build output is missing");

function collectFiles(path) {
  return readdirSync(path, { withFileTypes: true }).flatMap((entry) => {
    const child = join(path, entry.name);
    return entry.isDirectory() ? collectFiles(child) : [child];
  });
}

const text = collectFiles(buildRoot)
  .filter((file) => statSync(file).size < 5_000_000)
  .map((file) => readFileSync(file, "utf8"))
  .join("\n");

const forbidden = [/api\.(?:openai|anthropic)\.com/i, /sk-[a-z0-9_-]{8,}/i, /x-bf-vk\s*[:=]\s*["'][^"']+/i];
for (const pattern of forbidden) {
  if (pattern.test(text)) throw new Error(`Forbidden vendor or credential marker found: ${pattern}`);
}

console.log("Build output contains no default vendor URL or embedded credential.");
