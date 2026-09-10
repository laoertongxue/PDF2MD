import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiConfig } from "./runtime";

const DEV_TOKEN = "dev-session-token-0123456789abcdef0123456789abcdef";

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
}

describe("runtime API endpoint", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  afterEach(() => {
    vi.doUnmock("@tauri-apps/api/core");
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

  it("caps unresolved raw Tauri configuration invocations at two", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi.fn().mockImplementation(() => new Promise<ApiConfig>(() => undefined));
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiConfig } = await import("./runtime");
    const controllers = [new AbortController(), new AbortController(), new AbortController()];

    const requests = [getApiConfig(controllers[0]!.signal)];
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    requests.push(getApiConfig(controllers[1]!.signal));
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    requests.push(getApiConfig(controllers[2]!.signal));
    requests.forEach((request) => void request.catch(() => undefined));
    await flushMicrotasks();
    expect(invoke).toHaveBeenCalled();
    expect(invoke.mock.calls.length).toBeLessThanOrEqual(2);
    controllers.forEach((controller) => controller.abort());
    await Promise.allSettled(requests);
  });

  it("discards an orphaned configuration that settles after a newer session", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const resolvers: Array<(value: ApiConfig) => void> = [];
    const invoke = vi.fn().mockImplementation(
      () =>
        new Promise<ApiConfig>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiConfig } = await import("./runtime");
    const controller = new AbortController();
    const reason = new DOMException("deadline reached", "TimeoutError");
    const firstRequest = getApiConfig(controller.signal);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    controller.abort(reason);
    await expect(firstRequest).rejects.toBe(reason);

    const secondRequest = getApiConfig();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    resolvers[1]?.({
      apiBase: "http://127.0.0.1:43128",
      sessionToken: "second-session-token-0123456789abcdef0123456789abcdef",
    });
    await expect(secondRequest).resolves.toMatchObject({ apiBase: "http://127.0.0.1:43128" });

    resolvers[0]?.({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "orphaned-session-token-0123456789abcdef0123456789abcdef",
    });
    await flushMicrotasks();
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    const thirdRequest = getApiConfig();
    await flushMicrotasks();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(invoke).toHaveBeenCalledTimes(3);
    resolvers[2]?.({
      apiBase: "http://127.0.0.1:43129",
      sessionToken: "third-session-token-0123456789abcdef0123456789abcdef",
    });
    await expect(thirdRequest).resolves.toMatchObject({ apiBase: "http://127.0.0.1:43129" });
  });

  it("never delivers an older non-orphaned configuration after a newer invocation succeeds", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const resolvers: Array<(value: ApiConfig) => void> = [];
    const invoke = vi.fn().mockImplementation(
      () =>
        new Promise<ApiConfig>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiConfig } = await import("./runtime");
    let firstOutcome: ApiConfig | "pending" = "pending";

    void getApiConfig().then((config) => {
      firstOutcome = config;
    });
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    const secondRequest = getApiConfig();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));

    const secondConfig = {
      apiBase: "http://127.0.0.1:43128",
      sessionToken: "second-session-token-0123456789abcdef0123456789abcdef",
    };
    resolvers[1]?.(secondConfig);
    await expect(secondRequest).resolves.toEqual(secondConfig);
    await flushMicrotasks();
    expect(firstOutcome).toEqual(secondConfig);

    resolvers[0]?.({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "stale-session-token-0123456789abcdef0123456789abcdef",
    });
    await flushMicrotasks();
    expect(firstOutcome).toEqual(secondConfig);

    const thirdRequest = getApiConfig();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(3));
    const thirdConfig = {
      apiBase: "http://127.0.0.1:43129",
      sessionToken: "third-session-token-0123456789abcdef0123456789abcdef",
    };
    resolvers[2]?.(thirdConfig);
    await expect(thirdRequest).resolves.toEqual(thirdConfig);
  });

  it("hard-caps combined API and WebSocket configuration waiters and releases the quota", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi.fn().mockImplementation(() => new Promise<ApiConfig>(() => undefined));
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const runtime = await import("./runtime");
    const controllers = Array.from({ length: 300 }, () => new AbortController());
    const requests: Array<Promise<unknown>> = [runtime.getApiConfig(controllers[0]!.signal)];
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    requests.push(runtime.getApiConfig(controllers[1]!.signal));
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    for (let index = 2; index < controllers.length; index += 1) {
      const controller = controllers[index]!;
      requests.push(index % 2 === 0 ? runtime.getApiConfig(controller.signal) : runtime.getWsConfig(controller.signal));
    }
    const settledRequests = Promise.allSettled(requests);

    expect(invoke.mock.calls.length).toBeLessThanOrEqual(2);
    expect(runtime.__getApiConfigWaiterSnapshotForTests()).toEqual({
      active: 128,
      invocation: 128,
      slot: 0,
    });

    controllers.forEach((controller, index) =>
      controller.abort(
        new DOMException(
          index % 2 === 0 ? "request cancelled" : "deadline reached",
          index % 2 === 0 ? "AbortError" : "TimeoutError",
        ),
      ),
    );
    const results = await settledRequests;
    const busy = results.filter(
      (result) => result.status === "rejected" && result.reason instanceof runtime.ApiConfigBusyError,
    );
    const canceled = results.filter(
      (result) =>
        result.status === "rejected" &&
        result.reason instanceof DOMException &&
        ["AbortError", "TimeoutError"].includes(result.reason.name),
    );

    expect(busy).toHaveLength(172);
    expect(canceled).toHaveLength(128);
    expect(runtime.__getApiConfigWaiterSnapshotForTests()).toEqual({
      active: 0,
      invocation: 0,
      slot: 0,
    });
  });

  it("waits for a slot instead of attaching to two orphaned invocations", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const resolvers: Array<(value: ApiConfig) => void> = [];
    const invoke = vi.fn().mockImplementation(
      () =>
        new Promise<ApiConfig>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiConfig } = await import("./runtime");
    const first = new AbortController();
    const second = new AbortController();
    const firstRequest = getApiConfig(first.signal);
    void firstRequest.catch(() => undefined);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    const secondRequest = getApiConfig(second.signal);
    void secondRequest.catch(() => undefined);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    const abandoned = [firstRequest, secondRequest];
    first.abort();
    second.abort();
    await Promise.allSettled(abandoned);
    let outcome: unknown = "pending";

    void getApiConfig().then(
      (value) => {
        outcome = value;
      },
      (error: unknown) => {
        outcome = error;
      },
    );
    resolvers[0]?.({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "stale-session-token-0123456789abcdef0123456789abcdef",
    });
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(outcome).toBe("pending");
    expect(invoke).toHaveBeenCalledTimes(3);
    resolvers[2]?.({
      apiBase: "http://127.0.0.1:43129",
      sessionToken: "fresh-session-token-0123456789abcdef0123456789abcdef",
    });
    await vi.waitFor(() => expect(outcome).toMatchObject({ apiBase: "http://127.0.0.1:43129" }));
    resolvers[1]?.({
      apiBase: "http://127.0.0.1:43128",
      sessionToken: "other-stale-token-0123456789abcdef0123456789abcdef",
    });
  });

  it("never attaches a new waiter below the highest pending invocation sequence", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const resolvers: Array<(value: ApiConfig) => void> = [];
    const invoke = vi.fn().mockImplementation(
      () =>
        new Promise<ApiConfig>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getApiConfig } = await import("./runtime");
    const firstRequest = getApiConfig();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    const secondController = new AbortController();
    const secondRequest = getApiConfig(secondController.signal);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    secondController.abort(new DOMException("second waiter left", "AbortError"));
    await expect(secondRequest).rejects.toMatchObject({ name: "AbortError" });
    let thirdOutcome: ApiConfig | "pending" = "pending";

    void getApiConfig().then((config) => {
      thirdOutcome = config;
    });
    resolvers[0]?.({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "first-session-token-0123456789abcdef0123456789abcdef",
    });
    await expect(firstRequest).resolves.toMatchObject({ apiBase: "http://127.0.0.1:43127" });
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(3));
    expect(thirdOutcome).toBe("pending");

    const thirdConfig = {
      apiBase: "http://127.0.0.1:43129",
      sessionToken: "third-session-token-0123456789abcdef0123456789abcdef",
    };
    resolvers[2]?.(thirdConfig);
    await vi.waitFor(() => expect(thirdOutcome).toEqual(thirdConfig));
    resolvers[1]?.({
      apiBase: "http://127.0.0.1:43128",
      sessionToken: "orphaned-session-token-0123456789abcdef0123456789abcdef",
    });
  });

  it("cancels a WebSocket configuration waiter with the caller signal", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi.fn().mockImplementation(() => new Promise<ApiConfig>(() => undefined));
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const { getWsConfig } = await import("./runtime");
    const controller = new AbortController();
    const reason = new DOMException("socket disconnected", "AbortError");
    let outcome: unknown = "pending";

    void getWsConfig(controller.signal).then(
      (value) => {
        outcome = value;
      },
      (error: unknown) => {
        outcome = error;
      },
    );
    await flushMicrotasks();
    controller.abort(reason);
    await flushMicrotasks();

    expect(outcome).toBe(reason);
    expect(invoke).toHaveBeenCalledOnce();
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

  it("passes through exit cleanup and force-quit command results in Tauri", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi.fn().mockResolvedValueOnce("exit_requested").mockResolvedValueOnce("force_exit_cancelled");
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const runtime = await import("./runtime");

    await expect(runtime.retryExitCleanup()).resolves.toBe("exit_requested");
    await expect(runtime.requestForceExitConfirmation()).resolves.toBe("force_exit_cancelled");
    expect(invoke).toHaveBeenNthCalledWith(1, "retry_exit_cleanup");
    expect(invoke).toHaveBeenNthCalledWith(2, "request_force_exit_confirmation");
  });

  it("passes through exit recovery command errors without rewriting them", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const cleanupError = new Error("cleanup failed");
    const confirmationError = { message: "dialog failed" };
    const invoke = vi.fn().mockRejectedValueOnce(cleanupError).mockRejectedValueOnce(confirmationError);
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const runtime = await import("./runtime");

    await expect(runtime.retryExitCleanup()).rejects.toBe(cleanupError);
    await expect(runtime.requestForceExitConfirmation()).rejects.toBe(confirmationError);
  });

  it("does not invoke native exit recovery commands in a browser", async () => {
    const invoke = vi.fn();
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    const runtime = await import("./runtime");

    await expect(runtime.retryExitCleanup()).resolves.toBeUndefined();
    await expect(runtime.requestForceExitConfirmation()).resolves.toBeUndefined();
    expect(invoke).not.toHaveBeenCalled();
  });
});
