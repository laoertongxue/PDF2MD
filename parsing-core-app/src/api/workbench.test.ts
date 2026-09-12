import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TAURI_INTERNALS = "__TAURI_INTERNALS__";
const API_CONFIG = {
  apiBase: "http://127.0.0.1:43127",
  sessionToken: "tauri-session-token-0123456789abcdef0123456789abcdef",
};

describe("workbench API error boundaries", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(globalThis, TAURI_INTERNALS);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.doUnmock("@tauri-apps/api/core");
    vi.unstubAllGlobals();
    Reflect.deleteProperty(globalThis, TAURI_INTERNALS);
  });

  it("preserves an ordinary Tauri invoke error while the service is not ready", async () => {
    Reflect.set(globalThis, TAURI_INTERNALS, {});
    const invokeError = new Error("service not ready");
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockRejectedValue(invokeError),
    }));
    const api = await import("./workbench");

    await expect(api.listCourses()).rejects.toBe(invokeError);
    expect(api.getSafeApiErrorMessage(invokeError)).toBeNull();
  });

  it("keeps malformed service data classified as a stable protocol error", async () => {
    Reflect.set(globalThis, TAURI_INTERNALS, {});
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue(API_CONFIG),
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ unexpected: true }), { status: 200 })),
    );
    const api = await import("./workbench");

    const error = await api.listCourseCards("course-1").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(api.SafeApiError);
    expect(error).toMatchObject({ category: "protocol" });
    expect(api.getSafeApiErrorMessage(error)).toBe("服务返回数据格式异常，请稍后重试");
  });

  it("classifies a response-body deadline as a stable timeout error", async () => {
    vi.useFakeTimers();
    Reflect.set(globalThis, TAURI_INTERNALS, {});
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue(API_CONFIG),
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => new Promise(() => undefined),
      } as Response),
    );
    const api = await import("./workbench");
    let outcome: unknown = "pending";

    void api.listCourses().then(
      (value) => {
        outcome = value;
      },
      (error: unknown) => {
        outcome = error;
      },
    );
    await vi.advanceTimersByTimeAsync(30_000);

    expect(outcome).toBeInstanceOf(api.SafeApiError);
    expect(outcome).toMatchObject({ category: "timeout" });
    expect(api.getSafeApiErrorMessage(outcome)).toBe("请求超时，请重试");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("fetchEnvironment returns structured dependency states", async () => {
    Reflect.set(globalThis, TAURI_INTERNALS, {});
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue(API_CONFIG),
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            app_version: "0.1.4",
            data_dir: { path: "/tmp/data", writable: true },
            deepseek: { state: "ready", last_test_ok: null, detail_code: null },
            codex: { state: "missing", path: null, source: null, detail_code: "codex_not_found" },
            baidu: { state: "optional", masked: null, detail_code: null },
            vision: { state: "ready", detail_code: null },
          }),
          { status: 200 },
        ),
      ),
    );
    const api = await import("./workbench");

    const report = await api.fetchEnvironment();
    expect(report.codex.detail_code).toBe("codex_not_found");
  });

  it("saveCodexPath posts the selected path", async () => {
    Reflect.set(globalThis, TAURI_INTERNALS, {});
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue(API_CONFIG),
    }));
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          deepseek_model: "deepseek-v4-pro",
          deepseek_key_masked: null,
          codex_cli_path: "/opt/homebrew/bin/codex",
          baidu_key_masked: null,
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./workbench");

    const settings = await api.saveCodexPath("/opt/homebrew/bin/codex");
    expect(settings.codex_cli_path).toBe("/opt/homebrew/bin/codex");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_CONFIG.apiBase}/api/workbench/settings/codex`);
    expect(JSON.parse(String(init.body))).toEqual({ path: "/opt/homebrew/bin/codex" });
  });

  it("propagates a caller cancellation through course list requests", async () => {
    Reflect.set(globalThis, TAURI_INTERNALS, {});
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockResolvedValue(API_CONFIG),
    }));
    let fetchSignal: AbortSignal | null = null;
    const currentFetchSignal = () => fetchSignal;
    let rejectFetch: ((reason?: unknown) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        fetchSignal = init?.signal ?? null;
        return new Promise((_resolve, reject) => {
          rejectFetch = reject;
          fetchSignal?.addEventListener("abort", () => reject(fetchSignal?.reason), { once: true });
        });
      }),
    );
    const api = await import("./workbench");
    const controller = new AbortController();
    const request = api.listCourses(controller.signal);
    await vi.waitFor(() => expect(fetchSignal).not.toBeNull());

    controller.abort(new DOMException("service restarted", "AbortError"));
    await Promise.resolve();
    if (!currentFetchSignal()?.aborted) rejectFetch?.(controller.signal.reason);
    const outcome = await request.catch((error: unknown) => error);

    expect(currentFetchSignal()?.aborted).toBe(true);
    expect(outcome).toMatchObject({ category: "canceled" });
  });
});
