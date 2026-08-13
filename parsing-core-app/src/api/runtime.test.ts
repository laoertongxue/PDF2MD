import { beforeEach, describe, expect, it, vi } from "vitest";

const DEV_TOKEN = "dev-session-token-0123456789abcdef0123456789abcdef";

describe("runtime API endpoint", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("requires an explicit browser development session token", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", "");
    const { getApiConfig } = await import("./runtime");

    await expect(getApiConfig()).rejects.toThrow("VITE_PDF2MD_SESSION_TOKEN");
  });

  it("returns the explicit browser development API configuration", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const { getApiConfig } = await import("./runtime");

    await expect(getApiConfig()).resolves.toEqual({
      apiBase: "http://127.0.0.1:8000",
      sessionToken: DEV_TOKEN,
    });
  });

  it("rejects unsafe browser endpoints without echoing them", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    vi.stubEnv("VITE_API_BASE_URL", `http://127.0.0.1:8000/${DEV_TOKEN}`);
    const { getApiConfig } = await import("./runtime");

    const error = await getApiConfig().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("invalid API configuration");
    expect((error as Error).message).not.toContain(DEV_TOKEN);
  });

  it("loads the ready per-instance endpoint and session from Tauri", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue({
        apiBase: "http://127.0.0.1:43127",
        sessionToken: "tauri-session-token-0123456789abcdef0123456789abcdef",
      }),
    }));
    const { getApiConfig } = await import("./runtime");

    await expect(getApiConfig()).resolves.toEqual({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "tauri-session-token-0123456789abcdef0123456789abcdef",
    });
  });

  it("cannot expose API configuration before the sidecar is ready", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockRejectedValue(new Error("service not ready")),
    }));
    const { getApiConfig } = await import("./runtime");

    await expect(getApiConfig()).rejects.toThrow("service not ready");
  });

  it("refreshes the Tauri configuration after a sidecar restart", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi
      .fn()
      .mockResolvedValueOnce({
        apiBase: "http://127.0.0.1:43127",
        sessionToken: "first-session-token-0123456789abcdef0123456789abcdef",
      })
      .mockResolvedValueOnce({
        apiBase: "http://127.0.0.1:43128",
        sessionToken: "second-session-token-0123456789abcdef0123456789abcdef",
      });
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiConfig } = await import("./runtime");

    await expect(getApiConfig()).resolves.toMatchObject({ apiBase: "http://127.0.0.1:43127" });
    await expect(getApiConfig()).resolves.toMatchObject({ apiBase: "http://127.0.0.1:43128" });
  });

  it("sends the session header for browser health checks", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: "ok" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { getServiceStatus } = await import("./runtime");

    await expect(getServiceStatus()).resolves.toMatchObject({ state: "running", port: 8000 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/health");
    expect(new Headers(init.headers).get("X-PDF2MD-Session")).toBe(DEV_TOKEN);
  });

  it("reports the browser service offline when health cannot be reached", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const { getServiceStatus } = await import("./runtime");

    await expect(getServiceStatus()).resolves.toMatchObject({
      state: "offline",
      port: 8000,
      error: { category: "offline", message: "Failed to fetch" },
    });
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
});
