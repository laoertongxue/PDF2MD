import { FormEvent, useEffect, useState } from "react";
import { Loader2, Save, Wifi } from "lucide-react";
import { getWorkbenchSettings, saveDeepSeekSettings, testDeepSeekSettings } from "../../api/workbench";

export default function Settings() {
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [maskedKey, setMaskedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    getWorkbenchSettings()
      .then((settings) => {
        setModel(settings.deepseek_model);
        setMaskedKey(settings.deepseek_key_masked);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "设置加载失败"))
      .finally(() => setLoading(false));
  }, []);

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

  return (
    <div className="max-w-2xl space-y-6 animate-in">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">精读设置</h1>
        <p className="mt-0.5 text-sm text-zinc-500">DeepSeek API Key 保存在 macOS Keychain。</p>
      </div>

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
              onChange={(event) => setModel(event.target.value)}
              placeholder="deepseek-chat"
              className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
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
    </div>
  );
}
