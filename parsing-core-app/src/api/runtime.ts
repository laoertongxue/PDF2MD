const DEFAULT_BROWSER_API_BASE = "http://127.0.0.1:8000";
export const SESSION_HEADER = "X-PDF2MD-Session";
export const WS_SESSION_PROTOCOL = "pdf2md-session-v1";
export const WS_SESSION_TOKEN_PREFIX = "pdf2md-session-token.";

export interface ApiConfig {
  apiBase: string;
  sessionToken: string;
}

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

function browserApiConfig(): ApiConfig {
  const sessionToken = import.meta.env.VITE_PDF2MD_SESSION_TOKEN;
  if (!sessionToken) throw new Error("VITE_PDF2MD_SESSION_TOKEN is required");
  return {
    apiBase: import.meta.env.VITE_API_BASE_URL ?? DEFAULT_BROWSER_API_BASE,
    sessionToken,
  };
}

function parseApiConfig(value: unknown): ApiConfig {
  if (typeof value !== "object" || value === null) throw new Error("invalid API configuration");
  const candidate = value as Partial<ApiConfig>;
  if (typeof candidate.apiBase !== "string" || typeof candidate.sessionToken !== "string") {
    throw new Error("invalid API configuration");
  }
  let endpoint: URL;
  try {
    endpoint = new URL(candidate.apiBase);
  } catch {
    throw new Error("invalid API configuration");
  }
  const port = Number(endpoint.port);
  if (
    endpoint.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(endpoint.hostname) ||
    endpoint.username !== "" ||
    endpoint.password !== "" ||
    endpoint.pathname !== "/" ||
    endpoint.search !== "" ||
    endpoint.hash !== "" ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65_535 ||
    !/^[A-Za-z0-9._~-]{32,}$/.test(candidate.sessionToken)
  ) {
    throw new Error("invalid API configuration");
  }
  return { apiBase: endpoint.origin, sessionToken: candidate.sessionToken };
}

export async function getApiConfig(): Promise<ApiConfig> {
  if (!isTauriRuntime()) return parseApiConfig(browserApiConfig());
  const { invoke } = await import("@tauri-apps/api/core");
  return parseApiConfig(await invoke<ApiConfig>("get_api_config"));
}

export async function getApiBase(): Promise<string> {
  return (await getApiConfig()).apiBase;
}

export async function getWsConfig(): Promise<{ wsBase: string; sessionToken: string }> {
  const { apiBase, sessionToken } = await getApiConfig();
  return { wsBase: apiBase.replace(/^http/, "ws"), sessionToken };
}

export async function getWsBase(): Promise<string> {
  return (await getWsConfig()).wsBase;
}

export async function getServiceStatus(): Promise<ServiceStatus> {
  if (!isTauriRuntime()) {
    let port = 0;
    try {
      const { apiBase, sessionToken } = await getApiConfig();
      port = Number(new URL(apiBase).port);
      const headers = new Headers({ Accept: "application/json" });
      headers.set(SESSION_HEADER, sessionToken);
      const response = await fetch(`${apiBase}/health`, {
        headers,
        signal: AbortSignal.timeout(1500),
      });
      if (!response.ok) throw new Error(`health returned HTTP ${response.status}`);
      const payload = (await response.json()) as { status?: unknown };
      if (payload.status !== "ok") throw new Error("health response was not ok");
      return { state: "running", port };
    } catch (error) {
      return {
        state: "offline",
        port,
        error: {
          category: "offline",
          message: error instanceof Error ? error.message : "本地服务不可用",
        },
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
