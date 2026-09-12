import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
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
});
