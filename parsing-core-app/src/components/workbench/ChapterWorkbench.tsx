import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Circle, Loader2, Sparkles, XCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import MermaidEditor from "./MermaidEditor";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";
import { chapterOptionLabel, createSourceChapterGroups } from "./sourceChapterGroups";
import type { ChapterRun, NoteBlock } from "../../api/workbenchTypes";

const CONTENT_KINDS = ["summary", "concepts", "plain_explain", "application", "reflection"] as const;
const ROUND_LABELS: Record<string, string> = {
  structure: "章节结构",
  concepts: "核心概念",
  plain_explain: "通俗解释",
  application: "实际应用",
  mermaid: "图示生成",
  cards: "卡片提炼",
  review: "审核",
};
const SOURCE_RE = /\[《([^\]\n]+)》·第\s*(\d+)\s*章\]/g;
const leaveMessage = "当前 Mermaid 有未保存修改，确定离开吗？";

interface AcceptedNavigation {
  courseId: string | null;
  chapterId: string | null;
  searchParams: string;
  epoch: number;
}

interface CourseLoadState {
  courseId: string | null;
  requestId: number;
  status: "idle" | "loading" | "success" | "error";
  error: string | null;
}

function searchParamsForChapter(searchParams: URLSearchParams, chapterId: string | null) {
  const next = new URLSearchParams(searchParams);
  if (chapterId) next.set("chapterId", chapterId);
  else next.delete("chapterId");
  return next;
}

export default function ChapterWorkbench() {
  const store = useWorkbenchStore();
  const [error, setError] = useState<string | null>(null);
  const [chapterLoadError, setChapterLoadError] = useState<string | null>(null);
  const [runningHybrid, setRunningHybrid] = useState(false);
  const [dirtyEditors, setDirtyEditors] = useState<Record<string, number>>({});
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedChapterId = searchParams.get("chapterId");
  const [acceptedNavigation, setAcceptedNavigation] = useState<AcceptedNavigation>(() => ({
    courseId: store.selectedCourseId,
    chapterId: null,
    searchParams: searchParams.toString(),
    epoch: 0,
  }));
  const acceptedNavigationRef = useRef(acceptedNavigation);
  const courseLoadRequestRef = useRef(0);
  const chapterLoadRequestRef = useRef(0);
  const hybridRunRequestRef = useRef(0);
  const rollbackCourseRef = useRef<string | null>(null);
  const rejectedChapterSearchRef = useRef<string | null>(null);
  const [courseLoadState, setCourseLoadState] = useState<CourseLoadState>({
    courseId: null,
    requestId: 0,
    status: "idle",
    error: null,
  });
  const acceptedCourseId = acceptedNavigation.courseId;
  const activeChapterId = acceptedNavigation.chapterId;
  const navigationEpoch = acceptedNavigation.epoch;
  const dirty = Object.values(dirtyEditors).some((epoch) => epoch === navigationEpoch);
  const confirmLeave = useCallback(() => !dirty || window.confirm(leaveMessage), [dirty]);
  const selectCourse = store.selectCourse;
  const loadCourses = store.loadCourses;
  const loadSources = store.loadSources;
  const loadChapters = store.loadChapters;
  const loadChapterNoteBlocks = store.loadChapterNoteBlocks;
  const loadChapterRuns = store.loadChapterRuns;
  const invalidateHybridRun = useCallback(() => {
    hybridRunRequestRef.current += 1;
    setRunningHybrid(false);
  }, []);
  const acceptNavigation = useCallback(
    (courseId: string | null, chapterId: string | null, nextSearch: string) => {
      invalidateHybridRun();
      setDirtyEditors({});
      setAcceptedNavigation((current) => ({
        courseId,
        chapterId,
        searchParams: nextSearch,
        epoch: current.epoch + 1,
      }));
    },
    [invalidateHybridRun],
  );
  const loadChapterContent = useCallback(
    (chapterId: string | null) => {
      const requestId = ++chapterLoadRequestRef.current;
      setChapterLoadError(null);
      const result = (async () => {
        if (!chapterId) return chapterLoadRequestRef.current === requestId;
        try {
          await Promise.all([loadChapterNoteBlocks(chapterId), loadChapterRuns(chapterId)]);
          return chapterLoadRequestRef.current === requestId;
        } catch (reason) {
          if (chapterLoadRequestRef.current === requestId) {
            setChapterLoadError(message(reason, "精读结果加载失败"));
          }
          return false;
        }
      })();
      return { requestId, result };
    },
    [loadChapterNoteBlocks, loadChapterRuns],
  );
  const cancelChapterContentLoad = useCallback((requestId: number) => {
    if (chapterLoadRequestRef.current === requestId) chapterLoadRequestRef.current += 1;
  }, []);
  const chapterGroups = useMemo(
    () => createSourceChapterGroups(acceptedCourseId ? (store.sources[acceptedCourseId] ?? []) : [], store.chapters),
    [acceptedCourseId, store.chapters, store.sources],
  );
  const courseChapters = useMemo(() => chapterGroups.flatMap((group) => group.chapters), [chapterGroups]);
  const requestedCourseChapters = useMemo(
    () =>
      createSourceChapterGroups(
        store.selectedCourseId ? (store.sources[store.selectedCourseId] ?? []) : [],
        store.chapters,
      ).flatMap((group) => group.chapters),
    [store.chapters, store.selectedCourseId, store.sources],
  );
  const requestedCourseDataLoaded = useMemo(() => {
    const courseId = store.selectedCourseId;
    if (!courseId || !Object.prototype.hasOwnProperty.call(store.sources, courseId)) return false;
    return (store.sources[courseId] ?? []).every((source) =>
      Object.prototype.hasOwnProperty.call(store.chapters, source.id),
    );
  }, [store.chapters, store.selectedCourseId, store.sources]);
  const requestedCourseReady =
    courseLoadState.courseId === store.selectedCourseId &&
    courseLoadState.requestId === courseLoadRequestRef.current &&
    courseLoadState.status === "success" &&
    requestedCourseDataLoaded;
  const activeChapter = courseChapters.find((chapter) => chapter.id === activeChapterId) ?? null;
  const blocks = useMemo(
    () => (activeChapterId ? (store.noteBlocksByChapter[activeChapterId] ?? []) : []),
    [activeChapterId, store.noteBlocksByChapter],
  );
  const runs = activeChapterId ? (store.chapterRunsById[activeChapterId] ?? []) : [];
  const initialChapterId = useMemo(() => {
    const next =
      (requestedChapterId && courseChapters.find((chapter) => chapter.id === requestedChapterId)) ||
      courseChapters.find((chapter) => (store.noteBlocksByChapter[chapter.id] ?? []).length) ||
      courseChapters.find((chapter) => ["CONFIRMED", "COMPLETED", "FAILED"].includes(chapter.status));
    return next?.id ?? null;
  }, [courseChapters, requestedChapterId, store.noteBlocksByChapter]);
  const requestedCourseInitialChapterId = useMemo(() => {
    const next =
      (requestedChapterId && requestedCourseChapters.find((chapter) => chapter.id === requestedChapterId)) ||
      requestedCourseChapters.find((chapter) => (store.noteBlocksByChapter[chapter.id] ?? []).length) ||
      requestedCourseChapters.find((chapter) => ["CONFIRMED", "COMPLETED", "FAILED"].includes(chapter.status));
    return next?.id ?? null;
  }, [requestedChapterId, requestedCourseChapters, store.noteBlocksByChapter]);

  useEffect(() => {
    acceptedNavigationRef.current = acceptedNavigation;
  }, [acceptedNavigation]);
  useEffect(
    () => () => {
      hybridRunRequestRef.current += 1;
    },
    [],
  );
  useEffect(() => {
    loadCourses().catch((reason: unknown) => setError(message(reason, "课程加载失败")));
  }, [loadCourses]);
  useEffect(() => {
    const courseId = store.selectedCourseId;
    if (!courseId) {
      courseLoadRequestRef.current += 1;
      rollbackCourseRef.current = null;
      setCourseLoadState({ courseId: null, requestId: courseLoadRequestRef.current, status: "idle", error: null });
      return;
    }
    if (rollbackCourseRef.current === courseId) {
      rollbackCourseRef.current = null;
      return;
    }

    const requestId = ++courseLoadRequestRef.current;
    setError(null);
    setCourseLoadState({ courseId, requestId, status: "loading", error: null });
    async function loadCourseChapters(targetCourseId: string) {
      try {
        const items = await loadSources(targetCourseId);
        if (courseLoadRequestRef.current !== requestId) return;
        await Promise.all(items.map((source) => loadChapters(source.id)));
        if (courseLoadRequestRef.current !== requestId) return;
        setCourseLoadState({ courseId: targetCourseId, requestId, status: "success", error: null });
      } catch (reason) {
        if (courseLoadRequestRef.current !== requestId) return;
        const loadError = message(reason, "章节加载失败");
        setCourseLoadState({ courseId: targetCourseId, requestId, status: "error", error: loadError });
        const accepted = acceptedNavigationRef.current;
        if (accepted.courseId && accepted.courseId !== targetCourseId) {
          rollbackCourseRef.current = accepted.courseId;
          selectCourse(accepted.courseId);
          setSearchParams(new URLSearchParams(accepted.searchParams), { replace: true });
        }
      }
    }
    void loadCourseChapters(courseId);
    return () => {
      if (courseLoadRequestRef.current === requestId) courseLoadRequestRef.current += 1;
    };
  }, [loadChapters, loadSources, selectCourse, setSearchParams, store.selectedCourseId]);
  useEffect(() => {
    if (activeChapterId !== null) return;
    let requestId: number | null = null;
    async function chooseInitial() {
      if (!initialChapterId) return;
      const request = loadChapterContent(initialChapterId);
      requestId = request.requestId;
      if (!(await request.result)) return;
      const nextSearchParams = searchParamsForChapter(searchParams, initialChapterId);
      const nextSearch = nextSearchParams.toString();
      acceptNavigation(acceptedCourseId, initialChapterId, nextSearch);
      if (nextSearch !== searchParams.toString()) setSearchParams(nextSearchParams, { replace: true });
    }
    void chooseInitial();
    return () => {
      if (requestId !== null) cancelChapterContentLoad(requestId);
    };
  }, [
    acceptNavigation,
    acceptedCourseId,
    activeChapterId,
    cancelChapterContentLoad,
    initialChapterId,
    loadChapterContent,
    searchParams,
    setSearchParams,
  ]);
  useEffect(() => {
    const requestedCourseId = store.selectedCourseId;
    const requestedSearch = searchParams.toString();
    if (rejectedChapterSearchRef.current && rejectedChapterSearchRef.current !== requestedSearch) {
      rejectedChapterSearchRef.current = null;
    }
    if (requestedCourseId !== acceptedCourseId) {
      const targetChapterId = requestedCourseInitialChapterId;
      if (!requestedCourseId || !requestedCourseReady) return;
      if (!confirmLeave()) {
        if (acceptedCourseId) selectCourse(acceptedCourseId);
        setSearchParams(new URLSearchParams(acceptedNavigation.searchParams), { replace: true });
        return;
      }

      const nextSearchParams = searchParamsForChapter(searchParams, targetChapterId);
      acceptNavigation(requestedCourseId, targetChapterId, nextSearchParams.toString());
      setSearchParams(nextSearchParams, { replace: true });
      void loadChapterContent(targetChapterId).result;
      return;
    }
    if (activeChapterId === null) {
      if (requestedCourseReady && !initialChapterId) {
        const nextSearchParams = searchParamsForChapter(searchParams, null);
        const nextSearch = nextSearchParams.toString();
        if (acceptedNavigation.searchParams !== nextSearch) {
          setAcceptedNavigation((current) => ({ ...current, searchParams: nextSearch }));
        }
        if (searchParams.toString() !== nextSearch) setSearchParams(nextSearchParams, { replace: true });
      }
      return;
    }
    const requestedTarget =
      requestedChapterId &&
      requestedChapterId !== activeChapterId &&
      courseChapters.some((chapter) => chapter.id === requestedChapterId)
        ? requestedChapterId
        : null;
    const activeChapterIsValid = courseChapters.some((chapter) => chapter.id === activeChapterId);
    const targetChapterId = requestedTarget ?? (!activeChapterIsValid ? initialChapterId : null);
    if (!targetChapterId || targetChapterId === activeChapterId) {
      if (activeChapterIsValid) {
        const nextSearchParams = searchParamsForChapter(searchParams, activeChapterId);
        const nextSearch = nextSearchParams.toString();
        if (acceptedNavigation.searchParams !== nextSearch) {
          setAcceptedNavigation((current) => ({ ...current, searchParams: nextSearch }));
        }
        if (searchParams.toString() !== nextSearch) setSearchParams(nextSearchParams, { replace: true });
      }
      return;
    }
    if (rejectedChapterSearchRef.current === requestedSearch) return;
    if (!confirmLeave()) {
      if (requestedTarget) {
        rejectedChapterSearchRef.current = requestedSearch;
        setSearchParams(new URLSearchParams(acceptedNavigation.searchParams), { replace: true });
      }
      return;
    }

    const nextSearchParams = searchParamsForChapter(searchParams, targetChapterId);
    const nextAcceptedSearchParams = nextSearchParams.toString();
    acceptNavigation(acceptedCourseId, targetChapterId, nextAcceptedSearchParams);
    if (searchParams.toString() !== nextAcceptedSearchParams) setSearchParams(nextSearchParams, { replace: true });
    void loadChapterContent(targetChapterId).result;
  }, [
    acceptNavigation,
    acceptedCourseId,
    acceptedNavigation.searchParams,
    activeChapterId,
    confirmLeave,
    courseChapters,
    initialChapterId,
    loadChapterContent,
    requestedCourseInitialChapterId,
    requestedCourseReady,
    requestedChapterId,
    searchParams,
    selectCourse,
    setSearchParams,
    store.selectedCourseId,
  ]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const chooseChapter = (chapterId: string) => {
    if (!confirmLeave()) return;
    const nextSearchParams = new URLSearchParams(searchParams);
    nextSearchParams.set("chapterId", chapterId);
    acceptNavigation(acceptedCourseId, chapterId, nextSearchParams.toString());
    setSearchParams(nextSearchParams);
    void loadChapterContent(chapterId).result;
  };
  const runHybrid = async () => {
    const chapterId = activeChapterId;
    const editorEpoch = navigationEpoch;
    if (!chapterId || !confirmLeave()) return;
    const requestId = ++hybridRunRequestRef.current;
    const isCurrentRun = () => {
      const accepted = acceptedNavigationRef.current;
      return (
        hybridRunRequestRef.current === requestId && accepted.chapterId === chapterId && accepted.epoch === editorEpoch
      );
    };
    setRunningHybrid(true);
    setError(null);
    try {
      await store.runHybridChapter(chapterId);
      if (!isCurrentRun()) return;
      await store.loadChapterRuns(chapterId);
    } catch (reason) {
      if (isCurrentRun()) setError(message(reason, "混合精读启动失败"));
    } finally {
      if (isCurrentRun()) setRunningHybrid(false);
    }
  };
  const saveBlock = async (
    chapterId: string,
    editorEpoch: number,
    block: NoteBlock,
    code: string,
    expected: string,
  ) => {
    await store.saveChapterBlock(chapterId, block.kind, code, expected);
    setDirtyEditors((current) => {
      if (current[block.id] !== editorEpoch) return current;
      const next = { ...current };
      delete next[block.id];
      return next;
    });
    return true;
  };
  const trackDirty = useCallback(
    (blockId: string, editorEpoch: number, value: boolean) =>
      setDirtyEditors((current) => {
        if (value) return current[blockId] === editorEpoch ? current : { ...current, [blockId]: editorEpoch };
        if (current[blockId] !== editorEpoch) return current;
        const next = { ...current };
        delete next[blockId];
        return next;
      }),
    [],
  );
  const sourceRefs = useMemo(() => collectSources(blocks), [blocks]);
  const review = runs.find((run) => run.round_key === "review");
  const latestRun = [...runs].sort((a, b) => b.updated_at - a.updated_at)[0];
  const hasContent = blocks.length > 0;
  const displayedError = courseLoadState.error ?? chapterLoadError ?? error;

  return (
    <div className="animate-in space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-zinc-200 pb-5">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-zinc-900">章节精读工作台</h1>
          <p className="mt-1 break-words text-sm text-zinc-500">{activeChapter?.title ?? "查看已生成的章节精读结果"}</p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button
            type="button"
            onClick={runHybrid}
            disabled={
              !activeChapterId || !["CONFIRMED", "FAILED"].includes(activeChapter?.status ?? "") || runningHybrid
            }
            className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {runningHybrid ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}混合精读
          </button>
          <Link
            onClick={(event) => {
              if (!confirmLeave()) event.preventDefault();
            }}
            to="/workbench/cards"
            className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700"
          >
            查看卡片池
          </Link>
        </div>
      </header>
      {displayedError && (
        <p role="alert" className="border-l-2 border-red-500 bg-red-50 px-3 py-2 text-sm text-red-700">
          {displayedError}
        </p>
      )}
      {courseChapters.length > 0 && (
        <label className="block max-w-2xl text-xs text-zinc-500">
          选择章节
          <select
            aria-label="选择章节"
            value={activeChapterId ?? ""}
            onChange={(event) => chooseChapter(event.target.value)}
            className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 focus:outline-none focus:ring-2 focus:ring-zinc-200"
          >
            <option value="" disabled>
              选择一个章节
            </option>
            {chapterGroups.map(({ source, chapters }) => (
              <optgroup key={source.id} label={`《${source.title}》`}>
                {chapters.map((chapter) => (
                  <option key={chapter.id} value={chapter.id}>
                    {chapterOptionLabel(source, chapter)}（{chapter.status}）
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
      )}
      {!hasContent ? (
        <EmptyState />
      ) : (
        <div className="grid min-w-0 gap-6 min-[1100px]:grid-cols-[minmax(0,1fr)_260px]">
          <main className="min-w-0 divide-y divide-zinc-200">
            {CONTENT_KINDS.map((kind) => {
              const block = blocks.find((item) => item.kind === kind);
              return <ContentSection key={kind} block={block} />;
            })}
            <section className="py-6">
              <h2 className="text-base font-semibold">来源</h2>
              <div className="mt-3 flex flex-wrap gap-2">
                {sourceRefs.length ? (
                  sourceRefs.map((source) => (
                    <span
                      key={source}
                      className="border-l-2 border-emerald-500 bg-emerald-50 px-3 py-1.5 text-sm text-emerald-900"
                    >
                      {source}
                    </span>
                  ))
                ) : (
                  <span className="text-sm text-zinc-400">未标注来源</span>
                )}
              </div>
            </section>
            <section className="py-6">
              <h2 className="text-base font-semibold">审核结果</h2>
              <ReviewResult run={review} />
            </section>
            {(["knowledge_mermaid", "application_mermaid"] as const).map((kind) => {
              const block = blocks.find((item) => item.kind === kind);
              return block && activeChapterId ? (
                <section key={`${navigationEpoch}:${block.id}`} className="py-6">
                  <MermaidEditor
                    title={block.title}
                    initial={block.body}
                    onSave={(code, expected) => saveBlock(activeChapterId, navigationEpoch, block, code, expected)}
                    onDirtyChange={(value) => trackDirty(block.id, navigationEpoch, value)}
                  />
                </section>
              ) : null;
            })}
          </main>
          <RunHistory runs={runs} latestRun={latestRun} onRerun={runHybrid} running={runningHybrid} />
        </div>
      )}
    </div>
  );
}

function message(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}
function ContentSection({ block }: { block: NoteBlock | undefined }) {
  return (
    <section className="py-6">
      <h2 className="text-base font-semibold">{block?.title ?? "未生成内容"}</h2>
      <div className="prose prose-zinc mt-3 max-w-none break-words text-sm leading-7 text-zinc-700">
        {block ? <ReactMarkdown>{block.body}</ReactMarkdown> : <p className="text-zinc-400">暂无内容</p>}
      </div>
    </section>
  );
}
function collectSources(blocks: NoteBlock[]) {
  const result = new Set<string>();
  for (const block of blocks)
    for (const match of block.body.matchAll(SOURCE_RE)) result.add(`《${match[1]}》·第 ${match[2]} 章`);
  return [...result];
}
function ReviewResult({ run }: { run: ChapterRun | undefined }) {
  if (!run) return <p className="mt-3 text-sm text-zinc-400">暂无审核记录</p>;
  const failed = run.status === "FAILED";
  return (
    <div
      role={failed ? "alert" : undefined}
      className={`mt-3 border-l-2 px-3 py-2 text-sm ${failed ? "border-red-500 bg-red-50 text-red-800" : "border-emerald-500 bg-emerald-50 text-emerald-800"}`}
    >
      {failed ? run.error || "审核未通过" : run.output || "审核通过"}
    </div>
  );
}
function RunHistory({
  runs,
  latestRun,
  onRerun,
  running,
}: {
  runs: ChapterRun[];
  latestRun: ChapterRun | undefined;
  onRerun: () => Promise<void>;
  running: boolean;
}) {
  return (
    <aside
      aria-label="精读轮次历史"
      className="self-start border-t border-zinc-200 min-[1100px]:sticky min-[1100px]:top-5 min-[1100px]:border-l min-[1100px]:border-t-0 min-[1100px]:pl-5"
    >
      <h2 className="py-4 text-sm font-semibold">精读轮次历史</h2>
      <ol className="space-y-1 pb-5">
        {runs.length ? (
          runs.map((run) => (
            <li key={run.id} className="flex min-w-0 items-start gap-2 border-b border-zinc-100 py-3">
              <RunIcon run={run} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="text-sm font-medium">{ROUND_LABELS[run.round_key] ?? run.round_key}</span>
                  {latestRun?.id === run.id && <span className="text-xs text-blue-700">当前轮</span>}
                </div>
                <p
                  className={`mt-1 text-xs ${run.status === "FAILED" ? "text-red-700" : run.stale ? "text-amber-700" : "text-zinc-500"}`}
                >
                  {run.status === "FAILED"
                    ? "失败"
                    : run.status === "RUNNING"
                      ? "进行中"
                      : run.stale
                        ? "结果已过期"
                        : "已完成"}
                </p>
                {run.status === "FAILED" && run.error && (
                  <p className="mt-1 break-words text-xs text-red-700">{run.error}</p>
                )}
                {(run.status === "FAILED" || run.stale) && (
                  <button
                    type="button"
                    disabled={running}
                    onClick={() => void onRerun()}
                    className="mt-2 text-xs text-emerald-700 underline disabled:opacity-40"
                  >
                    从{ROUND_LABELS[run.round_key] ?? run.round_key}轮重跑
                  </button>
                )}
              </div>
            </li>
          ))
        ) : (
          <li className="text-sm text-zinc-400">暂无运行记录</li>
        )}
      </ol>
    </aside>
  );
}
function RunIcon({ run }: { run: ChapterRun }) {
  if (run.status === "FAILED") return <XCircle size={16} className="mt-0.5 shrink-0 text-red-500" />;
  if (run.status === "RUNNING") return <Loader2 size={16} className="mt-0.5 shrink-0 animate-spin text-blue-500" />;
  if (run.stale) return <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />;
  if (run.status === "COMPLETED") return <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-600" />;
  return <Circle size={16} className="mt-0.5 shrink-0 text-zinc-400" />;
}
function EmptyState() {
  return (
    <div className="border border-dashed border-zinc-300 bg-white px-8 py-12 text-center">
      <p className="text-sm font-medium text-zinc-700">还没有精读结果</p>
      <p className="mt-1 text-xs text-zinc-400">先确认章节并运行精读。</p>
      <Link
        to="/workbench/chapters"
        className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white"
      >
        去章节确认
      </Link>
    </div>
  );
}
