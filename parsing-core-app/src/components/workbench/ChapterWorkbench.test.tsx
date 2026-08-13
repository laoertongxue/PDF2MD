import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLayoutEffect } from "react";
import type { ChangeEvent } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { requireAt } from "../../test/requireValue";
import ChapterWorkbench from "./ChapterWorkbench";

vi.mock("../MermaidBlock", () => ({
  default: ({ code }: { code: string }) => <div data-testid="mermaid">{code}</div>,
}));

const mermaidEditorMode = vi.hoisted(() => ({ holdLocalDraft: false }));
vi.mock("./MermaidEditor", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./MermaidEditor")>();
  const React = await import("react");
  interface Props {
    title: string;
    initial: string;
    onSave: (code: string, expected: string) => Promise<boolean>;
    onDirtyChange?: (dirty: boolean) => void;
  }
  function LocalDraftEditor({ title, initial, onSave, onDirtyChange }: Props) {
    const [code, setCode] = React.useState(initial);
    const dirty = code !== initial;
    const onDirtyChangeRef = React.useRef(onDirtyChange);
    onDirtyChangeRef.current = onDirtyChange;
    React.useEffect(() => onDirtyChangeRef.current?.(dirty), [dirty]);
    return React.createElement(
      "div",
      null,
      React.createElement("textarea", {
        "aria-label": `${title} Mermaid 源码`,
        value: code,
        onChange: (event: ChangeEvent<HTMLTextAreaElement>) => setCode(event.target.value),
      }),
      React.createElement(
        "button",
        { type: "button", disabled: !dirty || !code.trim(), onClick: () => void onSave(code, initial) },
        "保存 Mermaid",
      ),
    );
  }
  return {
    default: (props: Props) =>
      React.createElement(mermaidEditorMode.holdLocalDraft ? LocalDraftEditor : actual.default, props),
  };
});

const actions = {
  selectCourse: vi.fn(),
  loadCourses: vi.fn(),
  loadSources: vi.fn(),
  loadChapters: vi.fn(),
  loadChapterNoteBlocks: vi.fn(),
  loadChapterRuns: vi.fn(),
  runHybridChapter: vi.fn(),
  saveChapterBlock: vi.fn(),
};
let state: Record<string, unknown>;
vi.mock("../../store/useWorkbenchStore", () => ({ useWorkbenchStore: () => state }));

interface NavigationCommit {
  chapterIds: string[];
  editorValue: string;
}

function ChapterNavigationHarness({ onCommit }: { onCommit?: (commit: NavigationCommit) => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  useLayoutEffect(() => {
    const select = document.querySelector<HTMLSelectElement>('select[aria-label="选择章节"]');
    const editor = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="知识结构图 Mermaid 源码"]');
    if (select && editor) {
      onCommit?.({
        chapterIds: Array.from(select.options, (option) => option.value).filter(Boolean),
        editorValue: editor.value,
      });
    }
  });
  return (
    <>
      <button type="button" onClick={() => navigate("/workbench/chapter?chapterId=ch1")}>
        导航到第一章
      </button>
      <button type="button" onClick={() => navigate("/workbench/chapter?chapterId=ch2")}>
        导航到第二章
      </button>
      <button type="button" onClick={() => navigate("/workbench/chapter?chapterId=missing")}>
        导航到无效章节
      </button>
      <output data-testid="current-location">{`${location.pathname}${location.search}`}</output>
      <ChapterWorkbench />
    </>
  );
}

const chapter = (id: string, title: string) => ({
  id,
  source_id: "s1",
  course_id: "c1",
  seq: id === "ch1" ? 0 : 1,
  title,
  status: "COMPLETED",
});
const blocks = [
  ["summary", "本章概要", "## 战略概要\n聚焦取舍 [《战略教材》·第 1 章]"],
  ["concepts", "核心概念", "- 成本领先\n- 差异化"],
  ["plain_explain", "通俗解释", "像选择一条明确赛道。"],
  ["application", "应用场景", "用于评估业务组合。"],
  ["reflection", "复盘反思", "需要识别能力边界。"],
  ["knowledge_mermaid", "知识结构图", "flowchart LR\nA-->B"],
  ["application_mermaid", "应用流程图", "flowchart LR\nX-->Y"],
].map(([kind, title, body], seq) => ({
  id: `b${seq}`,
  chapter_id: "ch1",
  kind,
  title,
  body,
  seq,
  updated_at: seq + 1,
}));
const secondChapterBlocks = blocks.map((block) => ({
  ...block,
  id: `${block.id}-ch2`,
  chapter_id: "ch2",
  body: block.kind === "knowledge_mermaid" ? "flowchart LR\nC-->D" : block.body,
}));
const thirdChapterBlocks = blocks.map((block) => ({
  ...block,
  id: `${block.id}-ch3`,
  chapter_id: "ch3",
  body: block.kind === "knowledge_mermaid" ? "flowchart LR\nE-->F" : block.body,
}));
const fourthChapterBlocks = blocks.map((block) => ({
  ...block,
  id: `${block.id}-ch4`,
  chapter_id: "ch4",
  body: block.kind === "knowledge_mermaid" ? "flowchart LR\nG-->H" : block.body,
}));

function addSecondCourse() {
  state = {
    ...state,
    sources: {
      ...(state.sources as Record<string, unknown[]>),
      c2: [{ id: "s2", course_id: "c2", title: "组织教材", kind: "main", file_path: "/org.pdf", status: "READY" }],
    },
    chapters: {
      ...(state.chapters as Record<string, unknown[]>),
      s2: [{ ...chapter("ch3", "组织能力"), source_id: "s2", course_id: "c2", seq: 0 }],
    },
    noteBlocksByChapter: { ch1: blocks, ch2: secondChapterBlocks, ch3: thirdChapterBlocks },
    chapterRunsById: { ch1: runs, ch2: [], ch3: [] },
  };
}

function addEmptySecondCourse() {
  state = {
    ...state,
    sources: { ...(state.sources as Record<string, unknown[]>), c2: [] },
  };
}

function addRunnableSecondChapter() {
  state = {
    ...state,
    chapters: {
      s1: [chapter("ch1", "竞争战略"), { ...chapter("ch2", "增长战略"), status: "FAILED" }],
    },
    noteBlocksByChapter: { ch1: blocks, ch2: secondChapterBlocks },
    chapterRunsById: { ch1: runs, ch2: [] },
  };
}

function addThirdCourse() {
  state = {
    ...state,
    sources: {
      ...(state.sources as Record<string, unknown[]>),
      c3: [
        { id: "s3", course_id: "c3", title: "创新教材", kind: "main", file_path: "/innovation.pdf", status: "READY" },
      ],
    },
    chapters: {
      ...(state.chapters as Record<string, unknown[]>),
      s3: [{ ...chapter("ch4", "创新路径"), source_id: "s3", course_id: "c3", seq: 0 }],
    },
    noteBlocksByChapter: {
      ...(state.noteBlocksByChapter as Record<string, unknown[]>),
      ch4: fourthChapterBlocks,
    },
    chapterRunsById: { ...(state.chapterRunsById as Record<string, unknown[]>), ch4: [] },
  };
}
const runs = [
  {
    id: "r1",
    chapter_id: "ch1",
    round_key: "structure",
    executor: "deepseek",
    status: "COMPLETED",
    output: "ok",
    error: "",
    stale: false,
    created_at: 1,
    updated_at: 2,
  },
  {
    id: "r2",
    chapter_id: "ch1",
    round_key: "concepts",
    executor: "deepseek",
    status: "COMPLETED",
    output: "ok",
    error: "",
    stale: true,
    created_at: 2,
    updated_at: 3,
  },
  {
    id: "r3",
    chapter_id: "ch1",
    round_key: "review",
    executor: "codex",
    status: "FAILED",
    output: "",
    error: "审核未通过：引用不足",
    stale: false,
    created_at: 3,
    updated_at: 4,
  },
];

function reset() {
  state = {
    selectedCourseId: "c1",
    sources: {
      c1: [{ id: "s1", course_id: "c1", title: "战略教材", kind: "main", file_path: "/book.pdf", status: "READY" }],
    },
    chapters: { s1: [chapter("ch1", "竞争战略"), chapter("ch2", "增长战略")] },
    noteBlocksByChapter: { ch1: blocks, ch2: [] },
    chapterRunsById: { ch1: runs, ch2: [] },
    ...actions,
  };
}

describe("ChapterWorkbench", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mermaidEditorMode.holdLocalDraft = false;
    Object.values(actions).forEach((fn) => fn.mockResolvedValue(undefined));
    actions.loadSources.mockResolvedValue([]);
    actions.loadChapterNoteBlocks.mockResolvedValue(blocks);
    actions.loadChapterRuns.mockResolvedValue(runs);
    reset();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders the complete note, sources, review and all round states", async () => {
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "本章概要" })).toBeInTheDocument();
    for (const heading of [
      "核心概念",
      "通俗解释",
      "应用场景",
      "复盘反思",
      "来源",
      "审核结果",
      "知识结构图",
      "应用流程图",
    ])
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "战略概要" })).toBeInTheDocument();
    expect(screen.getAllByTestId("mermaid")).toHaveLength(2);
    expect(screen.getByText("《战略教材》·第 1 章")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("审核未通过：引用不足");
    const history = screen.getByLabelText("精读轮次历史");
    expect(within(history).getByText("当前轮")).toBeInTheDocument();
    expect(within(history).getByText("结果已过期")).toBeInTheDocument();
    expect(within(history).getByText("失败")).toBeInTheDocument();
    await userEvent.click(within(history).getByRole("button", { name: "从审核轮重跑" }));
    expect(actions.runHybridChapter).toHaveBeenCalledWith("ch1");
  });

  it("ignores an old hybrid failure after navigating to another chapter", async () => {
    addRunnableSecondChapter();
    let rejectFirstRun: ((reason: Error) => void) | undefined;
    const firstRun = new Promise<unknown>((_resolve, reject) => {
      rejectFirstRun = reject;
    });
    actions.runHybridChapter.mockImplementation((chapterId: string) =>
      chapterId === "ch1" ? firstRun : Promise.resolve(),
    );
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });

    await userEvent.click(screen.getByRole("button", { name: "从审核轮重跑" }));
    expect(actions.runHybridChapter).toHaveBeenCalledWith("ch1");

    fireEvent.change(screen.getByLabelText("选择章节"), { target: { value: "ch2" } });
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch2"));

    await act(async () => {
      rejectFirstRun?.(new Error("A 混合精读失败"));
      await firstRun.catch(() => undefined);
    });

    await waitFor(() => expect(screen.getByRole("button", { name: "混合精读" })).toBeEnabled());
    expect(screen.queryByText("A 混合精读失败")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nC-->D");
  });

  it("keeps a new hybrid run active when an old run completes", async () => {
    addRunnableSecondChapter();
    let resolveFirstRun: (() => void) | undefined;
    let resolveSecondRun: (() => void) | undefined;
    const firstRun = new Promise<void>((resolve) => {
      resolveFirstRun = resolve;
    });
    const secondRun = new Promise<void>((resolve) => {
      resolveSecondRun = resolve;
    });
    actions.runHybridChapter.mockImplementation((chapterId: string) => (chapterId === "ch1" ? firstRun : secondRun));
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });

    await userEvent.click(screen.getByRole("button", { name: "从审核轮重跑" }));
    expect(screen.getByRole("button", { name: "从审核轮重跑" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("选择章节"), { target: { value: "ch2" } });
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch2"));
    const secondRunButton = screen.getByRole("button", { name: "混合精读" });
    expect(secondRunButton).toBeEnabled();

    await userEvent.click(secondRunButton);
    expect(actions.runHybridChapter).toHaveBeenCalledWith("ch2");
    expect(secondRunButton).toBeDisabled();

    await act(async () => {
      resolveFirstRun?.();
      await firstRun;
    });
    expect(secondRunButton).toBeDisabled();

    await act(async () => {
      resolveSecondRun?.();
      await secondRun;
    });
    await waitFor(() => expect(secondRunButton).toBeEnabled());
  });

  it("saves a real chapter block, reports failure and retries without losing the draft", async () => {
    actions.saveChapterBlock
      .mockRejectedValueOnce(new Error("保存失败"))
      .mockResolvedValueOnce({ ...blocks[5], body: "flowchart LR\nN-->M" });
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.clear(editor);
    await userEvent.type(editor, "flowchart LR\nN-->M");
    await userEvent.click(requireAt(screen.getAllByRole("button", { name: "保存 Mermaid" }), 0, "save button"));
    expect(await screen.findByText("保存失败")).toBeInTheDocument();
    expect(editor).toHaveValue("flowchart LR\nN-->M");
    await userEvent.click(screen.getByRole("button", { name: "重试保存" }));
    expect(actions.saveChapterBlock).toHaveBeenLastCalledWith(
      "ch1",
      "knowledge_mermaid",
      "flowchart LR\nN-->M",
      "flowchart LR\nA-->B",
    );
  });

  it("protects dirty Mermaid on chapter switching and browser refresh", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
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

  it("follows clean URL chapter navigation without remounting", async () => {
    state = {
      ...state,
      noteBlocksByChapter: { ch1: blocks, ch2: secondChapterBlocks },
    };
    const confirm = vi.spyOn(window, "confirm");
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nA-->B");

    await userEvent.click(screen.getByRole("button", { name: "导航到第二章" }));

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch2"));
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nC-->D");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch2");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("normalizes an invalid initial chapter URL to the selected fallback", async () => {
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=missing"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch1"));
    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1"),
    );
  });

  it("normalizes an invalid runtime chapter URL without leaving the active chapter", async () => {
    const confirm = vi.spyOn(window, "confirm");
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });

    await userEvent.click(screen.getByRole("button", { name: "导航到无效章节" }));

    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1"),
    );
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch1");
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toBe(editor);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("removes a stale chapter URL for an initially empty course", async () => {
    addEmptySecondCourse();
    state = { ...state, selectedCourseId: "c2" };
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    expect(await screen.findByText("还没有精读结果")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("current-location")).not.toHaveTextContent("chapterId"));
    expect(screen.queryByLabelText("选择章节")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "知识结构图 Mermaid 源码" })).not.toBeInTheDocument();
  });

  it("restores the URL when dirty chapter navigation is cancelled", async () => {
    state = {
      ...state,
      noteBlocksByChapter: { ch1: blocks, ch2: secondChapterBlocks },
    };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    await userEvent.click(screen.getByRole("button", { name: "导航到第二章" }));

    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch1");
    expect(editor).toHaveValue("flowchart LR\nA-->B\nB-->C");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1");
  });

  it("follows confirmed dirty URL chapter navigation", async () => {
    state = {
      ...state,
      noteBlocksByChapter: { ch1: blocks, ch2: secondChapterBlocks },
    };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    await userEvent.click(screen.getByRole("button", { name: "导航到第二章" }));

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch2"));
    expect(confirm).toHaveBeenCalledOnce();
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nC-->D");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch2");
  });

  it("selects a valid chapter when the course changes without remounting", async () => {
    addSecondCourse();
    const confirm = vi.spyOn(window, "confirm");
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nA-->B");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch3"));
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nE-->F");
    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch3"),
    );
    expect(confirm).not.toHaveBeenCalled();
  });

  it("脏草稿 + 课程切换 + 取消：恢复原课程与 URL 并保留草稿", async () => {
    addSecondCourse();
    actions.selectCourse.mockImplementation((courseId: string) => {
      state = { ...state, selectedCourseId: courseId };
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(actions.selectCourse).toHaveBeenCalledWith("c1"));
    expect(confirm).toHaveBeenCalledOnce();
    expect(state.selectedCourseId).toBe("c1");
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch1");
    expect(editor).toHaveValue("flowchart LR\nA-->B\nB-->C");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1");
  });

  it("脏草稿 + 课程切换 + 确认：原子切换至新课程有效章节", async () => {
    addSecondCourse();
    const commits: NavigationCommit[] = [];
    const recordCommit = (commit: NavigationCommit) => commits.push(commit);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness onCommit={recordCommit} />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness onCommit={recordCommit} />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch3"));
    expect(confirm).toHaveBeenCalledOnce();
    expect(
      commits.some(
        (commit) => commit.chapterIds.includes("ch3") && commit.editorValue === "flowchart LR\nA-->B\nB-->C",
      ),
    ).toBe(false);
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nE-->F");
    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch3"),
    );
  });

  it("replaces the Mermaid editor instance after confirmed course navigation", async () => {
    addSecondCourse();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const originalEditor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(originalEditor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nE-->F"),
    );
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).not.toBe(originalEditor);
  });

  it("never saves an old draft through the new chapter after an immediate switch click", async () => {
    addSecondCourse();
    mermaidEditorMode.holdLocalDraft = true;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId("current-location")).toHaveTextContent("chapterId=ch3"));
    await userEvent.click(requireAt(screen.getAllByRole("button", { name: "保存 Mermaid" }), 0, "save button"));
    expect(actions.saveChapterBlock).not.toHaveBeenCalledWith(
      "ch3",
      "knowledge_mermaid",
      "flowchart LR\nA-->B\nB-->C",
      "flowchart LR\nE-->F",
    );
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);

    const nextEditor = screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(nextEditor, "\nF-->G");
    await userEvent.click(requireAt(screen.getAllByRole("button", { name: "保存 Mermaid" }), 0, "save button"));
    await waitFor(() =>
      expect(actions.saveChapterBlock).toHaveBeenLastCalledWith(
        "ch3",
        "knowledge_mermaid",
        "flowchart LR\nE-->F\nF-->G",
        "flowchart LR\nE-->F",
      ),
    );
  });

  it("keeps the new chapter dirty when an earlier chapter save finishes late", async () => {
    addSecondCourse();
    mermaidEditorMode.holdLocalDraft = true;
    let finishOldSave: ((value: unknown) => void) | undefined;
    const oldSave = new Promise<unknown>((resolve) => {
      finishOldSave = resolve;
    });
    actions.saveChapterBlock.mockReturnValueOnce(oldSave);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const firstEditor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(firstEditor, "\nB-->C");
    await userEvent.click(requireAt(screen.getAllByRole("button", { name: "保存 Mermaid" }), 0, "save button"));
    expect(actions.saveChapterBlock).toHaveBeenCalledWith(
      "ch1",
      "knowledge_mermaid",
      "flowchart LR\nA-->B\nB-->C",
      "flowchart LR\nA-->B",
    );

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nE-->F"),
    );
    const secondEditor = screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(secondEditor, "\nF-->G");
    const beforeOldSaveFinishes = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(beforeOldSaveFinishes);
    expect(beforeOldSaveFinishes.defaultPrevented).toBe(true);

    await act(async () => {
      finishOldSave?.({ ...blocks[5], body: "flowchart LR\nA-->B\nB-->C" });
      await oldSave;
    });

    const afterOldSaveFinishes = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(afterOldSaveFinishes);
    expect(afterOldSaveFinishes.defaultPrevented).toBe(true);
  });

  it("keeps a new A draft dirty when an old A save finishes after A to B to A navigation", async () => {
    addSecondCourse();
    mermaidEditorMode.holdLocalDraft = true;
    let finishOldSave: ((value: unknown) => void) | undefined;
    const oldSave = new Promise<unknown>((resolve) => {
      finishOldSave = resolve;
    });
    actions.saveChapterBlock.mockReturnValueOnce(oldSave);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const originalEditor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(originalEditor, "\nB-->C");
    await userEvent.click(requireAt(screen.getAllByRole("button", { name: "保存 Mermaid" }), 0, "save button"));

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch3"));

    state = { ...state, selectedCourseId: "c1" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch3"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch1"));
    const newEditor = screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    expect(newEditor).not.toBe(originalEditor);
    await userEvent.type(newEditor, "\nA-->D");

    await act(async () => {
      finishOldSave?.({ ...blocks[5], body: "flowchart LR\nA-->B\nB-->C" });
      await oldSave;
    });

    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });

  it("prevents edits while the current Mermaid save is pending", async () => {
    let finishSave: ((value: unknown) => void) | undefined;
    const pendingSave = new Promise<unknown>((resolve) => {
      finishSave = resolve;
    });
    actions.saveChapterBlock.mockReturnValueOnce(pendingSave);
    render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");
    await userEvent.click(requireAt(screen.getAllByRole("button", { name: "保存 Mermaid" }), 0, "save button"));

    expect(editor).toBeDisabled();
    await userEvent.type(editor, "\nC-->D");
    expect(editor).toHaveValue("flowchart LR\nA-->B\nB-->C");

    await act(async () => {
      finishSave?.({ ...blocks[5], body: "flowchart LR\nA-->B\nB-->C" });
      await pendingSave;
    });
  });

  it("rolls back a loaded empty course when dirty navigation is cancelled", async () => {
    addEmptySecondCourse();
    actions.selectCourse.mockImplementation((courseId: string) => {
      state = { ...state, selectedCourseId: courseId };
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(actions.selectCourse).toHaveBeenCalledWith("c1"));
    expect(confirm).toHaveBeenCalledOnce();
    expect(state.selectedCourseId).toBe("c1");
    expect(editor).toHaveValue("flowchart LR\nA-->B\nB-->C");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1");
  });

  it("accepts a loaded empty course by clearing the draft, chapter and URL", async () => {
    addEmptySecondCourse();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    expect(state.selectedCourseId).toBe("c2");
    expect(screen.queryByRole("textbox", { name: "知识结构图 Mermaid 源码" })).not.toBeInTheDocument();
    expect(screen.getByText("还没有精读结果")).toBeInTheDocument();
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter");
    expect(screen.getByTestId("current-location")).not.toHaveTextContent("chapterId");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
  });

  it("rolls back the latest failed course load without losing the accepted draft", async () => {
    addThirdCourse();
    mermaidEditorMode.holdLocalDraft = true;
    actions.selectCourse.mockImplementation((courseId: string) => {
      state = { ...state, selectedCourseId: courseId };
    });
    actions.loadSources.mockImplementation(async (courseId: string) => {
      if (courseId === "c2") throw new Error("课程 B 加载失败");
      return ((state.sources as Record<string, unknown[]>)[courseId] ?? []) as unknown[];
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(actions.selectCourse).toHaveBeenCalledWith("c1"));
    expect(state.selectedCourseId).toBe("c1");
    expect(screen.getByText("课程 B 加载失败")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toBe(editor);
    expect(editor).toHaveValue("flowchart LR\nA-->B\nB-->C");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    state = { ...state, selectedCourseId: "c3" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch4"));
    expect(screen.queryByText("课程 B 加载失败")).not.toBeInTheDocument();
  });

  it("does not accept cached B when its latest course load fails", async () => {
    addSecondCourse();
    mermaidEditorMode.holdLocalDraft = true;
    let failSecondCourseLoad: ((reason: Error) => void) | undefined;
    const secondCourseLoad = new Promise<unknown[]>((_, reject) => {
      failSecondCourseLoad = reject;
    });
    actions.loadSources.mockImplementation(async (courseId: string) => {
      if (courseId === "c2") return secondCourseLoad;
      return ((state.sources as Record<string, unknown[]>)[courseId] ?? []) as unknown[];
    });
    actions.selectCourse.mockImplementation((courseId: string) => {
      state = { ...state, selectedCourseId: courseId };
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(actions.loadSources).toHaveBeenCalledWith("c2"));
    await act(async () => undefined);
    await act(async () => {
      failSecondCourseLoad?.(new Error("缓存课程 B 最新加载失败"));
      try {
        await secondCourseLoad;
      } catch {
        // The component owns the expected rejection.
      }
    });

    await waitFor(() => expect(actions.selectCourse).toHaveBeenCalledWith("c1"));
    expect(state.selectedCourseId).toBe("c1");
    expect(screen.getByText("缓存课程 B 最新加载失败")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toBe(editor);
    expect(editor).toHaveValue("flowchart LR\nA-->B\nB-->C");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch1");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("requires a fresh B success after an older B request populates its cache", async () => {
    addThirdCourse();
    let finishOldSecondCourseLoad: ((items: unknown[]) => void) | undefined;
    let failLatestSecondCourseLoad: ((reason: Error) => void) | undefined;
    const oldSecondCourseLoad = new Promise<unknown[]>((resolve) => {
      finishOldSecondCourseLoad = resolve;
    });
    const latestSecondCourseLoad = new Promise<unknown[]>((_, reject) => {
      failLatestSecondCourseLoad = reject;
    });
    let secondCourseAttempts = 0;
    actions.loadSources.mockImplementation(async (courseId: string) => {
      if (courseId === "c2") {
        secondCourseAttempts += 1;
        return secondCourseAttempts === 1 ? oldSecondCourseLoad : latestSecondCourseLoad;
      }
      return ((state.sources as Record<string, unknown[]>)[courseId] ?? []) as unknown[];
    });
    actions.selectCourse.mockImplementation((courseId: string) => {
      state = { ...state, selectedCourseId: courseId };
    });
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(secondCourseAttempts).toBe(1));

    state = { ...state, selectedCourseId: "c3" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch4"));

    const secondCourseSource = {
      id: "s2",
      course_id: "c2",
      title: "组织教材",
      kind: "main",
      file_path: "/org.pdf",
      status: "READY",
    };
    await act(async () => {
      state = {
        ...state,
        sources: { ...(state.sources as Record<string, unknown[]>), c2: [secondCourseSource] },
        chapters: {
          ...(state.chapters as Record<string, unknown[]>),
          s2: [{ ...chapter("ch3", "组织能力"), source_id: "s2", course_id: "c2", seq: 0 }],
        },
        noteBlocksByChapter: {
          ...(state.noteBlocksByChapter as Record<string, unknown[]>),
          ch3: thirdChapterBlocks,
        },
        chapterRunsById: { ...(state.chapterRunsById as Record<string, unknown[]>), ch3: [] },
      };
      finishOldSecondCourseLoad?.([secondCourseSource]);
      await oldSecondCourseLoad;
    });
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch4"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch4"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(secondCourseAttempts).toBeGreaterThanOrEqual(2));
    await act(async () => undefined);
    await act(async () => {
      failLatestSecondCourseLoad?.(new Error("课程 B 第二次加载失败"));
      try {
        await latestSecondCourseLoad;
      } catch {
        // The component owns the expected rejection.
      }
    });

    await waitFor(() => expect(actions.selectCourse).toHaveBeenLastCalledWith("c3"));
    expect(state.selectedCourseId).toBe("c3");
    expect(screen.getByText("课程 B 第二次加载失败")).toBeInTheDocument();
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch4");
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nG-->H");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch4");
  });

  it("ignores an earlier course failure after a later course succeeds", async () => {
    addThirdCourse();
    let failSecondCourseLoad: ((reason: Error) => void) | undefined;
    const secondCourseLoad = new Promise<unknown[]>((_, reject) => {
      failSecondCourseLoad = reject;
    });
    actions.loadSources.mockImplementation(async (courseId: string) => {
      if (courseId === "c2") return secondCourseLoad;
      return ((state.sources as Record<string, unknown[]>)[courseId] ?? []) as unknown[];
    });
    actions.selectCourse.mockImplementation((courseId: string) => {
      state = { ...state, selectedCourseId: courseId };
    });
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(actions.loadSources).toHaveBeenCalledWith("c2"));

    state = { ...state, selectedCourseId: "c3" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch4"));
    await waitFor(() => expect(actions.loadChapters).toHaveBeenCalledWith("s3"));

    await act(async () => {
      failSecondCourseLoad?.(new Error("迟到的课程 B 加载失败"));
      try {
        await secondCourseLoad;
      } catch {
        // The rejected request is expected; the component must ignore it as stale.
      }
    });

    expect(state.selectedCourseId).toBe("c3");
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch4");
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nG-->H");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch4");
    expect(screen.queryByText("迟到的课程 B 加载失败")).not.toBeInTheDocument();
    expect(actions.selectCourse).not.toHaveBeenCalled();
  });

  it("keeps the latest course when an earlier course load finishes after A to B to C navigation", async () => {
    addThirdCourse();
    mermaidEditorMode.holdLocalDraft = true;
    let finishSecondCourseLoad: ((items: unknown[]) => void) | undefined;
    const secondCourseLoad = new Promise<unknown[]>((resolve) => {
      finishSecondCourseLoad = resolve;
    });
    actions.loadSources.mockImplementation(async (courseId: string) => {
      if (courseId === "c2") return secondCourseLoad;
      return ((state.sources as Record<string, unknown[]>)[courseId] ?? []) as unknown[];
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    await userEvent.type(editor, "\nB-->C");

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    state = { ...state, selectedCourseId: "c3" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("current-location")).toHaveTextContent("chapterId=ch4"));

    await act(async () => {
      const source = {
        id: "s2",
        course_id: "c2",
        title: "组织教材",
        kind: "main",
        file_path: "/org.pdf",
        status: "READY",
      };
      state = {
        ...state,
        sources: { ...(state.sources as Record<string, unknown[]>), c2: [source] },
        chapters: {
          ...(state.chapters as Record<string, unknown[]>),
          s2: [{ ...chapter("ch3", "组织能力"), source_id: "s2", course_id: "c2", seq: 0 }],
        },
      };
      finishSecondCourseLoad?.([source]);
      await secondCourseLoad;
    });
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch4"));
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nG-->H");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch4");
    expect(confirm).toHaveBeenCalledOnce();
  });

  it("ignores a B chapter content failure after navigating to C", async () => {
    addSecondCourse();
    addThirdCourse();
    let failSecondCourseContent: ((reason: Error) => void) | undefined;
    const secondCourseContent = new Promise<unknown[]>((_, reject) => {
      failSecondCourseContent = reject;
    });
    actions.loadChapterNoteBlocks.mockImplementation(async (chapterId: string) => {
      if (chapterId === "ch3") return secondCourseContent;
      if (chapterId === "ch4") return fourthChapterBlocks;
      return blocks;
    });
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });

    state = { ...state, selectedCourseId: "c2" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch1"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch3"));
    await waitFor(() => expect(actions.loadChapterNoteBlocks).toHaveBeenCalledWith("ch3"));

    state = { ...state, selectedCourseId: "c3" };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter?chapterId=ch3"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch4"));

    await act(async () => {
      failSecondCourseContent?.(new Error("迟到的课程 B 章节内容失败"));
      try {
        await secondCourseContent;
      } catch {
        // The component owns the expected rejection.
      }
    });

    expect(screen.queryByText("迟到的课程 B 章节内容失败")).not.toBeInTheDocument();
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch4");
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nG-->H");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch4");
  });

  it("ignores a cancelled initial chapter content failure", async () => {
    let failInitialContent: ((reason: Error) => void) | undefined;
    const initialContent = new Promise<unknown[]>((_, reject) => {
      failInitialContent = reject;
    });
    state = {
      ...state,
      noteBlocksByChapter: { ch1: [], ch2: [] },
      chapterRunsById: { ch1: [], ch2: [] },
    };
    actions.loadChapterNoteBlocks.mockImplementation(async (chapterId: string) => {
      if (chapterId === "ch1") return initialContent;
      state = {
        ...state,
        noteBlocksByChapter: { ch1: [], ch2: secondChapterBlocks },
      };
      return secondChapterBlocks;
    });
    render(
      <MemoryRouter initialEntries={["/workbench/chapter"]}>
        <ChapterNavigationHarness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(actions.loadChapterNoteBlocks).toHaveBeenCalledWith("ch1"));

    await userEvent.click(screen.getByRole("button", { name: "导航到第二章" }));
    await waitFor(() => expect(screen.getByLabelText("选择章节")).toHaveValue("ch2"));

    await act(async () => {
      failInitialContent?.(new Error("已取消的初始章节加载失败"));
      try {
        await initialContent;
      } catch {
        // The component owns the expected rejection.
      }
    });

    expect(screen.queryByText("已取消的初始章节加载失败")).not.toBeInTheDocument();
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch2");
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nC-->D");
    expect(screen.getByTestId("current-location")).toHaveTextContent("/workbench/chapter?chapterId=ch2");
  });

  it("keeps a dirty non-first chapter active when an older chapter load finishes late", async () => {
    let finishOldLoad: (() => void) | undefined;
    const oldLoad = new Promise<void>((resolve) => {
      finishOldLoad = resolve;
    });
    state = {
      ...state,
      noteBlocksByChapter: { ch1: [], ch2: [] },
    };
    actions.loadChapterNoteBlocks.mockImplementation(async (chapterId: string) => {
      if (chapterId === "ch1" && (state.noteBlocksByChapter as Record<string, unknown[]>).ch1?.length === 0) {
        await oldLoad;
        state = {
          ...state,
          noteBlocksByChapter: { ch1: blocks, ch2: secondChapterBlocks },
        };
        return blocks;
      }
      return chapterId === "ch2" ? secondChapterBlocks : blocks;
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const view = render(
      <MemoryRouter initialEntries={["/workbench/chapter"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );

    state = {
      ...state,
      noteBlocksByChapter: { ch1: [], ch2: secondChapterBlocks },
    };
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    const editor = await screen.findByRole("textbox", { name: "知识结构图 Mermaid 源码" });
    expect(screen.getByLabelText("选择章节")).toHaveValue("ch2");
    await userEvent.type(editor, "\nD-->E");

    await act(async () => {
      finishOldLoad?.();
      await oldLoad;
    });
    view.rerender(
      <MemoryRouter initialEntries={["/workbench/chapter"]}>
        <ChapterWorkbench />
      </MemoryRouter>,
    );
    await act(async () => undefined);

    expect(screen.getByLabelText("选择章节")).toHaveValue("ch2");
    expect(screen.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue("flowchart LR\nC-->D\nD-->E");
    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
  });
});
