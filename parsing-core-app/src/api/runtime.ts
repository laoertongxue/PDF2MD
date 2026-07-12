const BROWSER_API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export type ServiceState = "starting" | "running" | "failed" | "restarting";
export interface ServiceStatus {
  state: ServiceState;
  port: number;
  error?: { category: string; message: string } | null;
  logPath?: string | null;
  logs?: string[];
}

export function getApiBase(): Promise<string> {
  if (!("__TAURI_INTERNALS__" in window)) return Promise.resolve(BROWSER_API_BASE);
  return import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke<{ apiBase: string }>("get_api_config"))
    .then((config) => config.apiBase);
}

export async function getWsBase(): Promise<string> {
  return (await getApiBase()).replace(/^http/, "ws");
}

export async function getServiceStatus(): Promise<ServiceStatus> {
  if (!("__TAURI_INTERNALS__" in window)) {
    return { state: "running", port: Number(new URL(BROWSER_API_BASE).port) };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<ServiceStatus>("get_status");
}

export async function retryService(): Promise<void> {
  if (!("__TAURI_INTERNALS__" in window)) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("retry_service");
}
