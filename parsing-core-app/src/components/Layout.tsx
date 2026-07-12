import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  BookOpen,
  ChevronDown,
  FolderOpen,
  Home,
  Library,
  Layers3,
  NotebookTabs,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  Settings as SettingsIcon,
  Sparkles,
} from "lucide-react";
import { getServiceStatus, retryService, type ServiceStatus } from "../api/runtime";
import { useWorkbenchStore } from "../store/useWorkbenchStore";
import SourceChapterTree, { createSourceChapterGroups } from "./workbench/SourceChapterTree";
import type { Card, Chapter, Course, Source } from "../api/workbenchTypes";

interface SearchData {
  courses: Course[];
  sources: Record<string, Source[]>;
  chapters: Record<string, Chapter[]>;
  cardsByCourse: Record<string, Card[]>;
}

export interface SearchResult { id: string; kind: string; label: string; detail: string; to: string; courseId?: string }

export function buildSearchResults(data: SearchData, query: string): SearchResult[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return [];
  const matches = (values: string[]) => values.some((value) => value.toLocaleLowerCase().includes(needle));
  return [
    ...data.courses.filter((course) => matches([course.title, course.description])).map((course) => ({ id: `course:${course.id}`, kind: "课程", label: course.title, detail: course.description, to: "/workbench", courseId: course.id })),
    ...Object.values(data.sources).flat().filter((source) => matches([source.title, source.file_path])).map((source) => ({ id: `source:${source.id}`, kind: "教材", label: source.title, detail: source.file_path, to: "/workbench/chapters", courseId: source.course_id })),
    ...Object.values(data.chapters).flat().filter((chapter) => matches([chapter.title])).map((chapter) => ({ id: `chapter:${chapter.id}`, kind: "章节", label: chapter.title, detail: "打开章节精读", to: `/workbench/chapter?chapterId=${chapter.id}`, courseId: chapter.course_id })),
    ...Object.entries(data.cardsByCourse).flatMap(([courseId, cards]) => cards.filter((card) => matches([card.title, card.content, card.origin_title])).map((card) => ({ id: `card:${card.id}`, kind: "卡片", label: card.title, detail: card.origin_title, to: `/workbench/cards?cardId=${card.id}`, courseId }))),
  ].slice(0, 12);
}

const nav = [
  { to: "/", label: "开始", icon: Home },
  { to: "/workbench", label: "课程精读", icon: Sparkles },
  { to: "/submit", label: "资料导入", icon: Plus },
  { to: "/workbench/settings", label: "精读设置", icon: SettingsIcon },
];

export function ServiceStatusView({ service, onRetry }: { service: ServiceStatus; onRetry: () => void }) {
  return (
    <div className="flex items-start gap-2 text-xs text-zinc-500" aria-live="polite">
      <span className="relative flex h-2 w-2">
        {service.state === "running" && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${service.state === "running" ? "bg-emerald-500" : service.state === "failed" ? "bg-red-500" : "bg-amber-500"}`} />
      </span>
      <div className="min-w-0">
        <p>{service.state === "running" ? `服务运行中 :${service.port}` : service.state === "failed" ? "服务启动失败" : service.state === "restarting" ? "服务正在重启" : "服务正在启动"}</p>
        {service.state === "failed" && <><p className="mt-1 break-words text-red-600">{service.error?.message ?? "请查看运行日志"}</p>{service.logPath && <p className="mt-1 break-all text-zinc-400">日志：{service.logPath}</p>}<button type="button" onClick={onRetry} className="mt-2 font-medium text-emerald-700 hover:text-emerald-800">重试启动</button></>}
      </div>
    </div>
  );
}

export default function Layout() {
  const [service, setService] = useState<ServiceStatus>({ state: "starting", port: 0 });
  const [primaryOpen, setPrimaryOpen] = useState(() => window.innerWidth >= 1280);
  const [libraryOpen, setLibraryOpen] = useState(() => window.innerWidth >= 1024);
  const [search, setSearch] = useState("");
  const [activeResult, setActiveResult] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    const refresh = () => void getServiceStatus().then(setService).catch((error) => setService({ state: "failed", port: 0, error: { category: "status", message: String(error) } }));
    refresh();
    const timer = window.setInterval(refresh, 1500);
    return () => window.clearInterval(timer);
  }, []);
  const { pathname } = useLocation();
  const isWorkbench = pathname.startsWith("/workbench");
  const {
    chapters,
    cardsByCourse,
    courses,
    loadChapters,
    loadCourses,
    loadCourseCards,
    loadSources,
    selectCourse,
    selectedCourseId,
    sources,
  } = useWorkbenchStore();
  const selectedCourse = courses.find((course) => course.id === selectedCourseId) ?? null;
  const selectedSources = selectedCourseId ? (sources[selectedCourseId] ?? []) : [];
  const chapterGroups = createSourceChapterGroups(selectedSources, chapters);
  const selectedChapterCount = chapterGroups.reduce((total, group) => total + group.chapters.length, 0);
  const searchResults = buildSearchResults({ courses, sources, chapters, cardsByCourse }, search);

  useEffect(() => {
    loadCourses().catch(() => undefined);
  }, [loadCourses]);

  useEffect(() => {
    if (!selectedCourseId) return;
    Promise.all([loadSources(selectedCourseId).then((items) => Promise.all(items.map((source) => loadChapters(source.id)))), loadCourseCards(selectedCourseId)])
      .catch(() => undefined);
  }, [loadChapters, loadCourseCards, loadSources, selectedCourseId]);

  const openResult = (result: SearchResult) => {
    if (result.courseId) selectCourse(result.courseId);
    setSearch("");
    navigate(result.to);
  };

  return (
    <div className="flex h-screen overflow-hidden bg-white text-zinc-900">
      <aside aria-label="主导航" className={`${primaryOpen ? "fixed inset-y-0 left-0 z-40 flex xl:static" : "hidden"} w-[252px] shrink-0 flex-col border-r border-zinc-200 bg-zinc-50 shadow-xl xl:w-[292px] xl:shadow-none`}>
        <div className="flex h-16 items-center gap-3 px-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-500 text-white shadow-sm shadow-emerald-200">
            <BookOpen size={20} strokeWidth={2.2} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="truncate text-base font-semibold">PDF2MD</span>
              <ChevronDown size={14} className="text-zinc-400" />
            </div>
            <p className="truncate text-xs text-zinc-500">MBA 课程精读工作台</p>
          </div>
        </div>

        <div className="relative px-4 pb-3">
          <div className="flex h-10 items-center gap-2 rounded-lg bg-white px-3 text-sm text-zinc-500 shadow-sm ring-1 ring-zinc-200 focus-within:ring-2 focus-within:ring-emerald-500">
            <Search size={16} />
            <input value={search} onChange={(event) => { setSearch(event.target.value); setActiveResult(0); }} onKeyDown={(event) => {
              if (event.key === "ArrowDown") { event.preventDefault(); setActiveResult((current) => Math.min(current + 1, searchResults.length - 1)); }
              if (event.key === "ArrowUp") { event.preventDefault(); setActiveResult((current) => Math.max(current - 1, 0)); }
              if (event.key === "Enter" && searchResults[activeResult]) { event.preventDefault(); openResult(searchResults[activeResult]); }
              if (event.key === "Escape") setSearch("");
            }} role="combobox" aria-label="搜索课程、教材、章节、卡片" aria-expanded={searchResults.length > 0} aria-controls="global-search-results" placeholder="搜索课程、教材、章节、卡片" className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-zinc-400" />
          </div>
          {search && <ul id="global-search-results" role="listbox" className="absolute left-4 right-4 top-11 z-50 max-h-80 overflow-y-auto border border-zinc-200 bg-white py-1 shadow-xl">{searchResults.length ? searchResults.map((result, index) => <li key={result.id} role="option" aria-selected={index === activeResult}><button type="button" onMouseEnter={() => setActiveResult(index)} onClick={() => openResult(result)} className={`w-full px-3 py-2 text-left ${index === activeResult ? "bg-emerald-50" : "hover:bg-zinc-50"}`}><span className="block truncate text-sm font-medium">{result.label}</span><span className="block truncate text-xs text-zinc-500">{result.kind} · {result.detail}</span></button></li>) : <li className="px-3 py-3 text-sm text-zinc-500">没有匹配结果</li>}</ul>}
        </div>

        <nav className="space-y-1 px-3 py-2">
          {nav.map(({ to, label, icon: Icon }) => {
            const active = to === "/" ? pathname === to : pathname === to || pathname.startsWith(`${to}/`);
            return (
              <Link
                key={to}
                to={to}
                className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors ${
                  active
                    ? "bg-white font-medium text-zinc-900 shadow-sm ring-1 ring-zinc-200"
                    : "text-zinc-600 hover:bg-white hover:text-zinc-900"
                }`}
              >
                <Icon size={18} strokeWidth={active ? 2 : 1.6} />
                {label}
              </Link>
            );
          })}
        </nav>

        <div className="mt-5 border-t border-zinc-200 px-4 py-4">
          <div className="mb-3 flex items-center justify-between text-xs font-medium text-zinc-500">
            <span>知识库</span>
            <Link to="/workbench" className="rounded-md p-1 text-zinc-400 hover:bg-white hover:text-zinc-800" title="新建或选择课程">
              <Plus size={15} />
            </Link>
          </div>
          <div className="space-y-1">
            {courses.length === 0 ? (
              <Link to="/workbench" className="block rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-3 text-sm text-zinc-500">
                创建 MBA 课程库
              </Link>
            ) : (
              courses.map((course) => (
                <button
                  key={course.id}
                  type="button"
                  onClick={() => selectCourse(course.id)}
                  className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                    course.id === selectedCourseId ? "bg-white font-medium text-zinc-900 shadow-sm ring-1 ring-zinc-200" : "text-zinc-600 hover:bg-white"
                  }`}
                >
                  <Library size={16} className="shrink-0 text-blue-500" />
                  <span className="min-w-0 truncate">{course.title}</span>
                </button>
              ))
            )}
          </div>
        </div>

        <div className="mt-auto px-4 py-3">
          <ServiceStatusView service={service} onRetry={() => void retryService().then(() => setService((current) => ({ ...current, state: "restarting", error: null })))} />
        </div>
      </aside>

      {isWorkbench && (
        <aside aria-label="课程资料导航" className={`${libraryOpen ? "hidden lg:flex" : "hidden"} w-[280px] shrink-0 flex-col border-r border-zinc-200 bg-white xl:w-[330px]`}>
          <div className="flex h-16 items-center justify-between border-b border-zinc-100 px-5">
            <div className="min-w-0">
              <p className="text-xs text-zinc-400">资料库</p>
              <h2 className="truncate text-base font-semibold">{selectedCourse?.title ?? "课程精读"}</h2>
            </div>
            <Link to="/workbench/source" className="rounded-lg bg-emerald-500 p-2 text-white shadow-sm hover:bg-emerald-600" title="导入资料">
              <Plus size={18} />
            </Link>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-4">
            <div className="mb-5 space-y-1">
              <Link
                to="/workbench"
                className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${
                  pathname === "/workbench" ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"
                }`}
              >
                <Home size={16} /> 首页
              </Link>
              <Link
                to="/workbench/chapters"
                className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${
                  pathname === "/workbench/chapters" ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"
                }`}
              >
                <FolderOpen size={16} /> 教材
              </Link>
              {selectedCourseId && <Link to={`/workbench/courses/${selectedCourseId}/topics`} className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${pathname.includes("/topics") ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"}`}><Layers3 size={16} /> 课程主题</Link>}
              {selectedCourseId && <Link to={`/workbench/courses/${selectedCourseId}/fusion`} className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${pathname.includes("/fusion") ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"}`}><BookOpen size={16} /> 融合精读</Link>}
              <Link
                to="/workbench/cards"
                className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${
                  pathname === "/workbench/cards" ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"
                }`}
              >
                <NotebookTabs size={16} /> 写作卡片
              </Link>
            </div>

            <div className="mb-5">
              <div className="mb-2 flex items-center justify-between text-xs font-medium text-zinc-400">
                <span>教材资料</span>
                <span>{selectedSources.length}</span>
              </div>
              <div className="space-y-1">
                {selectedSources.length === 0 ? (
                  <Link to="/workbench/source" className="block rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-sm text-zinc-500">
                    导入 PDF / Word / PPT
                  </Link>
                ) : (
                  selectedSources.map((source) => (
                    <div key={source.id} className="rounded-lg px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-50">
                      <p className="truncate font-medium">{source.title}</p>
                      <p className="mt-0.5 truncate text-xs text-zinc-400">{source.status}</p>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div>
              <div className="mb-2 flex items-center justify-between text-xs font-medium text-zinc-400">
                <span>章节</span>
                <span>{selectedChapterCount}</span>
              </div>
              <div className="space-y-1">
                {selectedChapterCount === 0 ? (
                  <Link to="/workbench/chapters" className="block rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-sm text-zinc-500">
                    识别并确认章节
                  </Link>
                ) : (
                  <SourceChapterTree groups={chapterGroups} chapterHref={(chapterId) => `/workbench/chapter?chapterId=${chapterId}`} />
                )}
              </div>
            </div>
          </div>
        </aside>
      )}

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-white">
        <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-zinc-200 px-4 sm:px-6 lg:px-7">
          <div className="flex min-w-0 items-center gap-2">
            <button type="button" onClick={() => setPrimaryOpen((open) => !open)} aria-label={primaryOpen ? "收起主导航" : "展开主导航"} title={primaryOpen ? "收起主导航" : "展开主导航"} className="flex h-9 w-9 shrink-0 items-center justify-center text-zinc-500 hover:bg-zinc-100">{primaryOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}</button>
            {isWorkbench && <button type="button" onClick={() => setLibraryOpen((open) => !open)} aria-label={libraryOpen ? "收起课程资料栏" : "展开课程资料栏"} title={libraryOpen ? "收起课程资料栏" : "展开课程资料栏"} className="hidden h-9 w-9 shrink-0 items-center justify-center text-zinc-500 hover:bg-zinc-100 lg:flex">{libraryOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}</button>}
            <div className="min-w-0">
            <p className="text-xs text-zinc-400">{isWorkbench ? "精读文档" : "文档解析"}</p>
            <h1 className="truncate text-sm font-semibold">{isWorkbench ? selectedCourse?.title ?? "课程精读" : "PDF2MD"}</h1>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 whitespace-nowrap">
            <Link to="/workbench/chapter" className="rounded-lg border border-zinc-200 px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-50">
              打开精读
            </Link>
            <Link to="/workbench/source" className="rounded-lg bg-emerald-500 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-600">
              导入资料
            </Link>
          </div>
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className={`${isWorkbench ? "mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6 xl:px-8 xl:py-8" : "mx-auto max-w-4xl px-8 py-8"}`}>
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
