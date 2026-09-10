import type { BatchResponse, BatchStatus, TaskStatus } from "./types";
import { getApiConfig, SESSION_HEADER } from "./runtime";

const DEFAULT_API_TIMEOUT_MS = 30_000;

function requestSignal(callerSignal?: AbortSignal | null): { signal: AbortSignal; cleanup: () => void } {
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });

  const timer = window.setTimeout(
    () => controller.abort(new DOMException("API 请求超时，请重试", "TimeoutError")),
    DEFAULT_API_TIMEOUT_MS,
  );
  return {
    signal: controller.signal,
    cleanup: () => {
      window.clearTimeout(timer);
      callerSignal?.removeEventListener("abort", abortFromCaller);
    },
  };
}

function signalReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The operation was aborted", "AbortError");
}

function waitForSignal<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(signalReason(signal));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signalReason(signal));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

async function runWithSignal<T>(operation: () => T | Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) throw signalReason(signal);
  return waitForSignal(Promise.resolve().then(operation), signal);
}

export type ApiResponseReader<T> = (response: Response, signal: AbortSignal) => T | Promise<T>;

export async function apiFetch<T>(
  path: string,
  init: RequestInit | undefined,
  reader: ApiResponseReader<T>,
): Promise<T> {
  if (!path.startsWith("/")) throw new Error("API path must be relative");
  const requestInit = init ?? {};
  const request = requestSignal(requestInit.signal);
  try {
    const { apiBase, sessionToken } = await runWithSignal(() => getApiConfig(request.signal), request.signal);
    const headers = new Headers(requestInit.headers);
    headers.set(SESSION_HEADER, sessionToken);
    const response = await runWithSignal(
      () => fetch(`${apiBase}${path}`, { ...requestInit, headers, signal: request.signal }),
      request.signal,
    );
    return await runWithSignal(() => reader(response, request.signal), request.signal);
  } finally {
    request.cleanup();
  }
}

export async function createBatch(files: string[], concurrency = 4): Promise<BatchResponse> {
  return apiFetch(
    "/api/batches",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files, concurrency }),
    },
    async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return (await res.json()) as BatchResponse;
    },
  );
}

export async function getBatch(batchId: string): Promise<BatchStatus> {
  return apiFetch(`/api/batches/${batchId}`, undefined, async (res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as BatchStatus;
  });
}

export async function listBatches(status?: string): Promise<BatchStatus[]> {
  const q = status ? `?status=${status}` : "";
  return apiFetch(`/api/batches${q}`, undefined, async (res) => (await res.json()) as BatchStatus[]);
}

export async function cancelBatch(batchId: string): Promise<void> {
  await apiFetch(`/api/batches/${batchId}`, { method: "DELETE" }, () => undefined);
}

export async function getTask(taskId: string): Promise<TaskStatus> {
  return apiFetch(`/api/tasks/${taskId}`, undefined, async (res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as TaskStatus;
  });
}

export async function getMergedMd(taskId: string): Promise<string> {
  return apiFetch(`/api/tasks/${taskId}/merged`, undefined, async (res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  });
}
