import { beforeEach, describe, expect, it, vi } from "vitest";

describe("runtime API endpoint", () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("keeps the browser development fallback", async () => {
    const { getApiBase } = await import("./runtime");
    await expect(getApiBase()).resolves.toBe("http://127.0.0.1:8000");
  });

  it("loads the per-instance endpoint from Tauri", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue({ apiBase: "http://127.0.0.1:43127", port: 43127 }),
    }));

    const { getApiBase } = await import("./runtime");

    await expect(getApiBase()).resolves.toBe("http://127.0.0.1:43127");
  });
});
