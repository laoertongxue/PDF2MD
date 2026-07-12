import type { BatchResponse, BatchStatus, TaskStatus } from "./types";
import { getApiBase } from "./runtime";

export async function createBatch(files: string[], concurrency = 4): Promise<BatchResponse> {
  const res = await fetch(`${await getApiBase()}/api/batches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, concurrency }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function getBatch(batchId: string): Promise<BatchStatus> {
  const res = await fetch(`${await getApiBase()}/api/batches/${batchId}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function listBatches(status?: string): Promise<BatchStatus[]> {
  const q = status ? `?status=${status}` : "";
  const res = await fetch(`${await getApiBase()}/api/batches${q}`);
  return res.json();
}

export async function cancelBatch(batchId: string): Promise<void> {
  await fetch(`${await getApiBase()}/api/batches/${batchId}`, { method: "DELETE" });
}

export async function getTask(taskId: string): Promise<TaskStatus> {
  const res = await fetch(`${await getApiBase()}/api/tasks/${taskId}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function getMergedMd(taskId: string): Promise<string> {
  const res = await fetch(`${await getApiBase()}/api/tasks/${taskId}/merged`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}
