import { ChangeEvent, DragEvent, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, FileText, Loader2, RefreshCw, Upload } from "lucide-react";
import type { Chapter, ImportedSource, Source } from "../../api/workbenchTypes";
import { SafeApiError } from "../../api/workbench";

type QueueStatus = "等待" | "导入" | "识别章节" | "成功" | "失败" | "结果待确认";
type QueuePhase = "import" | "detect";

interface QueueItem {
  key: string;
  name: string;
  title: string;
  path: string | null;
  status: QueueStatus;
  phase: QueuePhase;
  sourceId?: string;
  storedPath?: string;
  error?: string;
}

interface Props {
  courseId: string;
  currentSources: Source[];
  importSources: (courseId: string, paths: string[], titles?: string[]) => Promise<ImportedSource[]>;
  detectChapters: (sourceId: string) => Promise<Chapter[]>;
  loadSources: (courseId: string) => Promise<Source[]>;
}

const supportedExtension = /\.(pdf|doc|docx)$/i;
const browserPathError = "浏览器无法读取本地路径，请使用桌面客户端选择教材";
const uncertainImportError = "请刷新教材列表核对导入结果，系统不会自动重复导入";

function safeName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? "未命名教材";
}

function isAbsolutePath(path: string) {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

function filePath(file: File): string | null {
  const path = (file as File & { path?: unknown }).path;
  return typeof path === "string" && isAbsolutePath(path) ? path : null;
}

function titleFromName(name: string) {
  return name.replace(supportedExtension, "");
}

function canRetryImport(error: unknown) {
  return error instanceof SafeApiError && !["network", "protocol", "canceled"].includes(error.category);
}

export default function ImportTextbooks({ courseId, currentSources, importSources, detectChapters, loadSources }: Props) {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const runningGeneration = useRef<number | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    generation.current += 1;
    runningGeneration.current = null;
    setItems([]);
    setErrors([]);
    setDragActive(false);
  }, [courseId]);

  const enqueue = (candidates: Array<{ name: string; path: string | null }>) => {
    const nextErrors = candidates
      .filter(({ name }) => !supportedExtension.test(name))
      .map(({ name }) => `${name}：不支持此文件类型`);
    const valid = candidates.filter(({ name }) => supportedExtension.test(name));
    setItems((current) => {
      const keys = new Set(current.map((item) => item.key));
      const additions = valid.flatMap(({ name, path }) => {
        const key = path ?? `browser:${name}`;
        if (keys.has(key)) return [];
        keys.add(key);
        return [{
          key,
          name,
          title: titleFromName(name),
          path,
          status: path ? "等待" as const : "失败" as const,
          phase: "import" as const,
          error: path ? undefined : browserPathError,
        }];
      });
      return [...current, ...additions];
    });
    if (nextErrors.length) setErrors((current) => [...current, ...nextErrors]);
  };

  const enqueuePaths = (paths: string[]) => enqueue(paths.map((path) => ({
    name: safeName(path),
    path: isAbsolutePath(path) ? path : null,
  })));

  const enqueueNativeDrop = async (paths: string[]) => {
    const dropGeneration = generation.current;
    const supported = paths.filter((path) => supportedExtension.test(safeName(path)));
    const unsupported = paths.filter((path) => !supportedExtension.test(safeName(path)));
    if (unsupported.length) enqueuePaths(unsupported);
    if (!supported.length) return;
    const { invoke } = await import("@tauri-apps/api/core");
    const checks = await Promise.all(supported.map(async (path) => ({
      path,
      isFile: await invoke<boolean>("textbook_path_is_file", { path }),
    })));
    if (generation.current !== dropGeneration) return;
    const directories = checks.filter((item) => !item.isFile);
    if (directories.length) {
      setErrors((current) => [...current, ...directories.map(({ path }) => `${safeName(path)}：不支持导入文件夹`)]);
    }
    enqueuePaths(checks.filter((item) => item.isFile).map((item) => item.path));
  };

  useEffect(() => {
    if (!("__TAURI_INTERNALS__" in globalThis)) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    import("@tauri-apps/api/webview")
      .then(({ getCurrentWebview }) => getCurrentWebview().onDragDropEvent((event) => {
        const payload = event.payload;
        if (payload.type === "enter" || payload.type === "over") setDragActive(true);
        if (payload.type === "leave") setDragActive(false);
        if (payload.type === "drop") {
          setDragActive(false);
          enqueueNativeDrop(payload.paths).catch(() => setErrors((current) => [...current, "无法读取拖放文件"]));
        }
      }))
      .then((stop) => {
        if (disposed) stop();
        else unlisten = stop;
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  const chooseFiles = async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const paths = await invoke<string[]>("pick_textbooks");
      if (paths.length) enqueuePaths(paths);
    } catch {
      inputRef.current?.click();
    }
  };

  const webFiles = (event: ChangeEvent<HTMLInputElement>) => {
    enqueue(Array.from(event.target.files ?? []).map((file) => ({ name: file.name, path: filePath(file) })));
    event.target.value = "";
  };

  const dropped = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const droppedErrors: string[] = [];
    const candidates: Array<{ name: string; path: string | null }> = [];
    const transferItems = Array.from(event.dataTransfer.items ?? []);
    if (transferItems.length) {
      for (const item of transferItems) {
        const file = item.getAsFile();
        if (!file) continue;
        const entry = "webkitGetAsEntry" in item ? item.webkitGetAsEntry?.() : null;
        if (entry?.isDirectory) droppedErrors.push(`${file.name}：不支持导入文件夹`);
        else candidates.push({ name: file.name, path: filePath(file) });
      }
    } else {
      candidates.push(...Array.from(event.dataTransfer.files).map((file) => ({ name: file.name, path: filePath(file) })));
    }
    if (droppedErrors.length) setErrors((current) => [...current, ...droppedErrors]);
    enqueue(candidates);
  };

  const setItem = (key: string, patch: Partial<QueueItem>) =>
    setItems((current) => current.map((item) => (item.key === key ? { ...item, ...patch } : item)));

  const processItem = async (item: QueueItem) => {
    if (!item.path) return;
    const title = item.title.trim();
    if (!title) {
      setItem(item.key, { status: "失败", phase: "import", error: "教材名称不能为空" });
      return;
    }
    const taskGeneration = generation.current;
    const taskCourseId = courseId;
    const isCurrent = () => generation.current === taskGeneration;
    let sourceId = item.sourceId;
    try {
      if (!sourceId) {
        setItem(item.key, { status: "导入", phase: "import", error: undefined });
        const beforeSources = await loadSources(taskCourseId).catch(() => currentSources);
        if (!isCurrent()) return;
        let imported: ImportedSource | undefined;
        try {
          [imported] = await importSources(taskCourseId, [item.path], [title]);
        } catch (error) {
          if (!isCurrent()) return;
          if (canRetryImport(error)) throw error;
          const refreshed = await loadSources(taskCourseId).catch(() => null);
          if (!isCurrent()) return;
          if (refreshed) {
            const beforeIds = new Set(beforeSources.map((source) => source.id));
            const candidates = refreshed.filter((source) => {
              if (beforeIds.has(source.id)) return false;
              return source.title === title || safeName(source.file_path) === item.name;
            });
            if (candidates.length === 1) {
              imported = {
                source_id: candidates[0].id,
                title: candidates[0].title,
                stored_path: candidates[0].file_path,
              };
            }
          }
          if (!imported) {
            setItem(item.key, { status: "结果待确认", phase: "import", error: uncertainImportError });
            return;
          }
        }
        if (!isCurrent()) return;
        if (!imported) throw new Error("服务返回数据格式异常，请稍后重试");
        sourceId = imported.source_id;
        setItem(item.key, {
          sourceId,
          storedPath: imported.stored_path,
          phase: "detect",
          status: "识别章节",
        });
        await loadSources(taskCourseId);
        if (!isCurrent()) return;
      } else {
        setItem(item.key, { phase: "detect", status: "识别章节", error: undefined });
      }
      await detectChapters(sourceId);
      if (!isCurrent()) return;
      setItem(item.key, { phase: "detect", status: "成功", error: undefined });
    } catch (error) {
      if (!isCurrent()) return;
      setItem(item.key, {
        phase: sourceId ? "detect" : "import",
        sourceId,
        status: "失败",
        error: error instanceof Error ? error.message : "教材处理失败",
      });
    }
  };

  const importAll = async () => {
    if (runningGeneration.current !== null) return;
    const runGeneration = generation.current;
    runningGeneration.current = runGeneration;
    try {
      for (const item of items) {
        if (item.path && (item.status === "等待" || item.status === "失败")) await processItem(item);
      }
    } finally {
      if (runningGeneration.current === runGeneration) runningGeneration.current = null;
    }
  };

  const retry = async (item: QueueItem) => {
    if (runningGeneration.current !== null || item.status !== "失败" || !item.path) return;
    const runGeneration = generation.current;
    runningGeneration.current = runGeneration;
    try {
      await processItem(item);
    } finally {
      if (runningGeneration.current === runGeneration) runningGeneration.current = null;
    }
  };

  const hasPending = items.some((item) => item.path && (item.status === "等待" || item.status === "失败"));

  return (
    <div className="space-y-4">
      <div data-testid="textbook-drop-zone" data-drag-active={dragActive} onDragEnter={() => setDragActive(true)} onDragLeave={() => setDragActive(false)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { setDragActive(false); dropped(event); }} className={`flex min-h-40 flex-col items-center justify-center border-2 border-dashed px-6 py-8 text-center transition-colors ${dragActive ? "border-emerald-500 bg-emerald-50" : "border-zinc-300 hover:border-zinc-400"}`}>
        <Upload size={28} className="mb-3 text-zinc-400" aria-hidden="true" />
        <p className="text-sm font-medium text-zinc-800">拖放 PDF 或 Word 教材</p>
        <p className="mt-1 text-xs text-zinc-500">支持 PDF、DOC、DOCX，可一次选择多本</p>
        <button type="button" onClick={chooseFiles} title="选择多本教材" className="mt-4 inline-flex h-9 items-center gap-2 rounded-md bg-zinc-900 px-4 text-sm font-medium text-white hover:bg-zinc-800"><FileText size={15} aria-hidden="true" />选择教材</button>
        <input ref={inputRef} type="file" multiple accept=".pdf,.doc,.docx" onChange={webFiles} className="sr-only" aria-label="选择教材文件" />
      </div>
      {errors.length > 0 && <div role="alert" className="space-y-1 text-sm text-red-600">{errors.map((error, index) => <p key={`${error}-${index}`}>{error}</p>)}</div>}
      {items.length > 0 && (
        <div className="border-y border-zinc-200">
          <div className="flex h-12 items-center justify-between border-b border-zinc-100 px-1">
            <span className="text-sm font-medium text-zinc-700">导入队列 · {items.length} 本</span>
            {hasPending && <button type="button" onClick={importAll} title="导入队列中的教材" className="inline-flex h-8 items-center gap-2 rounded-md bg-emerald-600 px-3 text-xs font-medium text-white hover:bg-emerald-700"><Upload size={14} aria-hidden="true" />导入全部</button>}
          </div>
          <ul className="divide-y divide-zinc-100">
            {items.map((item) => (
              <li key={item.key} className="grid min-h-14 grid-cols-[minmax(0,1fr)_96px_36px] items-center gap-3 px-1 py-2">
                <div className="min-w-0 py-1">
                  <p className="mb-1 truncate text-xs text-zinc-400" title={item.name}>{item.name}</p>
                  <label className="block">
                    <span className="mb-1 block text-xs text-zinc-500">教材名称</span>
                    <input
                      type="text"
                      aria-label="教材名称"
                      maxLength={120}
                      value={item.title}
                      disabled={!item.path || !(item.status === "等待" || (item.status === "失败" && item.phase === "import"))}
                      onChange={(event) => setItem(item.key, { title: event.target.value, error: undefined })}
                      className="h-8 w-full rounded-md border border-zinc-200 px-2 text-sm text-zinc-800 focus:outline-none focus:ring-2 focus:ring-zinc-200 disabled:bg-zinc-50 disabled:text-zinc-500"
                    />
                  </label>
                  {item.error && <p className="mt-1 text-xs text-red-600" title={item.error}>{item.error}</p>}
                </div>
                <span className="inline-flex h-7 items-center gap-1.5 text-xs text-zinc-500" aria-live="polite">{item.status === "成功" ? <CheckCircle2 size={14} className="text-emerald-600" /> : item.status === "失败" || item.status === "结果待确认" ? <AlertCircle size={14} className="text-red-600" /> : item.status !== "等待" ? <Loader2 size={14} className="animate-spin" /> : null}{item.status}</span>
                {item.status === "失败" && item.path ? <button type="button" onClick={() => retry(item)} aria-label={`重试 ${item.name}`} title={`重试 ${item.name}`} className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100"><RefreshCw size={15} aria-hidden="true" /></button> : <span className="h-8 w-8" />}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
