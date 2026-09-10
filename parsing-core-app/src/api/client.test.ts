import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const DEV_TOKEN = "dev-session-token-0123456789abcdef0123456789abcdef";

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
}

describe("authenticated API client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("requires every API call to provide an explicit response reader", async () => {
    const { apiFetch } = await import("./client");
    const invalidCall = () => {
      // @ts-expect-error API calls must declare how the response body is consumed.
      return apiFetch("/api/workbench/courses");
    };

    expect(invalidCall).toBeTypeOf("function");
  });

  it("adds the session header without placing the secret in the URL", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) });
    vi.stubGlobal("fetch", fetchMock);
    const { listBatches } = await import("./client");

    await listBatches();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/api/batches");
    expect(url).not.toContain(DEV_TOKEN);
    expect(new Headers(init.headers).get("X-PDF2MD-Session")).toBe(DEV_TOKEN);
  });

  it("preserves content headers while adding the session", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ batch_id: "batch-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { createBatch } = await import("./client");

    await createBatch(["/tmp/book.pdf"]);

    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-PDF2MD-Session")).toBe(DEV_TOKEN);
  });

  it("does not send a request before Tauri exposes ready configuration", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    let resolveConfig: ((value: unknown) => void) | undefined;
    const config = new Promise((resolve) => {
      resolveConfig = resolve;
    });
    vi.doMock("@tauri-apps/api/core", () => ({ invoke: vi.fn().mockReturnValue(config) }));
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) });
    vi.stubGlobal("fetch", fetchMock);
    const { listBatches } = await import("./client");

    const request = listBatches();
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();

    resolveConfig?.({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "tauri-session-token-0123456789abcdef0123456789abcdef",
    });
    await request;
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("aborts a hung request after the default 30 second timeout", async () => {
    vi.useFakeTimers();
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      const signal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { apiFetch } = await import("./client");

    const request = apiFetch("/api/workbench/courses", undefined, () => undefined);
    await flushMicrotasks();
    const signal = (fetchMock.mock.calls[0]?.[1] as RequestInit | undefined)?.signal;
    expect(signal).toBeInstanceOf(AbortSignal);
    const rejection = expect(request).rejects.toMatchObject({
      name: "TimeoutError",
      message: "API 请求超时，请重试",
    });

    await vi.advanceTimersByTimeAsync(29_999);
    expect(signal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    expect(signal?.aborted).toBe(true);
    await rejection;
  });

  it("honors a caller abort signal while the default timeout is active", async () => {
    vi.useFakeTimers();
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      const signal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { apiFetch } = await import("./client");
    const caller = new AbortController();
    const reason = new DOMException("caller canceled", "AbortError");

    const request = apiFetch("/api/workbench/courses", { signal: caller.signal }, () => undefined);
    await flushMicrotasks();
    const signal = (fetchMock.mock.calls[0]?.[1] as RequestInit | undefined)?.signal;

    caller.abort(reason);

    expect(signal?.aborted).toBe(true);
    expect(signal?.reason).toBe(reason);
    await expect(request).rejects.toBe(reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out configuration loading and caps unresolved raw Tauri invocations", async () => {
    vi.useFakeTimers();
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const invoke = vi.fn().mockImplementation(() => new Promise(() => undefined));
    vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
    vi.stubGlobal("fetch", vi.fn());
    const { apiFetch } = await import("./client");
    const outcomes: unknown[] = [];

    const startRequest = (index: number) => {
      void apiFetch(`/api/workbench/courses?request=${index}`, undefined, () => undefined).then(
        (value) => outcomes.push(value),
        (error: unknown) => outcomes.push(error),
      );
    };
    startRequest(0);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    startRequest(1);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    startRequest(2);
    await flushMicrotasks();
    expect(invoke).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30_000);

    expect(outcomes).toHaveLength(3);
    expect(outcomes).toEqual([
      expect.objectContaining({ name: "TimeoutError", message: "API 请求超时，请重试" }),
      expect.objectContaining({ name: "TimeoutError", message: "API 请求超时，请重试" }),
      expect.objectContaining({ name: "TimeoutError", message: "API 请求超时，请重试" }),
    ]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("uses one absolute deadline across delayed configuration and response body reading", async () => {
    vi.useFakeTimers();
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    vi.doMock("@tauri-apps/api/core", () => ({
      invoke: vi.fn().mockImplementation(
        () =>
          new Promise((resolve) => {
            window.setTimeout(() => {
              resolve({
                apiBase: "http://127.0.0.1:43127",
                sessionToken: "tauri-session-token-0123456789abcdef0123456789abcdef",
              });
            }, 10_000);
          }),
      ),
    }));
    const requestSignals: AbortSignal[] = [];
    const json = vi.fn().mockImplementation(() => new Promise(() => undefined));
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        if (init?.signal) requestSignals.push(init.signal);
        return Promise.resolve({ ok: true, status: 200, json } as unknown as Response);
      }),
    );
    const { apiFetch } = await import("./client");
    let outcome: unknown = "pending";

    void apiFetch("/api/workbench/courses", {}, (response) => response.json()).then(
      (value) => {
        outcome = value;
      },
      (error: unknown) => {
        outcome = error;
      },
    );
    await vi.advanceTimersByTimeAsync(10_000);
    expect(json).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(19_999);
    expect(outcome).toBe("pending");
    expect(requestSignals[0]?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    expect(outcome).toMatchObject({ name: "TimeoutError", message: "API 请求超时，请重试" });
    expect(requestSignals[0]?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("preserves a caller abort reason while reading the response body", async () => {
    vi.useFakeTimers();
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const json = vi.fn().mockImplementation(() => new Promise(() => undefined));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json } as unknown as Response));
    const { apiFetch } = await import("./client");
    const caller = new AbortController();
    const reason = new DOMException("reader canceled", "AbortError");
    let outcome: unknown = "pending";

    void apiFetch("/api/workbench/courses", { signal: caller.signal }, (response) => response.json()).then(
      (value) => {
        outcome = value;
      },
      (error: unknown) => {
        outcome = error;
      },
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(json).toHaveBeenCalledOnce();

    caller.abort(reason);
    await vi.advanceTimersByTimeAsync(0);

    expect(outcome).toBe(reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("clears the absolute deadline after a successful body reader", async () => {
    vi.useFakeTimers();
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) } as Response),
    );
    const { apiFetch } = await import("./client");

    await expect(apiFetch("/api/workbench/courses", {}, (response) => response.json())).resolves.toEqual({
      ok: true,
    });
    expect(vi.getTimerCount()).toBe(0);
  });
});
