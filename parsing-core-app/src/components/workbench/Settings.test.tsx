import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SafeApiError } from "../../api/workbench";
import * as workbenchApi from "../../api/workbench";
import type { EnvironmentReport, WorkbenchSettings } from "../../api/workbenchTypes";
import Settings from "./Settings";

const settings: WorkbenchSettings = {
  deepseek_model: "deepseek-v4-pro",
  deepseek_key_masked: "sk-****1234",
  codex_cli_path: null,
  baidu_key_masked: null,
};

const environment: EnvironmentReport = {
  app_version: "0.1.4",
  data_dir: { path: "/tmp", writable: true },
  deepseek: { state: "ready", last_test_ok: null, detail_code: null },
  codex: { state: "missing", path: null, source: null, detail_code: "codex_not_found" },
  baidu: { state: "optional", masked: null, detail_code: null },
  vision: { state: "ready", detail_code: null },
};

vi.mock("../../api/workbench", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/workbench")>();
  return {
    ...actual,
    getWorkbenchSettings: vi.fn(),
    fetchEnvironment: vi.fn(),
    saveCodexPath: vi.fn(),
    saveBaiduKey: vi.fn(),
    clearCodexPath: vi.fn(),
    clearBaiduKey: vi.fn(),
    saveDeepSeekSettings: vi.fn(),
    testDeepSeekSettings: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(workbenchApi.getWorkbenchSettings).mockResolvedValue(settings);
  vi.mocked(workbenchApi.fetchEnvironment).mockResolvedValue(environment);
  vi.mocked(workbenchApi.saveCodexPath).mockResolvedValue(settings);
  vi.mocked(workbenchApi.saveBaiduKey).mockResolvedValue(settings);
});

it("shows codex and baidu configuration sections", async () => {
  render(<Settings />);
  expect(await screen.findByText("Codex CLI")).toBeInTheDocument();
  expect(screen.getByText("百度 OCR（可选）")).toBeInTheDocument();
});

it("saves the baidu key", async () => {
  render(<Settings />);
  const input = await screen.findByLabelText("百度 OCR Key");
  await userEvent.type(input, "baidu-key-1234");
  await userEvent.click(screen.getByRole("button", { name: "保存百度 Key" }));
  await waitFor(() => expect(workbenchApi.saveBaiduKey).toHaveBeenCalledWith("baidu-key-1234"));
  expect(await screen.findByText("已保存")).toBeInTheDocument();
});

it("saves the codex path and keeps the edited input across the refresh", async () => {
  render(<Settings />);
  const input = await screen.findByLabelText("Codex CLI 路径");
  await userEvent.type(input, "/opt/homebrew/bin/codex");
  await userEvent.click(screen.getByRole("button", { name: "保存 Codex 路径" }));
  await waitFor(() => expect(workbenchApi.saveCodexPath).toHaveBeenCalledWith("/opt/homebrew/bin/codex"));
  expect(await screen.findByText("已保存")).toBeInTheDocument();
  await waitFor(() => {
    expect(workbenchApi.getWorkbenchSettings).toHaveBeenCalledTimes(2);
    expect(input).toHaveValue("/opt/homebrew/bin/codex");
  });
});

it("shows actionable copy when saving the baidu key fails", async () => {
  vi.mocked(workbenchApi.saveBaiduKey).mockRejectedValueOnce(new SafeApiError("invalid_request", "baidu_key_missing"));
  render(<Settings />);
  const input = await screen.findByLabelText("百度 OCR Key");
  await userEvent.type(input, "bad-key");
  await userEvent.click(screen.getByRole("button", { name: "保存百度 Key" }));
  expect(await screen.findByText(/百度 OCR Key 未配置/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "去配置百度 Key" })).toBeInTheDocument();
});

it("keeps deepseek masking when the environment request fails", async () => {
  vi.mocked(workbenchApi.fetchEnvironment).mockRejectedValueOnce(new Error("offline"));
  render(<Settings />);
  const keyInput = await screen.findByLabelText("DeepSeek API Key");
  await waitFor(() => expect(keyInput).toHaveAttribute("placeholder", "sk-****1234"));
  expect(screen.getByText("Codex CLI")).toBeInTheDocument();
  expect(screen.getByText("环境状态加载失败，可稍后重试。")).toBeInTheDocument();
});

it("keeps the saved feedback when the follow-up refresh fails", async () => {
  vi.mocked(workbenchApi.getWorkbenchSettings)
    .mockResolvedValueOnce(settings)
    .mockRejectedValueOnce(new Error("refresh failed"));
  render(<Settings />);
  const input = await screen.findByLabelText("Codex CLI 路径");
  await userEvent.type(input, "/opt/homebrew/bin/codex");
  await userEvent.click(screen.getByRole("button", { name: "保存 Codex 路径" }));
  expect(await screen.findByText("已保存，但状态刷新失败，可重新检测")).toBeInTheDocument();
});

it("ignores a stale initial settings response that resolves after a save refresh", async () => {
  let resolveInitialSettings!: (value: WorkbenchSettings) => void;
  vi.mocked(workbenchApi.getWorkbenchSettings)
    .mockImplementationOnce(
      () =>
        new Promise<WorkbenchSettings>((resolve) => {
          resolveInitialSettings = resolve;
        }),
    )
    .mockResolvedValue(settings);
  vi.mocked(workbenchApi.saveCodexPath).mockResolvedValue(settings);
  render(<Settings />);
  const input = await screen.findByLabelText("Codex CLI 路径");
  await userEvent.type(input, "/opt/homebrew/bin/codex");
  await userEvent.click(screen.getByRole("button", { name: "保存 Codex 路径" }));
  const keyInput = screen.getByLabelText("DeepSeek API Key");
  await waitFor(() => expect(keyInput).toHaveAttribute("placeholder", "sk-****1234"));
  resolveInitialSettings({ ...settings, deepseek_key_masked: "sk-****0000" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(keyInput).toHaveAttribute("placeholder", "sk-****1234");
});
