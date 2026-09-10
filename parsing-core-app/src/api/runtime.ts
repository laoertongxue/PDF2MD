const DEFAULT_BROWSER_API_BASE = "http://127.0.0.1:8000";
export const SESSION_HEADER = "X-PDF2MD-Session";
export const WS_SESSION_PROTOCOL = "pdf2md-session-v1";
export const WS_SESSION_TOKEN_PREFIX = "pdf2md-session-token.";

export interface ApiConfig {
  apiBase: string;
  sessionToken: string;
}

const MAX_PENDING_API_CONFIG_INVOCATIONS = 2;
const MAX_API_CONFIG_WAITERS = 128;

export class ApiConfigBusyError extends Error {
  constructor() {
    super("API configuration is busy");
    this.name = "BusyError";
  }
}

interface ApiConfigWaiter {
  resolve: (config: ApiConfig) => void;
  reject: (error: unknown) => void;
  signal?: AbortSignal;
  onAbort?: () => void;
}

interface ApiConfigInvocation {
  sequence: number;
  waiters: Set<ApiConfigWaiter>;
  orphaned: boolean;
  settled: boolean;
}

interface ApiConfigSlotWaiter {
  resolve: () => void;
  reject: (error: unknown) => void;
  signal?: AbortSignal;
  onAbort?: () => void;
}

const pendingApiConfigInvocations: ApiConfigInvocation[] = [];
const apiConfigSlotWaiters = new Set<ApiConfigSlotWaiter>();
let nextApiConfigInvocationSequence = 0;
let latestSuccessfulInvocationSequence = 0;
let latestSuccessfulApiConfig: ApiConfig | null = null;
let activeApiConfigWaiters = 0;

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
  forceExitAvailable?: boolean;
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

function signalReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The operation was aborted", "AbortError");
}

function acquireApiConfigWaiter(): void {
  if (activeApiConfigWaiters >= MAX_API_CONFIG_WAITERS) throw new ApiConfigBusyError();
  activeApiConfigWaiters += 1;
}

function releaseApiConfigWaiter(): void {
  activeApiConfigWaiters -= 1;
}

function removePendingInvocation(invocation: ApiConfigInvocation): void {
  const index = pendingApiConfigInvocations.indexOf(invocation);
  if (index >= 0) pendingApiConfigInvocations.splice(index, 1);
}

function removeWaiter(invocation: ApiConfigInvocation, waiter: ApiConfigWaiter): boolean {
  if (!invocation.waiters.delete(waiter)) return false;
  releaseApiConfigWaiter();
  if (waiter.signal && waiter.onAbort) waiter.signal.removeEventListener("abort", waiter.onAbort);
  if (!invocation.settled && invocation.waiters.size === 0) invocation.orphaned = true;
  return true;
}

function settleWaiters(invocation: ApiConfigInvocation, config?: ApiConfig, error?: unknown): void {
  for (const waiter of [...invocation.waiters]) {
    removeWaiter(invocation, waiter);
    if (config) waiter.resolve(config);
    else waiter.reject(error);
  }
}

function releaseSlotWaiters(): void {
  for (const waiter of [...apiConfigSlotWaiters]) {
    apiConfigSlotWaiters.delete(waiter);
    releaseApiConfigWaiter();
    if (waiter.signal && waiter.onAbort) waiter.signal.removeEventListener("abort", waiter.onAbort);
    waiter.resolve();
  }
}

function waitForApiConfigSlot(signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(signalReason(signal));
  try {
    acquireApiConfigWaiter();
  } catch (error) {
    return Promise.reject(error);
  }
  return new Promise<void>((resolve, reject) => {
    const waiter: ApiConfigSlotWaiter = {
      resolve,
      reject,
      ...(signal ? { signal } : {}),
    };
    if (signal) {
      waiter.onAbort = () => {
        if (!apiConfigSlotWaiters.delete(waiter)) return;
        releaseApiConfigWaiter();
        if (waiter.onAbort) signal.removeEventListener("abort", waiter.onAbort);
        reject(signalReason(signal));
      };
      signal.addEventListener("abort", waiter.onAbort, { once: true });
    }
    apiConfigSlotWaiters.add(waiter);
  });
}

function startApiConfigInvocation(): ApiConfigInvocation {
  const invocation: ApiConfigInvocation = {
    sequence: ++nextApiConfigInvocationSequence,
    waiters: new Set(),
    orphaned: false,
    settled: false,
  };
  const rawConfig = import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke<ApiConfig>("get_api_config"))
    .then(parseApiConfig);
  pendingApiConfigInvocations.push(invocation);
  void rawConfig.then(
    (config) => {
      invocation.settled = true;
      removePendingInvocation(invocation);
      if (invocation.sequence >= latestSuccessfulInvocationSequence) {
        latestSuccessfulInvocationSequence = invocation.sequence;
        latestSuccessfulApiConfig = config;
        for (const older of pendingApiConfigInvocations) {
          if (older.sequence >= invocation.sequence) continue;
          older.orphaned = true;
          settleWaiters(older, config);
        }
        settleWaiters(invocation, config);
      } else {
        settleWaiters(invocation, latestSuccessfulApiConfig ?? undefined, new Error("configuration superseded"));
      }
      releaseSlotWaiters();
    },
    (error: unknown) => {
      invocation.settled = true;
      removePendingInvocation(invocation);
      if (invocation.sequence < latestSuccessfulInvocationSequence && latestSuccessfulApiConfig) {
        settleWaiters(invocation, latestSuccessfulApiConfig);
      } else {
        settleWaiters(invocation, undefined, error);
      }
      releaseSlotWaiters();
    },
  );
  return invocation;
}

function waitForInvocation(invocation: ApiConfigInvocation, signal?: AbortSignal): Promise<ApiConfig> {
  if (signal?.aborted) return Promise.reject(signalReason(signal));
  try {
    acquireApiConfigWaiter();
  } catch (error) {
    return Promise.reject(error);
  }
  return new Promise<ApiConfig>((resolve, reject) => {
    const waiter: ApiConfigWaiter = { resolve, reject, ...(signal ? { signal } : {}) };
    if (signal) {
      waiter.onAbort = () => {
        if (!removeWaiter(invocation, waiter)) return;
        reject(signalReason(signal));
      };
      signal.addEventListener("abort", waiter.onAbort, { once: true });
    }
    invocation.waiters.add(waiter);
  });
}

async function getTauriApiConfig(signal?: AbortSignal): Promise<ApiConfig> {
  if (signal?.aborted) throw signalReason(signal);
  while (pendingApiConfigInvocations.length >= MAX_PENDING_API_CONFIG_INVOCATIONS) {
    const latest = pendingApiConfigInvocations.reduce<ApiConfigInvocation | null>(
      (current, invocation) => (current && current.sequence > invocation.sequence ? current : invocation),
      null,
    );
    if (latest && !latest.orphaned) return waitForInvocation(latest, signal);

    await waitForApiConfigSlot(signal);
  }
  return waitForInvocation(startApiConfigInvocation(), signal);
}

export function __getApiConfigWaiterSnapshotForTests(): { active: number; invocation: number; slot: number } {
  return {
    active: activeApiConfigWaiters,
    invocation: pendingApiConfigInvocations.reduce((total, invocation) => total + invocation.waiters.size, 0),
    slot: apiConfigSlotWaiters.size,
  };
}

export async function getApiConfig(signal?: AbortSignal): Promise<ApiConfig> {
  if (!isTauriRuntime()) return parseApiConfig(browserApiConfig());
  return getTauriApiConfig(signal);
}

export async function getApiBase(): Promise<string> {
  return (await getApiConfig()).apiBase;
}

export async function getWsConfig(signal?: AbortSignal): Promise<{ wsBase: string; sessionToken: string }> {
  const { apiBase, sessionToken } = await getApiConfig(signal);
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

export async function retryExitCleanup(): Promise<string | undefined> {
  if (!isTauriRuntime()) return undefined;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<string>("retry_exit_cleanup");
}

export async function requestForceExitConfirmation(): Promise<string | undefined> {
  if (!isTauriRuntime()) return undefined;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<string>("request_force_exit_confirmation");
}
