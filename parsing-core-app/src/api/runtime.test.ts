import { beforeEach, describe, expect, it, vi } from "vitest";

describe("runtime API endpoint", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
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

  it("refreshes the Tauri endpoint after a failed startup is retried", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi.fn()
      .mockResolvedValueOnce({ apiBase: "http://127.0.0.1:43127", port: 43127 })
      .mockResolvedValueOnce({ apiBase: "http://127.0.0.1:43128", port: 43128 });
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiBase } = await import("./runtime");

    await expect(getApiBase()).resolves.toBe("http://127.0.0.1:43127");
    await expect(getApiBase()).resolves.toBe("http://127.0.0.1:43128");
  });

  it("reads structured sidecar status and retries through Tauri", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const failed = {
      state: "failed",
      error: { category: "startup", message: "backend exited" },
      logPath: "/tmp/sidecar.log",
      port: 43127,
    };
    const invoke = vi.fn().mockResolvedValueOnce(failed).mockResolvedValueOnce("restarting");
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getServiceStatus, retryService } = await import("./runtime");

    await expect(getServiceStatus()).resolves.toEqual(failed);
    await expect(retryService()).resolves.toBeUndefined();
    expect(invoke).toHaveBeenNthCalledWith(1, "get_status");
    expect(invoke).toHaveBeenNthCalledWith(2, "retry_service");
  });

  it("reports the browser service as running only after a healthy response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: "ok" }),
    }));
    const { getServiceStatus } = await import("./runtime");

    await expect(getServiceStatus()).resolves.toMatchObject({ state: "running", port: 8000 });
    expect(fetch).toHaveBeenCalledWith("http://127.0.0.1:8000/health", expect.objectContaining({
      headers: { Accept: "application/json" },
    }));
  });

  it("reports the browser service offline when health cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const { getServiceStatus } = await import("./runtime");

    await expect(getServiceStatus()).resolves.toMatchObject({
      state: "offline",
      port: 8000,
      error: { category: "offline", message: "Failed to fetch" },
    });
  });
});
