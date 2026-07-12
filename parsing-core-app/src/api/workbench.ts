import type {
  Card,
  CourseCardPatch,
  Chapter,
  ChapterRun,
  Course,
  CourseTopic,
  ImportedSource,
  NoteBlock,
  Source,
  TopicCard,
  TopicCreateRequest,
  TopicNoteBlock,
  TopicOutlineExecutor,
  TopicPatchRequest,
  TopicMergeRequest,
  TopicSplitRequest,
  TopicRun,
  TopicRunStatus,
  TopicStatus,
  TopicSyncStatus,
  WorkbenchSettings,
} from "./workbenchTypes";
import { getApiBase } from "./runtime";

export type SafeApiErrorCategory =
  | "invalid_request"
  | "not_found"
  | "conflict"
  | "invalid_format"
  | "model_unavailable"
  | "storage"
  | "service_unavailable"
  | "protocol"
  | "network"
  | "canceled"
  | "task_running"
  | "edit_saved_sync_failed";

const SAFE_ERROR_MESSAGES: Record<SafeApiErrorCategory, string> = {
  invalid_request: "请求内容不正确，请检查后重试",
  not_found: "请求的内容不存在或已被删除",
  conflict: "当前状态不允许此操作，请刷新后重试",
  invalid_format: "请求内容或格式无效，请检查后重试",
  model_unavailable: "主题生成服务暂时不可用，请稍后重试",
  storage: "文件同步失败，请检查存储空间后重试",
  service_unavailable: "服务暂时不可用，请稍后重试",
  protocol: "服务返回数据格式异常，请稍后重试",
  network: "无法连接本地服务，请确认服务已启动",
  canceled: "操作已取消",
  task_running: "任务仍在运行",
  edit_saved_sync_failed: "编辑已保存到数据库，Markdown同步失败，可重试",
};

export class SafeApiError extends Error {
  constructor(readonly category: SafeApiErrorCategory) {
    super(SAFE_ERROR_MESSAGES[category]);
    this.name = "SafeApiError";
  }
}

export function getSafeApiErrorMessage(error: unknown): string | null {
  return error instanceof SafeApiError ? SAFE_ERROR_MESSAGES[error.category] : null;
}

const TOPIC_STATUSES = new Set<TopicStatus>(["DRAFT", "NOT_READY", "READY", "RUNNING", "COMPLETED", "STALE", "FAILED"]);
const TOPIC_SYNC_STATUSES = new Set<TopicSyncStatus>(["PENDING", "SYNCING", "SYNCED", "FAILED"]);
const TOPIC_RUN_STATUSES = new Set<TopicRunStatus>(["RUNNING", "COMPLETED", "FAILED"]);
const CHAPTER_RUN_STATUSES = new Set(["PENDING", "RUNNING", "COMPLETED", "FAILED"]);

function protocolError(): SafeApiError {
  return new SafeApiError("protocol");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function parseArray<T>(value: unknown, parseItem: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) throw protocolError();
  return value.map(parseItem);
}

function parseImportedSource(value: unknown): ImportedSource {
  if (
    !isRecord(value) ||
    typeof value.source_id !== "string" ||
    typeof value.title !== "string" ||
    typeof value.stored_path !== "string"
  ) throw protocolError();
  return value as unknown as ImportedSource;
}

function parseImportedSources(value: unknown): ImportedSource[] {
  if (!isRecord(value)) throw protocolError();
  return parseArray(value.items, parseImportedSource);
}

function parseTopic(value: unknown): CourseTopic {
  if (!isRecord(value)) throw protocolError();
  const status = value.status;
  const syncStatus = value.sync_status;
  if (
    typeof value.id !== "string" ||
    typeof value.course_id !== "string" ||
    typeof value.seq !== "number" ||
    typeof value.title !== "string" ||
    typeof value.description !== "string" ||
    typeof value.generation_reason !== "string" ||
    typeof status !== "string" ||
    !TOPIC_STATUSES.has(status as TopicStatus) ||
    typeof value.confirmed !== "boolean" ||
    typeof value.stale_reason !== "string" ||
    !isStringArray(value.chapter_ids) ||
    !isStringArray(value.blocking_chapter_ids) ||
    typeof syncStatus !== "string" ||
    !TOPIC_SYNC_STATUSES.has(syncStatus as TopicSyncStatus) ||
    typeof value.sync_error !== "string"
  ) throw protocolError();
  return value as unknown as CourseTopic;
}

function parseTopicBlock(value: unknown): TopicNoteBlock {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.topic_id !== "string" ||
    typeof value.kind !== "string" || typeof value.content !== "string" || typeof value.updated_at !== "number") {
    throw protocolError();
  }
  return value as unknown as TopicNoteBlock;
}

function parseTopicCard(value: unknown): TopicCard {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.topic_id !== "string" ||
    typeof value.card_type !== "string" || typeof value.title !== "string" || typeof value.content !== "string" ||
    !isStringArray(value.source_refs) || typeof value.created_at !== "number") {
    throw protocolError();
  }
  return value as unknown as TopicCard;
}

function parseTopicRun(value: unknown): TopicRun {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.topic_id !== "string" ||
    typeof value.round_key !== "string" || typeof value.status !== "string" ||
    !TOPIC_RUN_STATUSES.has(value.status as TopicRunStatus) || typeof value.input_fingerprint !== "string" ||
    typeof value.output !== "string" || typeof value.error !== "string" || typeof value.started_at !== "number" ||
    (value.finished_at !== null && typeof value.finished_at !== "number")) {
    throw protocolError();
  }
  return value as unknown as TopicRun;
}

function parseCourseCard(value: unknown): Card {
  if (!isRecord(value) || typeof value.id !== "string" ||
    (value.origin_type !== "chapter" && value.origin_type !== "topic") ||
    typeof value.origin_id !== "string" || typeof value.origin_title !== "string" ||
    typeof value.card_type !== "string" || typeof value.title !== "string" ||
    typeof value.content !== "string" || !isStringArray(value.source_refs) || !isStringArray(value.tags) ||
    (value.status !== "ACTIVE" && value.status !== "ARCHIVED") || typeof value.favorite !== "boolean" ||
    typeof value.updated_at !== "number") throw protocolError();
  return value as unknown as Card;
}

function parseNoteBlock(value: unknown): NoteBlock {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.chapter_id !== "string" ||
    typeof value.kind !== "string" || typeof value.title !== "string" || typeof value.body !== "string" ||
    typeof value.seq !== "number" || (value.updated_at !== undefined && typeof value.updated_at !== "number")) throw protocolError();
  return value as unknown as NoteBlock;
}

function parseChapterRun(value: unknown): ChapterRun {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.chapter_id !== "string" ||
    typeof value.round_key !== "string" || typeof value.executor !== "string" || typeof value.status !== "string" ||
    !CHAPTER_RUN_STATUSES.has(value.status) || typeof value.output !== "string" || typeof value.error !== "string" ||
    typeof value.stale !== "boolean" || typeof value.created_at !== "number" || typeof value.updated_at !== "number") throw protocolError();
  return value as unknown as ChapterRun;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  parse: (value: unknown) => T = (value) => value as T,
  allowNoContent = false,
  statusCategories: Partial<Record<number, SafeApiErrorCategory>> = {},
): Promise<T> {
  try {
    const res = await fetch(`${await getApiBase()}${path}`, init);
    if (!res.ok) {
      const categories: Record<number, SafeApiErrorCategory> = {
        400: "invalid_request",
        404: "not_found",
        409: "conflict",
        422: "invalid_format",
        502: "model_unavailable",
        507: "storage",
      };
      throw new SafeApiError(statusCategories[res.status] ?? categories[res.status] ?? "service_unavailable");
    }
    if (res.status === 204) {
      if (allowNoContent) return undefined as T;
      throw protocolError();
    }
    let value: unknown;
    try {
      value = await res.json();
    } catch {
      throw protocolError();
    }
    return parse(value);
  } catch (error) {
    if (error instanceof SafeApiError) {
      throw error;
    }
    if (isRecord(error) && error.name === "AbortError") {
      throw new SafeApiError("canceled");
    }
    if (error instanceof TypeError) {
      throw new SafeApiError("network");
    }
    throw protocolError();
  }
}

function post<T>(path: string, body?: unknown, parse?: (value: unknown) => T): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, parse);
}

export function listCourses(): Promise<Course[]> {
  return request<Course[]>("/api/workbench/courses");
}

export function createCourse(title: string, description: string, root_dir: string): Promise<Course> {
  return post<Course>("/api/workbench/courses", { title, description, root_dir });
}

export function createSource(courseId: string, file_path: string, title: string, kind = "main"): Promise<Source> {
  return post<Source>(`/api/workbench/courses/${courseId}/sources`, { kind, file_path, title });
}

export function importSources(courseId: string, paths: string[], titles?: string[]): Promise<ImportedSource[]> {
  const payload = titles === undefined ? { paths } : { paths, titles };
  return post<ImportedSource[]>(`/api/workbench/courses/${courseId}/sources/import`, payload, parseImportedSources);
}

export function listSources(courseId: string): Promise<Source[]> {
  return request<Source[]>(`/api/workbench/courses/${courseId}/sources`);
}

export function detectChapters(sourceId: string): Promise<Chapter[]> {
  return post<Chapter[]>(`/api/workbench/sources/${sourceId}/detect-chapters`);
}

export function listChapters(sourceId: string): Promise<Chapter[]> {
  return request<Chapter[]>(`/api/workbench/sources/${sourceId}/chapters`);
}

export function getChapter(chapterId: string): Promise<Chapter> {
  return request<Chapter>(`/api/workbench/chapters/${chapterId}`);
}

export function confirmChapter(chapterId: string): Promise<Chapter> {
  return post<Chapter>(`/api/workbench/chapters/${chapterId}/confirm`);
}

export function runChapter(chapterId: string, executor = "stub"): Promise<Chapter> {
  return post<Chapter>(`/api/workbench/chapters/${chapterId}/run`, { executor });
}

export function getWorkbenchSettings(): Promise<WorkbenchSettings> {
  return request<WorkbenchSettings>("/api/workbench/settings");
}

export function saveDeepSeekSettings(api_key: string | null, model: string): Promise<WorkbenchSettings> {
  const payload: { model: string; api_key?: string } = { model };
  if (api_key?.trim()) {
    payload.api_key = api_key.trim();
  }
  return post<WorkbenchSettings>("/api/workbench/settings/deepseek", payload);
}

export function testDeepSeekSettings(): Promise<{ status: string }> {
  return post<{ status: string }>("/api/workbench/settings/deepseek/test");
}

export function runHybridChapter(chapterId: string): Promise<Chapter> {
  return post<Chapter>(`/api/workbench/chapters/${chapterId}/run-hybrid`);
}

export function listCourseCards(courseId: string): Promise<Card[]> {
  return request<Card[]>(`/api/workbench/courses/${courseId}/cards`, undefined, (value) => parseArray(value, parseCourseCard));
}

export function updateCourseCard(cardId: string, body: CourseCardPatch): Promise<Card> {
  return request<Card>(`/api/workbench/cards/${cardId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }, parseCourseCard);
}

export function setCourseCardFavorite(cardId: string, favorite: boolean, expectedUpdatedAt: number): Promise<Card> {
  return request<Card>(`/api/workbench/cards/${cardId}/favorite`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ favorite, expected_updated_at: expectedUpdatedAt }),
  }, parseCourseCard);
}

export function listChapterNoteBlocks(chapterId: string): Promise<NoteBlock[]> {
  return request<NoteBlock[]>(`/api/workbench/chapters/${chapterId}/note-blocks`, undefined, (value) => parseArray(value, parseNoteBlock));
}

export function listChapterRuns(chapterId: string): Promise<ChapterRun[]> {
  return request<ChapterRun[]>(`/api/workbench/chapters/${chapterId}/runs`, undefined, (value) => parseArray(value, parseChapterRun));
}

export function saveChapterBlock(chapterId: string, kind: string, body: string, expectedBody: string): Promise<NoteBlock> {
  return request<NoteBlock>(`/api/workbench/chapters/${chapterId}/note-blocks/${kind}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ body, expected_body: expectedBody }),
  }, parseNoteBlock, false, { 507: "edit_saved_sync_failed" });
}

export function listTopics(courseId: string): Promise<CourseTopic[]> {
  return request<CourseTopic[]>(`/api/workbench/courses/${courseId}/topics`, undefined, (value) => parseArray(value, parseTopic));
}

export function createTopic(courseId: string, body: TopicCreateRequest): Promise<CourseTopic> {
  return post<CourseTopic>(`/api/workbench/courses/${courseId}/topics`, body, parseTopic);
}

export function generateTopics(courseId: string, executor: TopicOutlineExecutor = "stub"): Promise<CourseTopic[]> {
  return post<CourseTopic[]>(`/api/workbench/courses/${courseId}/topics/generate`, { executor }, (value) => parseArray(value, parseTopic));
}

export function reorderTopics(courseId: string, topic_ids: string[]): Promise<CourseTopic[]> {
  return request<CourseTopic[]>(`/api/workbench/courses/${courseId}/topics/reorder`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic_ids }),
  }, (value) => parseArray(value, parseTopic));
}

export function mergeTopics(courseId: string, body: TopicMergeRequest): Promise<CourseTopic> {
  return post<CourseTopic>(`/api/workbench/courses/${courseId}/topics/merge`, body, parseTopic);
}

export function splitTopic(topicId: string, body: TopicSplitRequest): Promise<CourseTopic[]> {
  return post<CourseTopic[]>(`/api/workbench/topics/${topicId}/split`, body, (value) => parseArray(value, parseTopic));
}

export function confirmTopics(courseId: string): Promise<CourseTopic[]> {
  return post<CourseTopic[]>(`/api/workbench/courses/${courseId}/topics/confirm`, undefined, (value) => parseArray(value, parseTopic));
}

export function getTopic(topicId: string): Promise<CourseTopic> {
  return request<CourseTopic>(`/api/workbench/topics/${topicId}`, undefined, parseTopic);
}

export function patchTopic(topicId: string, body: TopicPatchRequest): Promise<CourseTopic> {
  return request<CourseTopic>(`/api/workbench/topics/${topicId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, parseTopic);
}

export function deleteTopic(topicId: string): Promise<void> {
  return request<void>(`/api/workbench/topics/${topicId}`, { method: "DELETE" }, undefined, true);
}

export function updateTopicMapping(topicId: string, chapter_ids: string[]): Promise<CourseTopic> {
  return request<CourseTopic>(`/api/workbench/topics/${topicId}/chapters`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chapter_ids }),
  }, parseTopic);
}

export function runTopic(topicId: string): Promise<CourseTopic> {
  return post<CourseTopic>(`/api/workbench/topics/${topicId}/run`, { executor: "stub" }, parseTopic);
}

export function runTopicHybrid(topicId: string): Promise<CourseTopic> {
  return post<CourseTopic>(`/api/workbench/topics/${topicId}/run-hybrid`, undefined, parseTopic);
}

export function recoverTopic(topicId: string): Promise<CourseTopic> {
  return request<CourseTopic>(`/api/workbench/topics/${topicId}/recover`, {
    method: "POST",
  }, parseTopic, false, { 409: "task_running" });
}

export function retryTopicSync(topicId: string): Promise<CourseTopic> {
  return post<CourseTopic>(`/api/workbench/topics/${topicId}/sync/retry`, undefined, parseTopic);
}

export function listTopicNoteBlocks(topicId: string): Promise<TopicNoteBlock[]> {
  return request<TopicNoteBlock[]>(`/api/workbench/topics/${topicId}/note-blocks`, undefined, (value) => parseArray(value, parseTopicBlock));
}

export function listTopicCards(topicId: string): Promise<TopicCard[]> {
  return request<TopicCard[]>(`/api/workbench/topics/${topicId}/cards`, undefined, (value) => parseArray(value, parseTopicCard));
}

export function listTopicRuns(topicId: string): Promise<TopicRun[]> {
  return request<TopicRun[]>(`/api/workbench/topics/${topicId}/runs`, undefined, (value) => parseArray(value, parseTopicRun));
}

export function saveTopicBlock(topicId: string, kind: string, content: string, expectedContent: string): Promise<TopicNoteBlock> {
  return request<TopicNoteBlock>(`/api/workbench/topics/${topicId}/note-blocks/${kind}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, expected_content: expectedContent }),
  }, parseTopicBlock, false, { 507: "edit_saved_sync_failed" });
}
