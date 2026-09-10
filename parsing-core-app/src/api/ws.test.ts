import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const DEV_TOKEN = "dev-session-token-0123456789abcdef0123456789abcdef";

class WebSocketDouble {
  static instances: WebSocketDouble[] = [];
  readonly url: string;
  readonly protocols: string | string[] | undefined;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocols = protocols;
    WebSocketDouble.instances.push(this);
  }

  close() {}
}

describe("authenticated batch WebSocket", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    WebSocketDouble.instances = [];
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    vi.stubGlobal("WebSocket", WebSocketDouble);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.doUnmock("./runtime");
  });

  it("sends the session through subprotocols and never in the URL", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const { connectBatchWs } = await import("./ws");

    connectBatchWs("batch-1", 7, vi.fn());
    await vi.waitFor(() => expect(WebSocketDouble.instances).toHaveLength(1));

    const socket = WebSocketDouble.instances[0]!;
    expect(socket.url).toBe("ws://127.0.0.1:8000/ws/batch/batch-1?since=7");
    expect(socket.url).not.toContain(DEV_TOKEN);
    expect(socket.protocols).toEqual(["pdf2md-session-v1", `pdf2md-session-token.${DEV_TOKEN}`]);
  });

  it("does not open a socket before Tauri is ready", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    let resolveConfig: ((value: unknown) => void) | undefined;
    const config = new Promise((resolve) => {
      resolveConfig = resolve;
    });
    vi.doMock("@tauri-apps/api/core", () => ({ invoke: vi.fn().mockReturnValue(config) }));
    const { connectBatchWs } = await import("./ws");

    connectBatchWs("batch-1", 0, vi.fn());
    await Promise.resolve();
    expect(WebSocketDouble.instances).toHaveLength(0);

    resolveConfig?.({
      apiBase: "http://127.0.0.1:43127",
      sessionToken: "tauri-session-token-0123456789abcdef0123456789abcdef",
    });
    await vi.waitFor(() => expect(WebSocketDouble.instances).toHaveLength(1));
  });

  it("times out a permanently hung configuration once", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.doMock("./runtime", () => ({
      WS_SESSION_PROTOCOL: "pdf2md-session-v1",
      WS_SESSION_TOKEN_PREFIX: "pdf2md-session-token.",
      getWsConfig: vi.fn((signal?: AbortSignal) => {
        if (signal) signals.push(signal);
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      }),
    }));
    const onClose = vi.fn();
    const { connectBatchWs } = await import("./ws");

    connectBatchWs("batch-1", 0, vi.fn(), onClose);
    await vi.advanceTimersByTimeAsync(9_999);
    expect(onClose).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    expect(signals).toHaveLength(1);
    expect(signals[0]?.aborted).toBe(true);
    expect(onClose).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(10_000);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("aborts every pending configuration without reporting user disconnects", async () => {
    vi.useFakeTimers();
    const signals: Array<AbortSignal | undefined> = [];
    vi.doMock("./runtime", () => ({
      WS_SESSION_PROTOCOL: "pdf2md-session-v1",
      WS_SESSION_TOKEN_PREFIX: "pdf2md-session-token.",
      getWsConfig: vi.fn((signal?: AbortSignal) => {
        signals.push(signal);
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      }),
    }));
    const onClose = vi.fn();
    const { connectBatchWs } = await import("./ws");

    for (let index = 0; index < 20; index += 1) {
      const disconnect = connectBatchWs(`batch-${index}`, 0, vi.fn(), onClose);
      disconnect();
      disconnect();
    }
    await vi.advanceTimersByTimeAsync(20_000);

    expect(signals).toHaveLength(20);
    expect(signals.every((signal) => signal?.aborted === true)).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(WebSocketDouble.instances).toHaveLength(0);
  });

  it("reports configuration capacity rejection exactly once without opening a socket", async () => {
    const busy = Object.assign(new Error("API configuration is busy"), { name: "BusyError" });
    vi.doMock("./runtime", () => ({
      WS_SESSION_PROTOCOL: "pdf2md-session-v1",
      WS_SESSION_TOKEN_PREFIX: "pdf2md-session-token.",
      getWsConfig: vi.fn().mockRejectedValue(busy),
    }));
    const onClose = vi.fn();
    const { connectBatchWs } = await import("./ws");

    connectBatchWs("batch-busy", 0, vi.fn(), onClose);
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    await Promise.resolve();

    expect(onClose).toHaveBeenCalledOnce();
    expect(WebSocketDouble.instances).toHaveLength(0);
  });

  it("contains synchronous and asynchronous onClose failures without unhandled rejections", async () => {
    vi.stubEnv("VITE_PDF2MD_SESSION_TOKEN", DEV_TOKEN);
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);
    const { connectBatchWs } = await import("./ws");
    const syncClose = vi.fn(() => {
      throw new Error("sync close failure");
    });
    const asyncClose = vi.fn(() => Promise.reject(new Error("async close failure")));

    connectBatchWs("batch-sync", 0, vi.fn(), syncClose);
    connectBatchWs("batch-async", 0, vi.fn(), asyncClose);
    await vi.waitFor(() => expect(WebSocketDouble.instances).toHaveLength(2));

    expect(() => WebSocketDouble.instances[0]?.onclose?.()).not.toThrow();
    WebSocketDouble.instances[1]?.onclose?.();
    await Promise.resolve();
    await Promise.resolve();

    expect(syncClose).toHaveBeenCalledOnce();
    expect(asyncClose).toHaveBeenCalledOnce();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });
});
