import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchEnvironment } from "../../api/workbench";
import type { EnvironmentReport } from "../../api/workbenchTypes";

const DETAIL_LABELS: Record<string, string> = {
  deepseek_key_missing: "未配置 API Key",
  codex_not_found: "未找到可执行文件",
  codex_is_symlink: "符号链接不受支持，请选择真实文件",
  codex_not_regular_file: "不是普通文件",
  codex_not_executable: "没有执行权限",
  codex_layout_unsupported: "不是受支持的官方 Codex CLI",
};

type ItemState = "ready" | "missing" | "invalid" | "optional";

interface EnvironmentItem {
  key: string;
  label: string;
  state: ItemState;
  detailCode: string | null | undefined;
  actionLabel: string | null;
}

function StateBadge({ state }: { state: ItemState }) {
  const label = state === "ready" ? "已就绪" : state === "optional" ? "可选" : "待配置";
  const tone =
    state === "ready"
      ? "bg-emerald-50 text-emerald-700"
      : state === "optional"
        ? "bg-zinc-100 text-zinc-500"
        : "bg-amber-50 text-amber-700";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>{label}</span>;
}

export default function EnvironmentCard() {
  const navigate = useNavigate();
  const [report, setReport] = useState<EnvironmentReport | null>(null);
  const [error, setError] = useState(false);
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null);

  const refresh = useCallback(() => {
    setError(false);
    fetchEnvironment()
      .then((value) => {
        setReport(value);
        setError(false);
      })
      .catch(() => setError(true));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (error) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        环境检测失败
        <button type="button" className="ml-2 underline" onClick={refresh}>
          重新检测
        </button>
      </div>
    );
  }
  if (report === null) {
    return <div className="rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-400">正在检测环境…</div>;
  }

  const items: EnvironmentItem[] = [
    {
      key: "deepseek",
      label: "DeepSeek",
      state: report.deepseek.state,
      detailCode: report.deepseek.detail_code,
      actionLabel: "去配置",
    },
    {
      key: "codex",
      label: "Codex CLI",
      state: report.codex.state,
      detailCode: report.codex.detail_code,
      actionLabel: "配置 Codex",
    },
    {
      key: "baidu",
      label: "百度 OCR",
      state: report.baidu.state,
      detailCode: report.baidu.detail_code,
      actionLabel: "去配置",
    },
    {
      key: "vision",
      label: "Apple Vision",
      state: report.vision.state,
      detailCode: report.vision.detail_code,
      actionLabel: null,
    },
  ];

  const requiredStates = items.map((item) => item.state).filter((state) => state !== "optional");
  const readyCount = requiredStates.filter((state) => state === "ready").length;
  const allReady = readyCount === requiredStates.length;
  const expanded = expandedOverride ?? !allReady;

  return (
    <section aria-label="环境自检" className="rounded-lg border border-zinc-200 bg-white">
      <button
        type="button"
        className="flex w-full items-center justify-between px-3 py-2 text-left"
        onClick={() => setExpandedOverride(!expanded)}
        aria-expanded={expanded}
      >
        <span className="text-sm font-medium text-zinc-900">
          {allReady
            ? `环境就绪 · ${readyCount}/${requiredStates.length}`
            : `环境待配置 · ${readyCount}/${requiredStates.length}`}
        </span>
        <span className="text-xs text-zinc-400">{expanded ? "收起" : "展开"}</span>
      </button>
      {expanded && (
        <ul className="space-y-2 border-t border-zinc-100 px-3 py-2 text-sm">
          {items.map((item) => {
            const detailLabel = item.detailCode ? DETAIL_LABELS[item.detailCode] : null;
            return (
              <li key={item.key} className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-zinc-700">{item.label}</span>
                    <StateBadge state={item.state} />
                  </div>
                  {detailLabel && <p className="mt-0.5 text-xs text-zinc-400">{detailLabel}</p>}
                </div>
                {item.actionLabel && (
                  <button
                    type="button"
                    className="shrink-0 text-xs text-emerald-700 underline"
                    onClick={() => navigate("/workbench/settings")}
                  >
                    {item.actionLabel}
                  </button>
                )}
              </li>
            );
          })}
          <li className="flex items-center justify-between gap-3 text-xs text-zinc-400">
            <span className="truncate" title={report.data_dir.path}>
              数据目录：{report.data_dir.path}
            </span>
            <button type="button" className="shrink-0 underline" onClick={refresh}>
              重新检测
            </button>
          </li>
        </ul>
      )}
    </section>
  );
}
