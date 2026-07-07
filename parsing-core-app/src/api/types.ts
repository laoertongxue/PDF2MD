export interface BatchResponse {
  batch_id: string;
  task_ids: string[];
  accepted: number;
  rejected: number;
}

export interface TaskItem {
  task_id: string;
  status: string;
  file_path: string;
}

export interface BatchStatus {
  batch_id: string;
  status: string;
  total_tasks: number;
  completed_tasks: number;
  tasks: TaskItem[];
}

export interface TaskStatus {
  task_id: string;
  batch_id: string | null;
  status: string;
  sections: number;
  completed: number;
  error_msg?: string;
}

export interface WsEvent {
  seq: number;
  batch_id: string;
  task_id?: string;
  event: string;
  payload: Record<string, unknown>;
  ts: number;
}
