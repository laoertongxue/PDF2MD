export interface Course {
  id: string;
  title: string;
  description: string;
  root_dir: string;
  created_at?: number;
  updated_at?: number;
}

export interface Source {
  id: string;
  course_id: string;
  kind: string;
  file_path: string;
  title: string;
  markdown_path?: string | null;
  status: string;
  created_at?: number;
  updated_at?: number;
}

export interface ImportedSource {
  source_id: string;
  title: string;
  stored_path: string;
}

export interface Chapter {
  id: string;
  source_id: string;
  course_id: string;
  seq: number;
  title: string;
  source_md_path?: string;
  status: string;
  created_at?: number;
  updated_at?: number;
}

export interface ChapterDraft extends Chapter {
  start: number;
  end: number;
}

export interface ChapterDraftSpec {
  id?: string;
  title: string;
  start: number;
  end: number;
}

export interface ChapterDraftState {
  chapters: ChapterDraft[];
  fingerprint: string;
}

export interface Card {
  id: string;
  origin_type: "chapter" | "topic";
  origin_id: string;
  origin_title: string;
  card_type: string;
  title: string;
  content: string;
  source_refs: string[];
  tags: string[];
  status: "ACTIVE" | "ARCHIVED";
  favorite: boolean;
  updated_at: number;
}

export interface CourseCardPatch {
  title: string;
  content: string;
  tags: string[];
  status: "ACTIVE" | "ARCHIVED";
  expected_updated_at: number;
}

export interface NoteBlock {
  id: string;
  chapter_id: string;
  kind: string;
  title: string;
  body: string;
  seq: number;
  updated_at?: number;
}

export type ChapterRunStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";

export interface ChapterRun {
  id: string;
  chapter_id: string;
  round_key: string;
  executor: string;
  status: ChapterRunStatus;
  output: string;
  error: string;
  stale: boolean;
  created_at: number;
  updated_at: number;
}

export interface WorkbenchSettings {
  deepseek_model: string;
  deepseek_key_masked: string | null;
}

export type TopicOutlineExecutor = "stub" | "deepseek" | "hybrid";
export type TopicStatus = "DRAFT" | "NOT_READY" | "READY" | "RUNNING" | "COMPLETED" | "STALE" | "FAILED";
export type TopicSyncStatus = "PENDING" | "SYNCING" | "SYNCED" | "FAILED";
export type TopicRunStatus = "RUNNING" | "COMPLETED" | "FAILED";

export interface TopicCreateRequest {
  title?: string;
  description?: string;
  chapter_ids?: string[] | null;
}

export interface TopicPatchRequest {
  title?: string;
  description?: string;
}

export interface TopicMappingRequest {
  chapter_ids: string[];
}

export interface TopicReorderRequest {
  topic_ids: string[];
}

export interface TopicMergeRequest {
  topic_ids: string[];
  title: string;
  description?: string;
  chapter_ids?: string[];
}

export interface TopicSplitRequest {
  title: string;
  description?: string;
  new_chapter_ids: string[];
}

export interface TopicGenerateRequest {
  executor: TopicOutlineExecutor;
}

export interface TopicRunRequest {
  executor: "stub";
}

export interface CourseTopic {
  id: string;
  course_id: string;
  seq: number;
  title: string;
  description: string;
  generation_reason: string;
  status: TopicStatus;
  confirmed: boolean;
  stale_reason: string;
  chapter_ids: string[];
  blocking_chapter_ids: string[];
  sync_status: TopicSyncStatus;
  sync_error: string;
}

export interface TopicNoteBlock {
  id: string;
  topic_id: string;
  kind: string;
  content: string;
  updated_at: number;
}

export interface TopicCard {
  id: string;
  topic_id: string;
  card_type: string;
  title: string;
  content: string;
  source_refs: string[];
  created_at: number;
}

export interface TopicRun {
  id: string;
  topic_id: string;
  round_key: string;
  status: TopicRunStatus;
  input_fingerprint: string;
  output: string;
  error: string;
  started_at: number;
  finished_at: number | null;
}
