import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SafeApiError } from "../../api/workbench";
import type { OcrStatus, Source } from "../../api/workbenchTypes";
import OcrWorkflowPanel from "./OcrWorkflowPanel";

const mocks = vi.hoisted(() => ({
  getSourceOcrStatus: vi.fn(),
  reviewSourceOcr: vi.fn(),
  startSourceOcr: vi.fn(),
  cancelSourceOcr: vi.fn(),
  recognizeSourceChapters: vi.fn(),
  confirmSourceChapter: vi.fn(),
  generateSourceNote: vi.fn(),
}));

vi.mock("../../api/workbench", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/workbench")>();
  return {
    SafeApiError: actual.SafeApiError,
    getSourceOcrStatus: mocks.getSourceOcrStatus,
    reviewSourceOcr: mocks.reviewSourceOcr,
    startSourceOcr: mocks.startSourceOcr,
    cancelSourceOcr: mocks.cancelSourceOcr,
    recognizeSourceChapters: mocks.recognizeSourceChapters,
    confirmSourceChapter: mocks.confirmSourceChapter,
    generateSourceNote: mocks.generateSourceNote,
  };
});

const source: Source = {
  id: "source-1",
  course_id: "course-1",
  kind: "main",
  file_path: "/tmp/book.pdf",
  title: "战略教材",
  status: "ready",
};

const reviewStatus: OcrStatus = {
  status: "review_required",
  source_path: "/tmp/book.pdf",
  state_path: "/tmp/state/batch-state.json",
  error: null,
  publishable: true,
  markdown_path: null,
  chapter_tree_path: null,
  review_pages: [
    { page: 3, reason: "conflict", alignment_status: "conflict" },
    { page: 7, reason: "sampled", alignment_status: "consistent" },
  ],
  review_pending: 2,
};

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSourceOcrStatus.mockResolvedValue(reviewStatus);
  mocks.reviewSourceOcr.mockResolvedValue(reviewStatus);
});

it("lists review pages and requests a review rerun", async () => {
  render(
    <MemoryRouter>
      <OcrWorkflowPanel source={source} />
    </MemoryRouter>,
  );

  expect(await screen.findByText("待复核 2 页")).toBeInTheDocument();
  expect(screen.getByText("第 3 页 · 冲突")).toBeInTheDocument();
  expect(screen.getByText("第 7 页 · 抽样")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "配置百度 Key 并继续复核" }));

  expect(mocks.reviewSourceOcr).toHaveBeenCalledWith("source-1");
});

it("links to settings when a review rerun is not ready", async () => {
  mocks.reviewSourceOcr.mockRejectedValueOnce(new SafeApiError("conflict", "ocr_review_not_ready", {}, 409));
  render(
    <MemoryRouter>
      <OcrWorkflowPanel source={source} />
    </MemoryRouter>,
  );

  await screen.findByText("待复核 2 页");
  await userEvent.click(screen.getByRole("button", { name: "配置百度 Key 并继续复核" }));

  expect(await screen.findByRole("button", { name: "去配置百度 Key" })).toBeInTheDocument();
});
