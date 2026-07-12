import type { WsEvent } from "./types";
import { getWsBase } from "./runtime";

export function connectBatchWs(
  batchId: string,
  since: number,
  onEvent: (e: WsEvent) => void,
  onClose?: () => void,
): () => void {
  let ws: WebSocket | undefined;
  let canceled = false;
  void getWsBase().then((base) => {
    if (canceled) return;
    ws = new WebSocket(`${base}/ws/batch/${batchId}?since=${since}`);
    ws.onmessage = (msg) => {
      try { onEvent(JSON.parse(msg.data)); } catch { /* ignore */ }
    };
    ws.onclose = () => { onClose?.(); };
  });
  return () => {
    canceled = true;
    ws?.close();
  };
}
