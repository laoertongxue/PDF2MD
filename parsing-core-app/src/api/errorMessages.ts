import { SafeApiError } from "./workbench";

export type ApiErrorAction =
  "open_deepseek_settings" | "pick_codex" | "open_baidu_settings" | "retry" | "open_logs" | "none";

interface ApiErrorInfo {
  title: string;
  description: string;
  action: ApiErrorAction;
}

const ERROR_MESSAGES: Record<string, ApiErrorInfo> = {
  deepseek_key_missing: {
    title: "DeepSeek API Key 未配置",
    description: "请在精读设置中保存 DeepSeek API Key 后重试。",
    action: "open_deepseek_settings",
  },
  codex_unavailable: {
    title: "Codex CLI 不可用",
    description: "请在精读设置或环境自检中选择可执行的 Codex CLI 路径。",
    action: "pick_codex",
  },
  codex_invalid: {
    title: "Codex CLI 路径无效",
    description: "所选文件不是受支持的 Codex CLI，请选择官方 npm 安装的可执行文件。",
    action: "pick_codex",
  },
  baidu_key_missing: {
    title: "百度 OCR Key 未配置",
    description: "请在精读设置中保存百度 OCR Key 后继续复核。",
    action: "open_baidu_settings",
  },
  ocr_review_pending: {
    title: "存在待复核页面",
    description: "配置百度 OCR Key 后可继续复核隔离页面。",
    action: "open_baidu_settings",
  },
  ocr_review_not_ready: {
    title: "暂时无法继续复核",
    description: "请先配置百度 OCR Key 并确认任务存在待复核页面。",
    action: "open_baidu_settings",
  },
};

export function apiErrorInfo(error: unknown): ApiErrorInfo | null {
  if (!(error instanceof SafeApiError) || !error.code) return null;
  return ERROR_MESSAGES[error.code] ?? null;
}

export function ocrErrorInfo(code: string | null | undefined): ApiErrorInfo | null {
  if (!code) return null;
  return ERROR_MESSAGES[code] ?? null;
}
