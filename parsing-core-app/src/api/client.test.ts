import { beforeEach, describe, expect, it, vi } from "vitest";

const DEV_TOKEN = "dev-session-token-0123456789abcdef0123456789abcdef";

describe("authenticated API client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
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
});
