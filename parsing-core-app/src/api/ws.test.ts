import { beforeEach, describe, expect, it, vi } from "vitest";

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
});
