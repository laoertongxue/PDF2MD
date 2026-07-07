import { create } from "zustand";
import { createBatch, getBatch, listBatches, cancelBatch, getTask, getMergedMd } from "../api/client";
import { connectBatchWs } from "../api/ws";
import type { BatchStatus, TaskStatus, WsEvent } from "../api/types";

interface AppState {
  batches: BatchStatus[];
  tasks: Record<string, TaskStatus>;
  mergedDocs: Record<string, string>;
  wsDisconnectors: Record<string, () => void>;
  
  loadBatches: () => Promise<void>;
  submitBatch: (files: string[], concurrency: number) => Promise<string>;
  loadBatch: (id: string) => Promise<void>;
  cancelBatchAction: (id: string) => Promise<void>;
  loadTask: (taskId: string) => Promise<TaskStatus>;
  loadMerged: (taskId: string) => Promise<void>;
  connectWs: (batchId: string, since?: number) => void;
  handleWsEvent: (e: WsEvent) => void;
}

export const useStore = create<AppState>((set, get) => ({
  batches: [],
  tasks: {},
  mergedDocs: {},
  wsDisconnectors: {},

  loadBatches: async () => {
    const batches = await listBatches();
    set({ batches });
  },

  submitBatch: async (files, concurrency) => {
    const res = await createBatch(files, concurrency);
    get().connectWs(res.batch_id, -1);
    return res.batch_id;
  },

  loadBatch: async (id) => {
    const batch = await getBatch(id);
    set((s) => ({ batches: s.batches.map((b) => (b.batch_id === id ? batch : b)) }));
  },

  cancelBatchAction: async (id) => {
    await cancelBatch(id);
    const disco = get().wsDisconnectors[id];
    if (disco) { disco(); }
  },

  loadTask: async (taskId) => {
    const task = await getTask(taskId);
    set((s) => ({ tasks: { ...s.tasks, [taskId]: task } }));
    return task;
  },

  loadMerged: async (taskId) => {
    const md = await getMergedMd(taskId);
    set((s) => ({ mergedDocs: { ...s.mergedDocs, [taskId]: md } }));
  },

  connectWs: (batchId, since = -1) => {
    const disco = connectBatchWs(batchId, since, (e) => get().handleWsEvent(e));
    set((s) => ({ wsDisconnectors: { ...s.wsDisconnectors, [batchId]: disco } }));
  },

  handleWsEvent: (e) => {
    if (e.event === "TASK_STATE" && e.task_id) {
      set((s) => {
        const existing = s.tasks[e.task_id!] || { task_id: e.task_id!, batch_id: e.batch_id, status: "", sections: 0, completed: 0 };
        return { tasks: { ...s.tasks, [e.task_id!]: { ...existing, status: e.payload.status as string } } };
      });
    }
    if (e.event === "BATCH_DONE") {
      get().loadBatch(e.batch_id);
    }
  },
}));
