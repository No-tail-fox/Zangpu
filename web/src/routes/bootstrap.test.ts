import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const webRoot = fileURLToPath(new URL("../..", import.meta.url));
const sourceFiles = ["src/routes/+layout.svelte", "src/routes/+page.svelte", "src/app.css"];

describe("standalone operator shell", () => {
  it("ships a Chinese-first operations surface", () => {
    const source = sourceFiles.map((file) => readFileSync(`${webRoot}/${file}`, "utf8")).join("\n");
    expect(source).toContain("藏普 API 控制台");
    expect(source).toContain("调用方");
    expect(source).toContain("系统状态");
  });

  it("contains no default vendor endpoint or embedded credential", () => {
    const source = sourceFiles.map((file) => readFileSync(`${webRoot}/${file}`, "utf8")).join("\n");
    expect(source).not.toMatch(/api\.(?:openai|anthropic)\.com/i);
    expect(source).not.toMatch(/sk-[a-z0-9_-]{8,}/i);
    expect(source).not.toMatch(/x-bf-vk\s*[:=]\s*["'][^"']+/i);
  });
});
