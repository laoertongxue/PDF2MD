import { useState } from "react";
import { ChevronDown, ChevronRight, CircleCheck, FileText } from "lucide-react";
import { Link } from "react-router-dom";
import type { Chapter, Source } from "../../api/workbenchTypes";

export interface SourceChapterGroup {
  source: Source;
  chapters: Chapter[];
  completedCount: number;
}

interface Props {
  groups: SourceChapterGroup[];
  activeChapterId?: string | null;
  onSelectChapter?: (chapterId: string) => void;
  chapterHref?: (chapterId: string) => string;
}

export function createSourceChapterGroups(sources: Source[], chapters: Record<string, Chapter[]>): SourceChapterGroup[] {
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

export default function SourceChapterTree({ groups, activeChapterId, onSelectChapter, chapterHref }: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  return (
    <div className="space-y-3">
      {groups.map(({ source, chapters, completedCount }) => {
        const isCollapsed = collapsed[source.id] ?? false;
        return (
          <section key={source.id} data-testid={`source-group-${source.id}`} className="border-b border-zinc-100 pb-3 last:border-b-0">
            <button
              type="button"
              onClick={() => setCollapsed((current) => ({ ...current, [source.id]: !isCollapsed }))}
              aria-expanded={!isCollapsed}
              aria-label={`${isCollapsed ? "展开" : "折叠"}《${source.title}》`}
              title={`${isCollapsed ? "展开" : "折叠"}教材章节`}
              className="grid min-h-10 w-full grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-2 text-left"
            >
              {isCollapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
              <span className="min-w-0 truncate text-sm font-medium text-zinc-800">{source.title}</span>
              <span className="text-xs tabular-nums text-zinc-400">{completedCount}/{chapters.length}</span>
            </button>
            {!isCollapsed && (
              <div className="ml-6 space-y-1">
                {chapters.map((chapter) => {
                  const label = chapterOptionLabel(source, chapter);
                  const className = `flex min-h-9 w-full items-center gap-2 rounded-md px-2 text-left text-sm ${activeChapterId === chapter.id ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"}`;
                  const content = <><FileText size={14} className="shrink-0 text-zinc-400" /><span className="min-w-0 truncate">{chapter.title}</span>{chapter.status === "COMPLETED" && <CircleCheck size={14} className="ml-auto shrink-0 text-emerald-600" />}</>;
                  return chapterHref ? (
                    <Link key={chapter.id} to={chapterHref(chapter.id)} aria-label={label} title={label} className={className}>{content}</Link>
                  ) : (
                    <button key={chapter.id} type="button" onClick={() => onSelectChapter?.(chapter.id)} aria-label={label} title={label} className={className}>{content}</button>
                  );
                })}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
