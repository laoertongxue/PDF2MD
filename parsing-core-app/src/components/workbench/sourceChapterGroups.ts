import type { Chapter, Source } from "../../api/workbenchTypes";

export interface SourceChapterGroup {
  source: Source;
  chapters: Chapter[];
  completedCount: number;
}

export function createSourceChapterGroups(
  sources: Source[],
  chapters: Record<string, Chapter[]>,
): SourceChapterGroup[] {
  return sources.map((source) => {
    const sourceChapters = [...(chapters[source.id] ?? [])].sort((left, right) => left.seq - right.seq);
    return {
      source,
      chapters: sourceChapters,
      completedCount: sourceChapters.filter((chapter) => chapter.status === "COMPLETED").length,
    };
  });
}

export function chapterOptionLabel(source: Source, chapter: Chapter) {
  return `《${source.title}》 / 第${chapter.seq + 1}章 / ${chapter.title}`;
}
