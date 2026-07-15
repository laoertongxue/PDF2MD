const BROWSER_API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export function isTauriRuntime(): boolean {
  return "__TAURI_INTERNALS__" in globalThis;
}

export type ServiceState = "starting" | "running" | "offline" | "failed" | "restarting";
export interface ServiceStatus {
  state: ServiceState;
  port: number;
  error?: { category: string; message: string } | null;
  logPath?: string | null;
  logs?: string[];
}

export function getApiBase(): Promise<string> {
  if (!isTauriRuntime()) return Promise.resolve(BROWSER_API_BASE);
  return import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke<{ apiBase: string }>("get_api_config"))
    .then((config) => config.apiBase);
}

export async function getWsBase(): Promise<string> {
  return (await getApiBase()).replace(/^http/, "ws");
}

export async function getServiceStatus(): Promise<ServiceStatus> {
  if (!isTauriRuntime()) {
    const port = Number(new URL(BROWSER_API_BASE).port);
    try {
      const response = await fetch(`${BROWSER_API_BASE}/health`, {
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(1500),
      });
      if (!response.ok) throw new Error(`health returned HTTP ${response.status}`);
      const payload = await response.json() as { status?: unknown };
      if (payload.status !== "ok") throw new Error("health response was not ok");
      return { state: "running", port };
    } catch (error) {
      return {
        state: "offline",
        port,
        error: { category: "offline", message: error instanceof Error ? error.message : "本地服务不可用" },
      };
    }
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<ServiceStatus>("get_status");
}

export async function retryService(): Promise<void> {
  if (!isTauriRuntime()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("retry_service");
}
