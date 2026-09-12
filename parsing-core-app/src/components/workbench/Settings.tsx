import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { FolderOpen, Loader2, Save, Wifi } from "lucide-react";
import { apiErrorInfo } from "../../api/errorMessages";
import type { ApiErrorAction } from "../../api/errorMessages";
import { isTauriRuntime } from "../../api/runtime";
import {
  fetchEnvironment,
  getWorkbenchSettings,
  saveBaiduKey,
  saveCodexPath,
  saveDeepSeekSettings,
  testDeepSeekSettings,
} from "../../api/workbench";
import type { EnvironmentReport } from "../../api/workbenchTypes";

const MODEL = "deepseek-v4-pro";

interface SectionError {
  message: string;
  action: ApiErrorAction;
}

interface RefreshResult {
  stale: boolean;
  settingsOk: boolean;
  environmentOk: boolean;
}

function describeError(error: unknown, fallback: string): SectionError {
  const info = apiErrorInfo(error);
  if (info) return { message: `${info.title}：${info.description}`, action: info.action };
  return { message: error instanceof Error ? error.message : fallback, action: "none" };
}

export default function Settings() {
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState(MODEL);
  const [maskedKey, setMaskedKey] = useState<string | null>(null);
  const [codexPath, setCodexPath] = useState("");
  const [baiduKey, setBaiduKey] = useState("");
  const [environment, setEnvironment] = useState<EnvironmentReport | null>(null);
  const [environmentError, setEnvironmentError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [savingCodex, setSavingCodex] = useState(false);
  const [savingBaidu, setSavingBaidu] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [codexError, setCodexError] = useState<SectionError | null>(null);
  const [codexMessage, setCodexMessage] = useState<string | null>(null);
  const [baiduError, setBaiduError] = useState<SectionError | null>(null);
  const [baiduMessage, setBaiduMessage] = useState<string | null>(null);
  const desktop = isTauriRuntime();
  const initializedRef = useRef(false);
  const refreshSequenceRef = useRef(0);
  const baiduInputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async (): Promise<RefreshResult> => {
    const sequence = ++refreshSequenceRef.current;
    const [settingsResult, environmentResult] = await Promise.allSettled([getWorkbenchSettings(), fetchEnvironment()]);
    if (sequence !== refreshSequenceRef.current) {
      return { stale: true, settingsOk: false, environmentOk: false };
    }
    if (settingsResult.status === "fulfilled") {
      setModel(MODEL);
      setMaskedKey(settingsResult.value.deepseek_key_masked);
      if (!initializedRef.current) {
        const detectedPath = environmentResult.status === "fulfilled" ? environmentResult.value.codex.path : null;
        const initialPath = settingsResult.value.codex_cli_path ?? detectedPath ?? "";
        setCodexPath((current) => (current.trim() ? current : initialPath));
        initializedRef.current = true;
      }
    }
    if (environmentResult.status === "fulfilled") {
      setEnvironment(environmentResult.value);
      setEnvironmentError(false);
    } else {
      setEnvironmentError(true);
    }
    return {
      stale: false,
      settingsOk: settingsResult.status === "fulfilled",
      environmentOk: environmentResult.status === "fulfilled",
    };
  }, []);

  useEffect(() => {
    refresh()
      .then((result) => {
        if (result.stale) return;
        if (!result.settingsOk) setError("设置加载失败");
      })
      .finally(() => setLoading(false));
  }, [refresh]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedModel = model.trim();
    const trimmedKey = apiKey.trim();
    if (!trimmedModel || (!maskedKey && !trimmedKey)) return;
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const settings = await saveDeepSeekSettings(trimmedKey || null, trimmedModel);
      setMaskedKey(settings.deepseek_key_masked);
      setModel(settings.deepseek_model);
      setApiKey("");
      setMessage("已保存");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    setMessage(null);
    try {
      await testDeepSeekSettings();
      setMessage("连接正常");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "测试失败");
    } finally {
      setTesting(false);
    }
  };

  const handleSaveCodex = async () => {
    const path = codexPath.trim();
    if (!path) return;
    setSavingCodex(true);
    setCodexError(null);
    setCodexMessage(null);
    try {
      await saveCodexPath(path);
    } catch (err: unknown) {
      setCodexError(describeError(err, "保存失败"));
      setSavingCodex(false);
      return;
    }
    setSavingCodex(false);
    setCodexMessage("已保存");
    const result = await refresh();
    if (!result.stale && (!result.settingsOk || !result.environmentOk)) {
      setCodexMessage("已保存，但状态刷新失败，可重新检测");
    }
  };

  const handlePickCodex = async () => {
    if (!desktop) return;
    setCodexError(null);
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({ multiple: false });
      if (typeof selected === "string") setCodexPath(selected);
    } catch (err: unknown) {
      setCodexError(describeError(err, "无法打开文件选择器"));
    }
  };

  const handleSaveBaidu = async () => {
    const key = baiduKey.trim();
    if (!key) return;
    setSavingBaidu(true);
    setBaiduError(null);
    setBaiduMessage(null);
    try {
      await saveBaiduKey(key);
    } catch (err: unknown) {
      setBaiduError(describeError(err, "保存失败"));
      setSavingBaidu(false);
      return;
    }
    setSavingBaidu(false);
    setBaiduKey("");
    setBaiduMessage("已保存");
    const result = await refresh();
    if (!result.stale && (!result.settingsOk || !result.environmentOk)) {
      setBaiduMessage("已保存，但状态刷新失败，可重新检测");
    }
  };

  return (
    <div className="max-w-2xl space-y-6 animate-in">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">精读设置</h1>
        <p className="mt-0.5 text-sm text-zinc-500">API Key 保存在 macOS Keychain。</p>
      </div>

      {!loading && environmentError && (
        <p role="alert" className="text-sm text-amber-700">
          环境状态加载失败，可稍后重试。
        </p>
      )}

      <form onSubmit={submit} className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
        <div className="grid gap-4">
          <label className="block">
            <span className="text-xs text-zinc-500">DeepSeek API Key</span>
            <input
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={maskedKey ?? "sk-..."}
              className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
            />
          </label>

          <label className="block">
            <span className="text-xs text-zinc-500">Model</span>
            <input
              value={model}
              readOnly
              aria-readonly="true"
              className="mt-1 w-full cursor-not-allowed border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700"
            />
          </label>
        </div>

        {loading && (
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Loader2 size={16} className="animate-spin" />
            加载中
          </div>
        )}
        {!loading && error && <p className="text-sm text-red-500">{error}</p>}
        {!loading && message && <p className="text-sm text-emerald-600">{message}</p>}

        <div className="flex flex-wrap gap-2">
          <button
            type="submit"
            disabled={loading || saving || testing || !model.trim() || (!maskedKey && !apiKey.trim())}
            className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
          >
            {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
            保存
          </button>
          <button
            type="button"
            onClick={test}
            disabled={loading || saving || testing}
            className="inline-flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:border-zinc-300 disabled:opacity-50"
          >
            {testing ? <Loader2 size={15} className="animate-spin" /> : <Wifi size={15} />}
            测试连接
          </button>
        </div>
      </form>

      <section aria-label="Codex CLI 配置" className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
        <div>
          <h2 className="text-sm font-semibold text-zinc-900">Codex CLI</h2>
          <p className="mt-1 text-sm text-zinc-500">
            用于教材页面的视觉复核。当前状态：
            {environment?.codex.state === "ready" ? "已就绪" : "未配置"}
          </p>
        </div>
        <label className="block">
          <span className="text-xs text-zinc-500">Codex CLI 路径</span>
          <input
            value={codexPath}
            onChange={(event) => setCodexPath(event.target.value)}
            placeholder={environment?.codex.path ?? "/opt/homebrew/bin/codex"}
            className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleSaveCodex}
            disabled={savingCodex || !codexPath.trim()}
            className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
          >
            {savingCodex ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
            保存 Codex 路径
          </button>
          <button
            type="button"
            onClick={handlePickCodex}
            disabled={!desktop}
            className="inline-flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:border-zinc-300 disabled:cursor-not-allowed disabled:bg-zinc-100 disabled:text-zinc-400"
          >
            <FolderOpen size={15} />
            选择文件
          </button>
        </div>
        {codexError && (
          <div role="alert" className="text-sm text-red-500">
            <p>{codexError.message}</p>
            {codexError.action === "pick_codex" && (
              <button
                type="button"
                onClick={handlePickCodex}
                disabled={!desktop}
                className="mt-1 underline disabled:cursor-not-allowed disabled:text-zinc-400"
              >
                选择 Codex 文件
              </button>
            )}
          </div>
        )}
        {codexMessage && <p className="text-sm text-emerald-600">{codexMessage}</p>}
        {!desktop && (
          <p role="alert" className="text-sm text-amber-700">
            仅桌面应用可浏览文件，可手动输入路径。
          </p>
        )}
      </section>

      <section aria-label="百度 OCR 配置" className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
        <div>
          <h2 className="text-sm font-semibold text-zinc-900">百度 OCR（可选）</h2>
          <p className="mt-1 text-sm text-zinc-500">
            {environment?.baidu.state === "ready"
              ? `已配置：${environment.baidu.masked}`
              : "未配置时，冲突页会隔离为待复核，不影响其余页面产出。"}
          </p>
        </div>
        <label className="block">
          <span className="text-xs text-zinc-500">百度 OCR Key</span>
          <input
            ref={baiduInputRef}
            type="password"
            value={baiduKey}
            onChange={(event) => setBaiduKey(event.target.value)}
            placeholder={environment?.baidu.masked ?? "输入百度 OCR API Key"}
            className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleSaveBaidu}
            disabled={savingBaidu || !baiduKey.trim()}
            className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
          >
            {savingBaidu ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
            保存百度 Key
          </button>
        </div>
        {baiduError && (
          <div role="alert" className="text-sm text-red-500">
            <p>{baiduError.message}</p>
            {baiduError.action === "open_baidu_settings" && (
              <button type="button" onClick={() => baiduInputRef.current?.focus()} className="mt-1 underline">
                去配置百度 Key
              </button>
            )}
          </div>
        )}
        {baiduMessage && <p className="text-sm text-emerald-600">{baiduMessage}</p>}
      </section>
    </div>
  );
}
