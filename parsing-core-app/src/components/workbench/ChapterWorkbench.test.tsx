import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ChapterWorkbench from "./ChapterWorkbench";

vi.mock("../MermaidBlock", () => ({ default: ({ code }: { code: string }) => <div data-testid="mermaid">{code}</div> }));

const actions = {
  loadCourses: vi.fn(), loadSources: vi.fn(), loadChapters: vi.fn(), loadChapterNoteBlocks: vi.fn(),
  loadChapterRuns: vi.fn(), runHybridChapter: vi.fn(), saveChapterBlock: vi.fn(),
};
let state: Record<string, unknown>;
vi.mock("../../store/useWorkbenchStore", () => ({ useWorkbenchStore: () => state }));

const chapter = (id: string, title: string) => ({ id, source_id: "s1", course_id: "c1", seq: id === "ch1" ? 0 : 1, title, status: "COMPLETED" });
const blocks = [
  ["summary", "本章概要", "## 战略概要\n聚焦取舍 [《战略教材》·第 1 章]"],
  ["concepts", "核心概念", "- 成本领先\n- 差异化"],
  ["plain_explain", "通俗解释", "像选择一条明确赛道。"],
  ["application", "应用场景", "用于评估业务组合。"],
  ["reflection", "复盘反思", "需要识别能力边界。"],
  ["knowledge_mermaid", "知识结构图", "flowchart LR\nA-->B"],
  ["application_mermaid", "应用流程图", "flowchart LR\nX-->Y"],
].map(([kind, title, body], seq) => ({ id: `b${seq}`, chapter_id: "ch1", kind, title, body, seq, updated_at: seq + 1 }));
const runs = [
  { id: "r1", chapter_id: "ch1", round_key: "structure", executor: "deepseek", status: "COMPLETED", output: "ok", error: "", stale: false, created_at: 1, updated_at: 2 },
  { id: "r2", chapter_id: "ch1", round_key: "concepts", executor: "deepseek", status: "COMPLETED", output: "ok", error: "", stale: true, created_at: 2, updated_at: 3 },
  { id: "r3", chapter_id: "ch1", round_key: "review", executor: "codex", status: "FAILED", output: "", error: "审核未通过：引用不足", stale: false, created_at: 3, updated_at: 4 },
];

function reset() {
  state = {
    selectedCourseId: "c1", sources: { c1: [{ id: "s1", course_id: "c1", title: "战略教材", kind: "main", file_path: "/book.pdf", status: "READY" }] },
    chapters: { s1: [chapter("ch1", "竞争战略"), chapter("ch2", "增长战略")] },
    noteBlocksByChapter: { ch1: blocks, ch2: [] }, chapterRunsById: { ch1: runs, ch2: [] }, ...actions,
  };
}

describe("ChapterWorkbench", () => {
  beforeEach(() => { vi.clearAllMocks(); Object.values(actions).forEach((fn) => fn.mockResolvedValue(undefined)); actions.loadSources.mockResolvedValue([]); actions.loadChapterNoteBlocks.mockResolvedValue(blocks); actions.loadChapterRuns.mockResolvedValue(runs); reset(); });
  afterEach(cleanup);

  it("renders the complete note, sources, review and all round states", async () => {
    render(<MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}><ChapterWorkbench /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "本章概要" })).toBeInTheDocument();
    for (const heading of ["核心概念", "通俗解释", "应用场景", "复盘反思", "来源", "审核结果", "知识结构图", "应用流程图"]) expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "战略概要" })).toBeInTheDocument();
    expect(screen.getAllByTestId("mermaid")).toHaveLength(2);
    expect(screen.getByText("《战略教材》·第 1 章")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("审核未通过：引用不足");
    const history = screen.getByLabelText("精读轮次历史");
    expect(within(history).getByText("当前轮")).toBeInTheDocument();
    expect(within(history).getByText("结果已过期")).toBeInTheDocument();
    expect(within(history).getByText("失败")).toBeInTheDocument();
  });

  it("saves a real chapter block, reports failure and retries without losing the draft", async () => {
    actions.saveChapterBlock.mockRejectedValueOnce(new Error("保存失败")).mockResolvedValueOnce({ ...blocks[5], body: "flowchart LR\nN-->M" });
    render(<MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}><ChapterWorkbench /></MemoryRouter>);
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.clear(editor); await userEvent.type(editor, "flowchart LR\nN-->M");
    await userEvent.click(screen.getAllByRole("button", { name: "保存 Mermaid" })[0]);
    expect(await screen.findByText("保存失败")).toBeInTheDocument();
    expect(editor).toHaveValue("flowchart LR\nN-->M");
    await userEvent.click(screen.getByRole("button", { name: "重试保存" }));
    expect(actions.saveChapterBlock).toHaveBeenLastCalledWith("ch1", "knowledge_mermaid", "flowchart LR\nN-->M", "flowchart LR\nA-->B");
  });

  it("protects dirty Mermaid on chapter switching and browser refresh", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}><ChapterWorkbench /></MemoryRouter>);
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");
    fireEvent.change(screen.getByLabelText("选择章节"), { target: { value: "ch2" } });
    expect(confirm).toHaveBeenCalled();
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch1");
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    confirm.mockRestore();
  });
});
