import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import * as workbenchApi from "../../api/workbench";
import type { EnvironmentReport } from "../../api/workbenchTypes";
import EnvironmentCard from "./EnvironmentCard";

vi.mock("../../api/workbench", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/workbench")>();
  return {
    ...actual,
    fetchEnvironment: vi.fn(),
  };
});

const missingCodexReport: EnvironmentReport = {
  app_version: "0.1.4",
  data_dir: { path: "/tmp/data", writable: true },
  deepseek: { state: "ready", last_test_ok: true, detail_code: null },
  codex: { state: "missing", path: null, source: null, detail_code: "codex_not_found" },
  baidu: { state: "optional", masked: null, detail_code: null },
  vision: { state: "ready", detail_code: null },
};

const readyReport: EnvironmentReport = {
  ...missingCodexReport,
  codex: { state: "ready", path: "/opt/homebrew/bin/codex", source: "detected", detail_code: null },
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="current-location">{location.pathname}</output>;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("lists each dependency with its state", async () => {
  vi.mocked(workbenchApi.fetchEnvironment).mockResolvedValueOnce(missingCodexReport);
  render(
    <MemoryRouter>
      <EnvironmentCard />
    </MemoryRouter>,
  );
  expect(await screen.findByText("环境待配置 · 2/4")).toBeInTheDocument();
  expect(screen.getByText("DeepSeek")).toBeInTheDocument();
  expect(screen.getByText("Codex CLI")).toBeInTheDocument();
  expect(screen.getByText("百度 OCR")).toBeInTheDocument();
  expect(screen.getByText("Apple Vision")).toBeInTheDocument();
  expect(screen.getByText("可选")).toBeInTheDocument();
  expect(screen.getByText("未找到可执行文件")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "配置 Codex" })).toBeInTheDocument();
});

it("collapses when every dependency is ready or optional", async () => {
  vi.mocked(workbenchApi.fetchEnvironment).mockResolvedValueOnce(readyReport);
  render(
    <MemoryRouter>
      <EnvironmentCard />
    </MemoryRouter>,
  );
  expect(await screen.findByText("环境就绪 · 3/4")).toBeInTheDocument();
  expect(screen.queryByText("DeepSeek")).not.toBeInTheDocument();
});

it("recovers after a failed check when retried", async () => {
  vi.mocked(workbenchApi.fetchEnvironment).mockRejectedValueOnce(new Error("offline"));
  render(
    <MemoryRouter>
      <EnvironmentCard />
    </MemoryRouter>,
  );
  expect(await screen.findByText("环境检测失败")).toBeInTheDocument();
  vi.mocked(workbenchApi.fetchEnvironment).mockResolvedValueOnce(missingCodexReport);
  await userEvent.click(screen.getByRole("button", { name: "重新检测" }));
  expect(await screen.findByText("Codex CLI")).toBeInTheDocument();
  expect(screen.getByText("环境待配置 · 2/4")).toBeInTheDocument();
});

it("shows an invalid badge and navigates to settings from the Codex action", async () => {
  vi.mocked(workbenchApi.fetchEnvironment).mockResolvedValueOnce({
    ...missingCodexReport,
    codex: { state: "invalid", path: "/tmp/codex", source: "settings", detail_code: "codex_not_executable" },
  });
  render(
    <MemoryRouter initialEntries={["/workbench"]}>
      <EnvironmentCard />
      <LocationProbe />
    </MemoryRouter>,
  );
  expect(await screen.findByText("待配置")).toBeInTheDocument();
  expect(screen.getByText("没有执行权限")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "配置 Codex" }));
  expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/settings");
});
