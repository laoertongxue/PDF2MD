import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../api/workbench";
import ChapterConfirm from "./ChapterConfirm";

const loadCourses = vi.fn().mockResolvedValue(undefined);
const loadSources = vi.fn().mockResolvedValue([{ id: "s1", course_id: "c1", kind: "main", file_path: "/a.pdf", title: "战略教材", status: "READY" }]);
vi.mock("../../store/useWorkbenchStore", () => ({ useWorkbenchStore: () => ({ courses: [{ id: "c1", title: "MBA", description: "", root_dir: "/mba" }], loadCourses, loadSources, selectedCourseId: "c1", sources: { c1: [{ id: "s1", course_id: "c1", kind: "main", file_path: "/a.pdf", title: "战略教材", status: "READY" }] } }) }));

const drafts = () => ({
  fingerprint: "fp-1",
  chapters: [
    { id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "第一章", status: "DRAFT", start: 0, end: 100 },
    { id: "ch2", source_id: "s1", course_id: "c1", seq: 1, title: "第二章", status: "DRAFT", start: 100, end: 220 },
  ],
});

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getChapterDrafts").mockResolvedValue(drafts());
  vi.spyOn(api, "replaceChapterDrafts").mockImplementation(async (_sourceId, _fingerprint, chapters) => ({ fingerprint: "fp-2", chapters: chapters.map((chapter, seq) => ({ ...chapter, id: chapter.id ?? `new-${seq}`, source_id: "s1", course_id: "c1", seq, status: "DRAFT" })) }));
  vi.spyOn(api, "confirmChapterDrafts").mockImplementation(async () => ({ ...drafts(), fingerprint: "fp-3", chapters: drafts().chapters.map((chapter) => ({ ...chapter, status: "CONFIRMED" })) }));
});
afterEach(cleanup);

it("edits, reorders, splits, merges and saves one source snapshot", async () => {
  render(<MemoryRouter><ChapterConfirm /></MemoryRouter>);
  const source = await screen.findByRole("region", { name: "战略教材" });
  const names = within(source).getAllByRole("textbox", { name: "章节名称" });
  await userEvent.clear(names[0]); await userEvent.type(names[0], "战略导论");
  await userEvent.click(within(source).getByRole("button", { name: "下移 战略导论" }));
  await userEvent.click(within(source).getAllByRole("button", { name: /拆分/ })[0]);
  expect(within(source).getAllByRole("textbox", { name: "章节名称" })).toHaveLength(3);
  await userEvent.click(within(source).getAllByRole("button", { name: /合并下一章/ })[0]);
  const starts = within(source).getAllByRole("spinbutton", { name: "起始边界" });
  await userEvent.clear(starts[1]); await userEvent.type(starts[1], "90");
  await userEvent.click(within(source).getByRole("button", { name: "保存章节草稿" }));
  await waitFor(() => expect(api.replaceChapterDrafts).toHaveBeenCalledWith("s1", "fp-1", expect.arrayContaining([expect.objectContaining({ start: 90 })])));
});

it("confirms the saved fingerprint and locks all editing controls", async () => {
  render(<MemoryRouter><ChapterConfirm /></MemoryRouter>);
  const source = await screen.findByRole("region", { name: "战略教材" });
  await userEvent.click(within(source).getByRole("button", { name: "确认章节目录" }));
  await waitFor(() => expect(api.confirmChapterDrafts).toHaveBeenCalledWith("s1", "fp-1"));
  expect(within(source).getAllByRole("textbox", { name: "章节名称" })[0]).toBeDisabled();
  expect(within(source).getByText("章节目录已确认并锁定")).toBeInTheDocument();
});
