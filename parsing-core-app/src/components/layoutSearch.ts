import type { Card, Chapter, Course, Source } from "../api/workbenchTypes";

interface SearchData {
  courses: Course[];
  sources: Record<string, Source[]>;
  chapters: Record<string, Chapter[]>;
  cardsByCourse: Record<string, Card[]>;
}

export interface SearchResult {
  id: string;
  kind: string;
  label: string;
  detail: string;
  to: string;
  courseId?: string;
}

export function buildSearchResults(data: SearchData, query: string): SearchResult[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return [];
  const matches = (values: string[]) => values.some((value) => value.toLocaleLowerCase().includes(needle));
  return [
    ...data.courses
      .filter((course) => matches([course.title, course.description]))
      .map((course) => ({
        id: `course:${course.id}`,
        kind: "课程",
        label: course.title,
        detail: course.description,
        to: "/workbench",
        courseId: course.id,
      })),
    ...Object.values(data.sources)
      .flat()
      .filter((source) => matches([source.title, source.file_path]))
      .map((source) => ({
        id: `source:${source.id}`,
        kind: "教材",
        label: source.title,
        detail: source.file_path,
        to: "/workbench/chapters",
        courseId: source.course_id,
      })),
    ...Object.values(data.chapters)
      .flat()
      .filter((chapter) => matches([chapter.title]))
      .map((chapter) => ({
        id: `chapter:${chapter.id}`,
        kind: "章节",
        label: chapter.title,
        detail: "打开章节精读",
        to: `/workbench/chapter?chapterId=${chapter.id}`,
        courseId: chapter.course_id,
      })),
    ...Object.entries(data.cardsByCourse).flatMap(([courseId, cards]) =>
      cards
        .filter((card) => matches([card.title, card.content, card.origin_title]))
        .map((card) => ({
          id: `card:${card.id}`,
          kind: "卡片",
          label: card.title,
          detail: card.origin_title,
          to: `/workbench/cards?cardId=${card.id}`,
          courseId,
        })),
    ),
  ].slice(0, 12);
}
