import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const webRoot = fileURLToPath(new URL("../..", import.meta.url));
const sourceFiles = ["src/routes/+layout.svelte", "src/routes/+page.svelte", "src/app.css"];

describe("standalone administrator console", () => {
  it("ships a Chinese-first caller-management workflow without dead navigation", () => {
    const source = sourceFiles.map((file) => readFileSync(`${webRoot}/${file}`, "utf8")).join("\n");
    expect(source).toContain("藏普 API 控制台");
    expect(source).toContain("管理员登录");
    expect(source).toContain("调用方");
    expect(source).toContain("权限与配额");
    expect(source).toContain("创建并签发凭据");
    expect(source).toContain("保存调用方 Secret");
    expect(source).not.toContain("aria-disabled");
    expect(source).not.toContain("Task 0");
    expect(source).not.toContain("待接入");
  });

  it("contains no default vendor endpoint or embedded credential", () => {
    const source = sourceFiles.map((file) => readFileSync(`${webRoot}/${file}`, "utf8")).join("\n");
    expect(source).not.toMatch(/api\.(?:openai|anthropic)\.com/i);
    expect(source).not.toMatch(/sk-[a-z0-9_-]{8,}/i);
    expect(source).not.toMatch(/x-bf-vk\s*[:=]\s*["'][^"']+/i);
  });
});
