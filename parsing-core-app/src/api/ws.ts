import type { WsEvent } from "./types";

export function connectBatchWs(
  batchId: string,
  since: number,
  onEvent: (e: WsEvent) => void,
  onClose?: () => void,
): () => void {
  const url = `ws://127.0.0.1:8000/ws/batch/${batchId}?since=${since}`;
  const ws = new WebSocket(url);
  ws.onmessage = (msg) => {
    try { onEvent(JSON.parse(msg.data)); } catch { /* ignore */ }
  };
  ws.onclose = () => { onClose?.(); };
  return () => { ws.close(); };
}
