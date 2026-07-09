import type { Card, Chapter, Course, NoteBlock, Source } from "./workbenchTypes";

const BASE = "http://127.0.0.1:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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

export function listSources(courseId: string): Promise<Source[]> {
  return request<Source[]>(`/api/workbench/courses/${courseId}/sources`);
}

export function detectChapters(sourceId: string): Promise<Chapter[]> {
  return post<Chapter[]>(`/api/workbench/sources/${sourceId}/detect-chapters`);
}

export function listChapters(sourceId: string): Promise<Chapter[]> {
  return request<Chapter[]>(`/api/workbench/sources/${sourceId}/chapters`);
}

export function confirmChapter(chapterId: string): Promise<Chapter> {
  return post<Chapter>(`/api/workbench/chapters/${chapterId}/confirm`);
}

export function runChapter(chapterId: string, executor = "stub"): Promise<{ chapter_id: string; status: string }> {
  return post<{ chapter_id: string; status: string }>(`/api/workbench/chapters/${chapterId}/run`, { executor });
}

export function listCourseCards(courseId: string): Promise<Card[]> {
  return request<Card[]>(`/api/workbench/courses/${courseId}/cards`);
}

export function listChapterNoteBlocks(chapterId: string): Promise<NoteBlock[]> {
  return request<NoteBlock[]>(`/api/workbench/chapters/${chapterId}/note-blocks`);
}
