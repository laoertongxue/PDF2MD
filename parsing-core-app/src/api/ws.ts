import type { WsEvent } from "./types";
import { getWsConfig, WS_SESSION_PROTOCOL, WS_SESSION_TOKEN_PREFIX } from "./runtime";

const WS_CONFIG_TIMEOUT_MS = 10_000;

export function connectBatchWs(
  batchId: string,
  since: number,
  onEvent: (e: WsEvent) => void,
  onClose?: () => void | Promise<void>,
): () => void {
  let ws: WebSocket | undefined;
  let disconnected = false;
  let closeNotified = false;
  const configController = new AbortController();
  const configTimer = window.setTimeout(
    () => configController.abort(new DOMException("WebSocket 配置获取超时", "TimeoutError")),
    WS_CONFIG_TIMEOUT_MS,
  );
  const notifyClose = () => {
    if (disconnected || closeNotified) return;
    closeNotified = true;
    try {
      const result = onClose?.();
      if (result && typeof (result as PromiseLike<void>).then === "function") {
        void Promise.resolve(result).catch(() => undefined);
      }
    } catch {
      // Consumer cleanup must not break the socket lifecycle.
    }
  };

  void getWsConfig(configController.signal)
    .then(({ wsBase, sessionToken }) => {
      window.clearTimeout(configTimer);
      if (disconnected) return;
      ws = new WebSocket(`${wsBase}/ws/batch/${encodeURIComponent(batchId)}?since=${since}`, [
        WS_SESSION_PROTOCOL,
        `${WS_SESSION_TOKEN_PREFIX}${sessionToken}`,
      ]);
      ws.onmessage = (msg) => {
        try {
          onEvent(JSON.parse(msg.data));
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        notifyClose();
      };
    })
    .catch(() => notifyClose())
    .finally(() => window.clearTimeout(configTimer));
  return () => {
    if (disconnected) return;
    disconnected = true;
    window.clearTimeout(configTimer);
    configController.abort(new DOMException("WebSocket disconnected", "AbortError"));
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
  };
}
