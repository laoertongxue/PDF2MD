import type { WsEvent } from "./types";
import { getWsConfig, WS_SESSION_PROTOCOL, WS_SESSION_TOKEN_PREFIX } from "./runtime";

export function connectBatchWs(
  batchId: string,
  since: number,
  onEvent: (e: WsEvent) => void,
  onClose?: () => void,
): () => void {
  let ws: WebSocket | undefined;
  let canceled = false;
  void getWsConfig()
    .then(({ wsBase, sessionToken }) => {
      if (canceled) return;
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
        onClose?.();
      };
    })
    .catch(() => onClose?.());
  return () => {
    canceled = true;
    ws?.close();
  };
}
