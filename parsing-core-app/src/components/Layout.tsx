import { useEffect, useRef, useState } from "react";
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
  Power,
  RefreshCw,
  Search,
  Settings as SettingsIcon,
  Sparkles,
} from "lucide-react";
import {
  getServiceStatus,
  requestForceExitConfirmation,
  retryExitCleanup,
  retryService,
  type ServiceStatus,
} from "../api/runtime";
import { useWorkbenchStore } from "../store/useWorkbenchStore";
import SourceChapterTree from "./workbench/SourceChapterTree";
import { createSourceChapterGroups } from "./workbench/sourceChapterGroups";
import { buildSearchResults, type SearchResult } from "./layoutSearch";

const nav = [
  { to: "/", label: "开始", icon: Home },
  { to: "/workbench", label: "课程精读", icon: Sparkles },
  { to: "/submit", label: "资料导入", icon: Plus },
  { to: "/workbench/settings", label: "精读设置", icon: SettingsIcon },
];

const SERVICE_STATUS_POLL_MS = 1500;
const SERVICE_STATUS_TIMEOUT_MS = 5000;
const SERVICE_STATUS_IN_FLIGHT_LIMIT = 2;
const SERVICE_STATUS_LIMIT_MESSAGE = "服务状态查询持续无响应；已有后台查询仍在执行，请重启应用";
const SERVICE_RETRY_UI_TIMEOUT_MS = 10_000;
const SERVICE_RETRY_PENDING_MESSAGE = "重试仍在执行；若长时间无响应，请重启应用";

export interface WorkbenchOutletContext {
  coursesLoading: boolean;
  coursesError: string | null;
  serviceUnavailable: boolean;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) return error.message;
  if (
    typeof error === "object" &&
    error !== null &&
    "message" in error &&
    typeof error.message === "string" &&
    error.message
  )
    return error.message;
  if (typeof error === "string" && error) return error;
  return fallback;
}

function withTimeout<T>(request: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("服务状态查询超时")), timeoutMs);
    request.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function throwIfAborted(signal: AbortSignal): void {
  if (!signal.aborted) return;
  throw signal.reason ?? new DOMException("The operation was aborted", "AbortError");
}

export function ServiceStatusView({
  service,
  onRetry,
  retrying = false,
  onRetryExitCleanup,
  onForceQuit,
  exitCleanupRetrying = false,
  forceQuitting = false,
  exitCleanupError = null,
  forceQuitError = null,
}: {
  service: ServiceStatus;
  onRetry: () => void;
  retrying?: boolean;
  onRetryExitCleanup?: () => void;
  onForceQuit?: () => void;
  exitCleanupRetrying?: boolean;
  forceQuitting?: boolean;
  exitCleanupError?: string | null;
  forceQuitError?: string | null;
}) {
  const forceExitAvailable = service.forceExitAvailable === true;
  const exitActionPending = exitCleanupRetrying || forceQuitting;
  return (
    <div className="flex items-start gap-2 text-xs text-zinc-500" aria-live="polite">
      <span className="relative flex h-2 w-2">
        {service.state === "running" && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
        )}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${service.state === "running" && !forceExitAvailable ? "bg-emerald-500" : forceExitAvailable || service.state === "failed" || service.state === "offline" ? "bg-red-500" : "bg-amber-500"}`}
        />
      </span>
      <div className="min-w-0">
        <p>
          {forceExitAvailable
            ? "退出清理失败 / Shutdown Cleanup Failed"
            : service.state === "running"
              ? `服务运行中 :${service.port}`
              : service.state === "offline"
                ? "本地服务不可用"
                : service.state === "failed"
                  ? "服务启动失败"
                  : service.state === "restarting"
                    ? "服务正在重启"
                    : "服务正在启动"}
        </p>
        {service.state === "offline" && !forceExitAvailable && (
          <p className="mt-1 break-words text-red-600">{service.error?.message ?? "请启动本地服务后重试"}</p>
        )}
        {(service.state === "failed" || forceExitAvailable) && (
          <>
            <p className="mt-1 break-words text-red-600">{service.error?.message ?? "请查看运行日志"}</p>
            {service.logPath && <p className="mt-1 break-all text-zinc-400">日志：{service.logPath}</p>}
            {forceExitAvailable ? (
              <>
                <p className="mt-1 break-words text-zinc-500">
                  本地服务未完全退出，请先重试清理；强制退出仍需确认。 / Cleanup is incomplete; force quit requires
                  confirmation.
                </p>
                <div className="mt-2 flex flex-col items-start gap-2">
                  <button
                    type="button"
                    onClick={onRetryExitCleanup}
                    disabled={exitActionPending || !onRetryExitCleanup}
                    className="inline-flex items-center gap-1 font-medium text-emerald-700 hover:text-emerald-800 disabled:cursor-wait disabled:text-zinc-400"
                  >
                    <RefreshCw size={12} className={exitCleanupRetrying ? "animate-spin" : undefined} />
                    {exitCleanupRetrying ? "正在清理 / Cleaning Up" : "再次清理并退出 / Retry Cleanup & Exit"}
                  </button>
                  <button
                    type="button"
                    onClick={onForceQuit}
                    disabled={exitActionPending || !onForceQuit}
                    className="inline-flex items-center gap-1 font-medium text-red-700 hover:text-red-800 disabled:cursor-wait disabled:text-zinc-400"
                  >
                    <Power size={12} className={forceQuitting ? "animate-pulse" : undefined} />
                    {forceQuitting ? "等待确认 / Awaiting Confirmation" : "强制退出 / Force Quit"}
                  </button>
                </div>
                {exitCleanupError && (
                  <p role="alert" className="mt-2 break-words text-red-600">
                    {exitCleanupError}
                  </p>
                )}
                {forceQuitError && (
                  <p role="alert" className="mt-2 break-words text-red-600">
                    {forceQuitError}
                  </p>
                )}
              </>
            ) : (
              <button
                type="button"
                onClick={onRetry}
                disabled={retrying}
                className="mt-2 inline-flex items-center gap-1 font-medium text-emerald-700 hover:text-emerald-800 disabled:cursor-wait disabled:text-zinc-400"
              >
                <RefreshCw size={12} className={retrying ? "animate-spin" : undefined} />
                {retrying ? "正在重试" : "重试启动"}
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default function Layout() {
  const [service, setService] = useState<ServiceStatus>({ state: "starting", port: 0 });
  const [courseLoad, setCourseLoad] = useState<WorkbenchOutletContext>({
    coursesLoading: true,
    coursesError: null,
    serviceUnavailable: false,
  });
  const [courseLoadAttempt, setCourseLoadAttempt] = useState(0);
  const [serviceRetrying, setServiceRetrying] = useState(false);
  const [exitCleanupRetrying, setExitCleanupRetrying] = useState(false);
  const [forceQuitting, setForceQuitting] = useState(false);
  const [exitCleanupError, setExitCleanupError] = useState<string | null>(null);
  const [forceQuitError, setForceQuitError] = useState<string | null>(null);
  const [courseDataError, setCourseDataError] = useState<{ courseId: string; message: string } | null>(null);
  const [courseDataLoadAttempt, setCourseDataLoadAttempt] = useState(0);
  const mounted = useRef(false);
  const serviceStatusAttempt = useRef<{ request: Promise<ServiceStatus>; generation: number } | null>(null);
  const serviceStatusRawRequests = useRef(new Set<Promise<ServiceStatus>>());
  const serviceStatusSequence = useRef(0);
  const latestAppliedServiceStatus = useRef(0);
  const serviceStatusGeneration = useRef(0);
  const serviceRetryRequest = useRef<Promise<void> | null>(null);
  const serviceRetryTimedOut = useRef(false);
  const exitActionInFlight = useRef<"cleanup" | "force-quit" | null>(null);
  const serviceDataSession = useRef<{
    state: ServiceStatus["state"];
    generation: number;
    controller: AbortController | null;
  }>({ state: "starting", generation: 0, controller: null });
  const courseLoadRequest = useRef<{ generation: number; request: Promise<void> } | null>(null);
  const courseDataLoadRequest = useRef<{ courseId: string; generation: number; request: Promise<void> } | null>(null);
  const [primaryOpen, setPrimaryOpen] = useState(() => window.innerWidth >= 1280);
  const [libraryOpen, setLibraryOpen] = useState(() => window.innerWidth >= 1024);
  const [search, setSearch] = useState("");
  const [activeResult, setActiveResult] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      let attempt = serviceStatusAttempt.current;
      try {
        if (!attempt) {
          if (serviceStatusRawRequests.current.size >= SERVICE_STATUS_IN_FLIGHT_LIMIT) {
            throw new Error(SERVICE_STATUS_LIMIT_MESSAGE);
          }

          const generation = serviceStatusGeneration.current;
          const sequence = ++serviceStatusSequence.current;
          const rawRequest = getServiceStatus();
          serviceStatusRawRequests.current.add(rawRequest);
          void rawRequest.then(
            (status) => {
              serviceStatusRawRequests.current.delete(rawRequest);
              if (
                mounted.current &&
                serviceRetryRequest.current === null &&
                generation === serviceStatusGeneration.current &&
                sequence >= latestAppliedServiceStatus.current
              ) {
                latestAppliedServiceStatus.current = sequence;
                setService(status);
              }
            },
            () => {
              serviceStatusRawRequests.current.delete(rawRequest);
            },
          );
          attempt = {
            request: withTimeout(rawRequest, SERVICE_STATUS_TIMEOUT_MS),
            generation,
          };
          serviceStatusAttempt.current = attempt;
        }
        await attempt.request;
      } catch (error) {
        if (
          active &&
          serviceRetryRequest.current === null &&
          (attempt?.generation ?? serviceStatusGeneration.current) === serviceStatusGeneration.current
        ) {
          setService({
            state: "failed",
            port: 0,
            error: { category: "status", message: errorMessage(error, "服务状态查询失败") },
          });
        }
      } finally {
        if (attempt && serviceStatusAttempt.current === attempt) serviceStatusAttempt.current = null;
        refreshing = false;
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, SERVICE_STATUS_POLL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const current = serviceDataSession.current;
    if (
      current.state === service.state &&
      (service.state !== "running" || (current.controller !== null && !current.controller.signal.aborted))
    ) {
      return;
    }

    current.controller?.abort(new DOMException("Service generation replaced", "AbortError"));
    const next = {
      state: service.state,
      generation: current.generation + 1,
      controller: service.state === "running" ? new AbortController() : null,
    };
    serviceDataSession.current = next;
  }, [service.state]);

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
    if (service.state !== "running") {
      const serviceUnavailable = service.state === "failed" || service.state === "offline";
      setCourseLoad({
        coursesLoading: !serviceUnavailable,
        coursesError: null,
        serviceUnavailable,
      });
      return;
    }
    const session = serviceDataSession.current;
    if (session.state !== "running" || !session.controller) return;
    const generation = session.generation;
    const signal = session.controller.signal;
    let active = true;
    setCourseLoad({ coursesLoading: true, coursesError: null, serviceUnavailable: false });

    const refreshCourses = async () => {
      const pending = courseLoadRequest.current;
      const entry =
        pending?.generation === generation
          ? pending
          : {
              generation,
              request: Promise.resolve().then(() => loadCourses(signal)),
            };
      courseLoadRequest.current = entry;
      try {
        await entry.request;
        if (active && serviceDataSession.current.generation === generation && !signal.aborted) {
          setCourseLoad({ coursesLoading: false, coursesError: null, serviceUnavailable: false });
        }
      } catch (error) {
        if (active && serviceDataSession.current.generation === generation && !signal.aborted) {
          setCourseLoad({
            coursesLoading: false,
            coursesError: errorMessage(error, "加载课程失败"),
            serviceUnavailable: false,
          });
        }
      } finally {
        if (courseLoadRequest.current === entry) courseLoadRequest.current = null;
      }
    };

    void refreshCourses();
    return () => {
      active = false;
    };
  }, [courseLoadAttempt, loadCourses, service.state]);

  useEffect(() => {
    if (!selectedCourseId || service.state !== "running") {
      setCourseDataError(null);
      return;
    }
    const session = serviceDataSession.current;
    if (session.state !== "running" || !session.controller) return;
    const generation = session.generation;
    const signal = session.controller.signal;
    let active = true;
    setCourseDataError(null);

    const refreshCourseData = async () => {
      const pending = courseDataLoadRequest.current;
      const entry =
        pending?.courseId === selectedCourseId && pending.generation === generation
          ? pending
          : {
              courseId: selectedCourseId,
              generation,
              request: Promise.resolve()
                .then(() =>
                  Promise.all([
                    loadSources(selectedCourseId, signal).then((items) => {
                      throwIfAborted(signal);
                      return Promise.all(items.map((source) => loadChapters(source.id, signal)));
                    }),
                    loadCourseCards(selectedCourseId, signal),
                  ]),
                )
                .then(() => undefined),
            };
      courseDataLoadRequest.current = entry;

      try {
        await entry.request;
        if (active && serviceDataSession.current.generation === generation && !signal.aborted) {
          setCourseDataError(null);
        }
      } catch (error) {
        if (active && serviceDataSession.current.generation === generation && !signal.aborted) {
          setCourseDataError({
            courseId: selectedCourseId,
            message: errorMessage(error, "课程资料加载失败"),
          });
        }
      } finally {
        if (courseDataLoadRequest.current?.request === entry.request) courseDataLoadRequest.current = null;
      }
    };

    void refreshCourseData();
    return () => {
      active = false;
    };
  }, [courseDataLoadAttempt, loadChapters, loadCourseCards, loadSources, selectedCourseId, service.state]);

  const handleServiceRetry = () => {
    if (serviceRetryRequest.current) {
      serviceRetryTimedOut.current = true;
      setServiceRetrying(false);
      setService((current) => ({
        ...current,
        state: "failed",
        error: { category: "retry", message: SERVICE_RETRY_PENDING_MESSAGE },
      }));
      return;
    }
    serviceStatusGeneration.current += 1;
    serviceRetryTimedOut.current = false;
    setServiceRetrying(true);

    let request: Promise<void>;
    try {
      request = retryService();
    } catch (error) {
      serviceStatusGeneration.current += 1;
      serviceRetryTimedOut.current = false;
      setServiceRetrying(false);
      setService((current) => ({
        ...current,
        state: "failed",
        error: { category: "retry", message: errorMessage(error, "服务重试失败") },
      }));
      return;
    }
    serviceRetryRequest.current = request;
    const uiTimer = window.setTimeout(() => {
      if (serviceRetryRequest.current !== request || !mounted.current) return;
      serviceRetryTimedOut.current = true;
      setServiceRetrying(false);
      setService((current) => ({
        ...current,
        state: "failed",
        error: { category: "retry", message: SERVICE_RETRY_PENDING_MESSAGE },
      }));
    }, SERVICE_RETRY_UI_TIMEOUT_MS);

    void request
      .then(
        () => {
          serviceStatusGeneration.current += 1;
          serviceRetryTimedOut.current = false;
          if (mounted.current) {
            setService((current) => ({ ...current, state: "restarting", error: null }));
          }
        },
        (error: unknown) => {
          serviceStatusGeneration.current += 1;
          serviceRetryTimedOut.current = false;
          if (mounted.current) {
            setService((current) => ({
              ...current,
              state: "failed",
              error: { category: "retry", message: errorMessage(error, "服务重试失败") },
            }));
          }
        },
      )
      .finally(() => {
        window.clearTimeout(uiTimer);
        if (serviceRetryRequest.current === request) serviceRetryRequest.current = null;
        if (mounted.current) setServiceRetrying(false);
      });
  };

  const handleRetryExitCleanup = () => {
    if (exitActionInFlight.current) return;
    exitActionInFlight.current = "cleanup";
    setExitCleanupError(null);
    setExitCleanupRetrying(true);

    let request: Promise<string | undefined>;
    try {
      request = Promise.resolve(retryExitCleanup());
    } catch (error) {
      exitActionInFlight.current = null;
      setExitCleanupRetrying(false);
      setExitCleanupError(errorMessage(error, "退出清理重试失败 / Exit cleanup retry failed"));
      return;
    }

    void request
      .catch((error: unknown) => {
        if (mounted.current) {
          setExitCleanupError(errorMessage(error, "退出清理重试失败 / Exit cleanup retry failed"));
        }
      })
      .finally(() => {
        if (exitActionInFlight.current === "cleanup") exitActionInFlight.current = null;
        if (mounted.current) setExitCleanupRetrying(false);
      });
  };

  const handleForceQuit = () => {
    if (exitActionInFlight.current) return;
    exitActionInFlight.current = "force-quit";
    setForceQuitError(null);
    setForceQuitting(true);

    let request: Promise<string | undefined>;
    try {
      request = Promise.resolve(requestForceExitConfirmation());
    } catch (error) {
      exitActionInFlight.current = null;
      setForceQuitting(false);
      setForceQuitError(errorMessage(error, "强制退出请求失败 / Force quit request failed"));
      return;
    }

    void request
      .catch((error: unknown) => {
        if (mounted.current) {
          setForceQuitError(errorMessage(error, "强制退出请求失败 / Force quit request failed"));
        }
      })
      .finally(() => {
        if (exitActionInFlight.current === "force-quit") exitActionInFlight.current = null;
        if (mounted.current) setForceQuitting(false);
      });
  };

  const openResult = (result: SearchResult) => {
    if (result.courseId) selectCourse(result.courseId);
    setSearch("");
    navigate(result.to);
  };

  return (
    <div className="flex h-screen overflow-hidden bg-white text-zinc-900">
      <aside
        aria-label="主导航"
        className={`${primaryOpen ? "fixed inset-y-0 left-0 z-40 flex xl:static" : "hidden"} w-[252px] shrink-0 flex-col border-r border-zinc-200 bg-zinc-50 shadow-xl xl:w-[292px] xl:shadow-none`}
      >
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
            <input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setActiveResult(0);
              }}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setActiveResult((current) => Math.min(current + 1, searchResults.length - 1));
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setActiveResult((current) => Math.max(current - 1, 0));
                }
                if (event.key === "Enter" && searchResults[activeResult]) {
                  event.preventDefault();
                  openResult(searchResults[activeResult]);
                }
                if (event.key === "Escape") setSearch("");
              }}
              role="combobox"
              aria-label="搜索课程、教材、章节、卡片"
              aria-expanded={searchResults.length > 0}
              aria-controls="global-search-results"
              placeholder="搜索课程、教材、章节、卡片"
              className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-zinc-400"
            />
          </div>
          {search && (
            <ul
              id="global-search-results"
              role="listbox"
              className="absolute left-4 right-4 top-11 z-50 max-h-80 overflow-y-auto border border-zinc-200 bg-white py-1 shadow-xl"
            >
              {searchResults.length ? (
                searchResults.map((result, index) => (
                  <li key={result.id} role="option" aria-selected={index === activeResult}>
                    <button
                      type="button"
                      onMouseEnter={() => setActiveResult(index)}
                      onClick={() => openResult(result)}
                      className={`w-full px-3 py-2 text-left ${index === activeResult ? "bg-emerald-50" : "hover:bg-zinc-50"}`}
                    >
                      <span className="block truncate text-sm font-medium">{result.label}</span>
                      <span className="block truncate text-xs text-zinc-500">
                        {result.kind} · {result.detail}
                      </span>
                    </button>
                  </li>
                ))
              ) : (
                <li className="px-3 py-3 text-sm text-zinc-500">没有匹配结果</li>
              )}
            </ul>
          )}
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
            <Link
              to="/workbench"
              className="rounded-md p-1 text-zinc-400 hover:bg-white hover:text-zinc-800"
              title="新建或选择课程"
            >
              <Plus size={15} />
            </Link>
          </div>
          <div className="space-y-1">
            {courses.length === 0 ? (
              <Link
                to="/workbench"
                className="block rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-3 text-sm text-zinc-500"
              >
                创建 MBA 课程库
              </Link>
            ) : (
              courses.map((course) => (
                <button
                  key={course.id}
                  type="button"
                  onClick={() => selectCourse(course.id)}
                  className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                    course.id === selectedCourseId
                      ? "bg-white font-medium text-zinc-900 shadow-sm ring-1 ring-zinc-200"
                      : "text-zinc-600 hover:bg-white"
                  }`}
                >
                  <Library size={16} className="shrink-0 text-blue-500" />
                  <span className="min-w-0 truncate">{course.title}</span>
                </button>
              ))
            )}
          </div>
          {courseLoad.coursesError && (
            <button
              type="button"
              onClick={() => setCourseLoadAttempt((attempt) => attempt + 1)}
              className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-red-700 hover:text-red-900"
            >
              <RefreshCw size={12} />
              重试课程列表
            </button>
          )}
        </div>

        <div className="mt-auto px-4 py-3">
          <ServiceStatusView
            service={service}
            onRetry={handleServiceRetry}
            retrying={serviceRetrying}
            onRetryExitCleanup={handleRetryExitCleanup}
            onForceQuit={handleForceQuit}
            exitCleanupRetrying={exitCleanupRetrying}
            forceQuitting={forceQuitting}
            exitCleanupError={exitCleanupError}
            forceQuitError={forceQuitError}
          />
        </div>
      </aside>

      {isWorkbench && (
        <aside
          aria-label="课程资料导航"
          className={`${libraryOpen ? "hidden lg:flex" : "hidden"} w-[280px] shrink-0 flex-col border-r border-zinc-200 bg-white xl:w-[330px]`}
        >
          <div className="flex h-16 items-center justify-between border-b border-zinc-100 px-5">
            <div className="min-w-0">
              <p className="text-xs text-zinc-400">资料库</p>
              <h2 className="truncate text-base font-semibold">{selectedCourse?.title ?? "课程精读"}</h2>
            </div>
            <Link
              to="/workbench/source"
              className="rounded-lg bg-emerald-500 p-2 text-white shadow-sm hover:bg-emerald-600"
              title="导入资料"
            >
              <Plus size={18} />
            </Link>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-4">
            {courseDataError?.courseId === selectedCourseId && (
              <div role="alert" className="mb-4 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-xs text-red-700">
                <p className="break-words">{courseDataError.message}</p>
                <button
                  type="button"
                  onClick={() => setCourseDataLoadAttempt((attempt) => attempt + 1)}
                  className="mt-2 inline-flex items-center gap-1 font-medium text-red-700 hover:text-red-900"
                >
                  <RefreshCw size={12} />
                  重试课程资料
                </button>
              </div>
            )}
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
                  pathname === "/workbench/chapters"
                    ? "bg-zinc-100 font-medium text-zinc-900"
                    : "text-zinc-600 hover:bg-zinc-50"
                }`}
              >
                <FolderOpen size={16} /> 教材
              </Link>
              {selectedCourseId && (
                <Link
                  to={`/workbench/courses/${selectedCourseId}/topics`}
                  className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${pathname.includes("/topics") ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"}`}
                >
                  <Layers3 size={16} /> 课程主题
                </Link>
              )}
              {selectedCourseId && (
                <Link
                  to={`/workbench/courses/${selectedCourseId}/fusion`}
                  className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${pathname.includes("/fusion") ? "bg-zinc-100 font-medium text-zinc-900" : "text-zinc-600 hover:bg-zinc-50"}`}
                >
                  <BookOpen size={16} /> 融合精读
                </Link>
              )}
              <Link
                to="/workbench/cards"
                className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${
                  pathname === "/workbench/cards"
                    ? "bg-zinc-100 font-medium text-zinc-900"
                    : "text-zinc-600 hover:bg-zinc-50"
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
                  <Link
                    to="/workbench/source"
                    className="block rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-sm text-zinc-500"
                  >
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
                  <Link
                    to="/workbench/chapters"
                    className="block rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-sm text-zinc-500"
                  >
                    识别并确认章节
                  </Link>
                ) : (
                  <SourceChapterTree
                    groups={chapterGroups}
                    chapterHref={(chapterId) => `/workbench/chapter?chapterId=${chapterId}`}
                  />
                )}
              </div>
            </div>
          </div>
        </aside>
      )}

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-white">
        <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-zinc-200 px-4 sm:px-6 lg:px-7">
          <div className="flex min-w-0 items-center gap-2">
            <button
              type="button"
              onClick={() => setPrimaryOpen((open) => !open)}
              aria-label={primaryOpen ? "收起主导航" : "展开主导航"}
              title={primaryOpen ? "收起主导航" : "展开主导航"}
              className="flex h-9 w-9 shrink-0 items-center justify-center text-zinc-500 hover:bg-zinc-100"
            >
              {primaryOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
            </button>
            {isWorkbench && (
              <button
                type="button"
                onClick={() => setLibraryOpen((open) => !open)}
                aria-label={libraryOpen ? "收起课程资料栏" : "展开课程资料栏"}
                title={libraryOpen ? "收起课程资料栏" : "展开课程资料栏"}
                className="hidden h-9 w-9 shrink-0 items-center justify-center text-zinc-500 hover:bg-zinc-100 lg:flex"
              >
                {libraryOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
              </button>
            )}
            <div className="min-w-0">
              <p className="text-xs text-zinc-400">{isWorkbench ? "精读文档" : "文档解析"}</p>
              <h1 className="truncate text-sm font-semibold">
                {isWorkbench ? (selectedCourse?.title ?? "课程精读") : "PDF2MD"}
              </h1>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 whitespace-nowrap">
            <Link
              to="/workbench/chapter"
              className="rounded-lg border border-zinc-200 px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-50"
            >
              打开精读
            </Link>
            <Link
              to="/workbench/source"
              className="rounded-lg bg-emerald-500 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-600"
            >
              导入资料
            </Link>
          </div>
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto">
          <div
            className={`${isWorkbench ? "mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6 xl:px-8 xl:py-8" : "mx-auto max-w-4xl px-8 py-8"}`}
          >
            <Outlet context={courseLoad} />
          </div>
        </main>
      </div>
    </div>
  );
}
