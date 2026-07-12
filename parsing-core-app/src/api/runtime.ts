const BROWSER_API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export function getApiBase(): Promise<string> {
  if (!("__TAURI_INTERNALS__" in window)) return Promise.resolve(BROWSER_API_BASE);
  return import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke<{ apiBase: string }>("get_api_config"))
    .then((config) => config.apiBase);
}

export async function getWsBase(): Promise<string> {
  return (await getApiBase()).replace(/^http/, "ws");
}
