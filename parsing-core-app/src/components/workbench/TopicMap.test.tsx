import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { Chapter, CourseTopic, Source } from "../../api/workbenchTypes";
import TopicMap from "./TopicMap";

const actions = {
  loadTopics: vi.fn(), generateTopics: vi.fn(), createTopic: vi.fn(), patchTopic: vi.fn(),
  updateTopicMapping: vi.fn(), confirmTopics: vi.fn(), reorderTopics: vi.fn(), deleteTopic: vi.fn(),
  mergeTopics: vi.fn(), splitTopic: vi.fn(),
  retryTopicSync: vi.fn(),
};

let state: Record<string, unknown>;
vi.mock("../../store/useWorkbenchStore", () => ({ useWorkbenchStore: () => state }));

const sources: Source[] = [
  { id: "s1", course_id: "c1", kind: "main", file_path: "/a.pdf", title: "战略管理", status: "READY" },
  { id: "s2", course_id: "c1", kind: "main", file_path: "/b.pdf", title: "组织行为学", status: "READY" },
];
const chapters: Record<string, Chapter[]> = {
  s1: [
    { id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "竞争战略", status: "COMPLETED" },
    { id: "ch2", source_id: "s1", course_id: "c1", seq: 1, title: "公司战略", status: "CONFIRMED" },
  ],
  s2: [{ id: "ch3", source_id: "s2", course_id: "c1", seq: 0, title: "组织动机", status: "COMPLETED" }],
};
const topic = (overrides: Partial<CourseTopic> = {}): CourseTopic => ({
  id: "t1", course_id: "c1", seq: 0, title: "战略选择", description: "比较不同战略路径",
  generation_reason: "跨教材共同解释决策", status: "DRAFT", confirmed: false, stale_reason: "",
  chapter_ids: ["ch1"], blocking_chapter_ids: [], sync_status: "SYNCED", sync_error: "", ...overrides,
});

function reset(overrides: Record<string, unknown> = {}) {
  state = {
    selectedCourseId: "c1", courses: [{ id: "c1", title: "MBA", description: "", root_dir: "/mba" }],
    sources: { c1: sources }, chapters, topicsByCourse: { c1: [topic(), topic({ id: "t2", seq: 1, title: "组织协同", chapter_ids: ["ch3"] })] },
    topicActions: {}, ...actions, ...overrides,
  };
}

function renderMap() {
  return render(<MemoryRouter><TopicMap /></MemoryRouter>);
}

describe("TopicMap", () => {
  beforeEach(() => { vi.clearAllMocks(); Object.values(actions).forEach((fn) => fn.mockResolvedValue([])); reset(); });
  afterEach(cleanup);

  it("disables generation until every chapter is completed and lists textbook plus chapter blockers", () => {
    renderMap();
    expect(screen.getByRole("button", { name: "AI 生成课程主题" })).toBeDisabled();
    expect(screen.getByText("战略管理 · 公司战略")).toBeInTheDocument();
  });

  it("renders topic status, rationale, uncovered chapters and grouped mapping checkboxes with counts", async () => {
    reset({ chapters: { ...chapters, s1: chapters.s1.map((chapter) => ({ ...chapter, status: "COMPLETED" })) } });
    renderMap();
    expect(screen.getAllByText("跨教材共同解释决策").length).toBeGreaterThan(0);
    expect(screen.getByText("未覆盖章节：公司战略")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "战略管理" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /竞争战略.*1 个主题/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /公司战略.*0 个主题/ })).not.toBeChecked();
  });

  it("renames, creates, reorders and deletes through store actions", async () => {
    renderMap();
    await userEvent.click(screen.getByRole("button", { name: "编辑主题" }));
    const name = screen.getByRole("textbox", { name: "主题名称" });
    await userEvent.clear(name); await userEvent.type(name, "战略判断");
    await userEvent.click(screen.getByRole("button", { name: "保存主题" }));
    expect(actions.patchTopic).toHaveBeenCalledWith("t1", expect.objectContaining({ title: "战略判断" }));
    await userEvent.click(screen.getByRole("button", { name: "新建主题" }));
    await userEvent.type(screen.getByRole("textbox", { name: "新主题名称" }), "领导力");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(actions.createTopic).toHaveBeenCalledWith("c1", expect.objectContaining({ title: "领导力" }));
    await userEvent.click(screen.getByRole("button", { name: "下移 战略选择" }));
    expect(actions.reorderTopics).toHaveBeenCalledWith("c1", ["t2", "t1"]);
    await userEvent.click(screen.getByRole("button", { name: "删除 战略选择" }));
    expect(actions.deleteTopic).toHaveBeenCalledWith("c1", "t1");
  });

  it("requires every topic to have chapters before confirming and saves checkbox mappings", async () => {
    reset({ topicsByCourse: { c1: [topic({ chapter_ids: [] })] } });
    renderMap();
    expect(screen.getByRole("button", { name: "确认课程主题目录" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /竞争战略/ }));
    await userEvent.click(screen.getByRole("button", { name: "保存章节映射" }));
    expect(actions.updateTopicMapping).toHaveBeenCalledWith("t1", ["ch1"]);
  });

  it("merges at least two topics and splits only with a name and chapter", async () => {
    reset({ topicsByCourse: { c1: [topic({ chapter_ids: ["ch1", "ch2"] }), topic({ id: "t2", seq: 1, title: "组织协同", chapter_ids: ["ch3"] })] } });
    renderMap();
    await userEvent.click(screen.getByRole("button", { name: "合并主题" }));
    const dialog = screen.getByRole("dialog", { name: "合并主题" });
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "战略选择" }));
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "组织协同" }));
    await userEvent.type(within(dialog).getByRole("textbox", { name: "合并后名称" }), "战略与组织");
    await userEvent.click(within(dialog).getByRole("button", { name: "确认合并" }));
    expect(actions.mergeTopics).toHaveBeenCalledWith("c1", expect.objectContaining({ topic_ids: ["t1", "t2"], chapter_ids: ["ch1", "ch2", "ch3"] }));

    await userEvent.click(screen.getByRole("button", { name: "拆分当前主题" }));
    const split = screen.getByRole("dialog", { name: "拆分主题" });
    expect(within(split).getByRole("button", { name: "确认拆分" })).toBeDisabled();
    await userEvent.type(within(split).getByRole("textbox", { name: "拆分主题名称" }), "细分战略");
    await userEvent.click(within(split).getByRole("checkbox", { name: /竞争战略/ }));
    await userEvent.click(within(split).getByRole("button", { name: "确认拆分" }));
    expect(actions.splitTopic).toHaveBeenCalledWith("t1", expect.objectContaining({ title: "细分战略", new_chapter_ids: ["ch1"] }));
  });

  it("disables split for zero or all chapters and explains that one must remain", async () => {
    reset({ topicsByCourse: { c1: [topic({ chapter_ids: ["ch1", "ch2"] })] } });
    renderMap();
    await userEvent.click(screen.getByRole("button", { name: "拆分当前主题" }));
    const dialog = screen.getByRole("dialog", { name: "拆分主题" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "拆分主题名称" }), "拆分主题");
    expect(within(dialog).getByRole("button", { name: "确认拆分" })).toBeDisabled();
    expect(within(dialog).getByText("原主题必须至少保留一个章节")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /竞争战略/ }));
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /公司战略/ }));
    expect(within(dialog).getByRole("button", { name: "确认拆分" })).toBeDisabled();
  });

  it("opens the stale old result route and renders ordered blocks instead of the editor", async () => {
    const blocks = [
      { id: "b2", topic_id: "t1", kind: "core_concepts", content: "核心概念正文", updated_at: 2 },
      { id: "b1", topic_id: "t1", kind: "overview", content: "主题概要正文", updated_at: 1 },
    ];
    reset({ topicsByCourse: { c1: [topic({ status: "STALE", stale_reason: "映射已变更" })] }, topicBlocksById: {}, loadTopicBlocks: vi.fn(async () => { state = { ...state, topicBlocksById: { t1: blocks } }; return blocks; }) });
    const view = render(<MemoryRouter initialEntries={["/workbench/courses/c1/topics"]}><Routes><Route path="/workbench/courses/:courseId/topics" element={<TopicMap />} /><Route path="/workbench/courses/:courseId/topics/:topicId" element={<TopicMap initialTopicId="t1" oldResult />} /></Routes></MemoryRouter>);
    await userEvent.click(screen.getByRole("link", { name: "查看旧结果" }));
    view.rerender(<MemoryRouter initialEntries={["/workbench/courses/c1/topics/t1"]}><Routes><Route path="/workbench/courses/:courseId/topics/:topicId" element={<TopicMap initialTopicId="t1" oldResult />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("主题概要正文")).toBeInTheDocument();
    expect(screen.getByText("核心概念正文")).toBeInTheDocument();
    expect(screen.getAllByRole("heading").map((node) => node.textContent).slice(-2)).toEqual(["主题概要", "核心概念"]);
    expect(screen.getByRole("link", { name: "返回主题目录" })).toHaveAttribute("href", "/workbench/courses/c1/topics");
    expect(screen.queryByRole("button", { name: "编辑主题" })).not.toBeInTheDocument();
  });

  it("shows explicit empty and Chinese error states for old results", async () => {
    reset({ topicBlocksById: {}, loadTopicBlocks: vi.fn().mockResolvedValueOnce([]) });
    const empty = render(<MemoryRouter><TopicMap initialTopicId="t1" oldResult /></MemoryRouter>);
    expect(screen.getByText("该主题暂无旧结果")).toBeInTheDocument();
    empty.unmount();

    reset({ topicBlocksById: {}, loadTopicBlocks: vi.fn().mockRejectedValueOnce(new Error("旧结果加载失败，请稍后重试")) });
    render(<MemoryRouter><TopicMap initialTopicId="t1" oldResult /></MemoryRouter>);
    expect(await screen.findByRole("alert")).toHaveTextContent("旧结果加载失败，请稍后重试");
  });

  it("ignores an old topic result failure after course and topic switch", async () => {
    let rejectOld!: (reason: Error) => void;
    const loadTopicBlocks = vi.fn()
      .mockReturnValueOnce(new Promise((_, reject) => { rejectOld = reject; }))
      .mockResolvedValueOnce([]);
    reset({ topicBlocksById: {}, loadTopicBlocks });
    const view = render(<MemoryRouter><TopicMap initialTopicId="t1" oldResult /></MemoryRouter>);
    reset({ selectedCourseId: "c2", courses: [{ id: "c2", title: "新课程", description: "", root_dir: "/new" }], topicBlocksById: {}, loadTopicBlocks });
    view.rerender(<MemoryRouter><TopicMap initialTopicId="t2" oldResult /></MemoryRouter>);
    rejectOld(new Error("旧主题加载失败"));
    await waitFor(() => expect(screen.queryByText("旧主题加载失败")).not.toBeInTheDocument());
    expect(screen.getByText("该主题暂无旧结果")).toBeInTheDocument();
    expect(loadTopicBlocks).toHaveBeenLastCalledWith("t2");
  });

  it("renders returned STALE mapping reason and navigable old result link", async () => {
    const stale = topic({ status: "STALE", stale_reason: "topic chapter mapping changed" });
    actions.updateTopicMapping.mockImplementationOnce(async () => {
      state = { ...state, topicsByCourse: { c1: [stale] } };
      return stale;
    });
    const view = renderMap();
    await userEvent.click(screen.getByRole("button", { name: "保存章节映射" }));
    view.rerender(<MemoryRouter><TopicMap /></MemoryRouter>);
    expect(screen.getAllByText("需要更新").length).toBeGreaterThan(0);
    expect(screen.getByText("topic chapter mapping changed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看旧结果" })).toHaveAttribute("href", "/workbench/courses/c1/topics/t1");
  });

  it("shows FAILED and PENDING Markdown sync states in list and detail", () => {
    reset({ topicsByCourse: { c1: [
      topic({ sync_status: "FAILED", sync_error: "Markdown 同步失败，请重试" }),
      topic({ id: "t2", title: "待同步主题", sync_status: "PENDING" }),
    ] } });
    renderMap();
    expect(screen.getAllByText("Markdown 同步失败").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("Markdown 同步失败，请重试").length).toBeGreaterThan(0);
    expect(screen.getByText("待同步")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试同步 待同步主题" })).not.toBeInTheDocument();
  });

  it("retries FAILED sync and renders the returned SYNCED topic", async () => {
    const synced = topic({ sync_status: "SYNCED", sync_error: "" });
    reset({ topicsByCourse: { c1: [topic({ sync_status: "FAILED", sync_error: "Markdown 同步失败" })] } });
    actions.retryTopicSync.mockImplementationOnce(async () => {
      state = { ...state, topicsByCourse: { c1: [synced] } };
      return synced;
    });
    const view = renderMap();
    await userEvent.click(screen.getByRole("button", { name: "重试同步 战略选择" }));
    expect(actions.retryTopicSync).toHaveBeenCalledWith("t1");
    view.rerender(<MemoryRouter><TopicMap /></MemoryRouter>);
    expect(screen.queryByText("Markdown 同步失败")).not.toBeInTheDocument();
  });

  it("keeps FAILED business state and shows safe error when retry fails", async () => {
    reset({ topicsByCourse: { c1: [topic({ status: "STALE", stale_reason: "旧结果可用", sync_status: "FAILED", sync_error: "Markdown 同步失败" })] } });
    actions.retryTopicSync.mockRejectedValueOnce(new Error("文件同步失败，请检查存储空间后重试"));
    renderMap();
    await userEvent.click(screen.getByRole("button", { name: "重试同步 战略选择" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("文件同步失败，请检查存储空间后重试");
    expect(screen.getAllByText("Markdown 同步失败").length).toBeGreaterThan(0);
    expect(screen.getAllByText("需要更新").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "查看旧结果" })).toBeInTheDocument();
  });

  it("ignores an old course retry response after switching courses", async () => {
    let resolve!: (value: CourseTopic) => void;
    actions.retryTopicSync.mockReturnValueOnce(new Promise<CourseTopic>((done) => { resolve = done; }));
    reset({ topicsByCourse: { c1: [topic({ sync_status: "FAILED", sync_error: "Markdown 同步失败" })] } });
    const view = renderMap();
    await userEvent.click(screen.getByRole("button", { name: "重试同步 战略选择" }));
    reset({ selectedCourseId: "c2", courses: [{ id: "c2", title: "新课程", description: "", root_dir: "/new" }], sources: { c2: sources.map((source) => ({ ...source, course_id: "c2" })) }, topicsByCourse: { c2: [topic({ id: "new", course_id: "c2", title: "新主题", sync_status: "FAILED", sync_error: "新课程同步失败" })] } });
    view.rerender(<MemoryRouter><TopicMap /></MemoryRouter>);
    resolve(topic({ sync_status: "SYNCED" }));
    await waitFor(() => expect(screen.getAllByText("新课程同步失败").length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: "重试同步 新主题" })).toBeEnabled();
    expect(screen.queryByText("文件同步失败，请检查存储空间后重试")).not.toBeInTheDocument();
  });

  it("ignores an old course load and disables repeated generation", async () => {
    let resolve!: () => void;
    actions.loadTopics.mockReturnValueOnce(new Promise<void>((done) => { resolve = done; }));
    const view = renderMap();
    reset({ selectedCourseId: "c2", courses: [{ id: "c2", title: "新课程", description: "", root_dir: "/new" }], sources: { c2: [] }, chapters: {}, topicsByCourse: { c2: [] } });
    view.rerender(<MemoryRouter><TopicMap /></MemoryRouter>); resolve();
    await waitFor(() => expect(screen.getByText("新课程还没有教材章节")).toBeInTheDocument());
    reset({ ...state, topicActions: { "generateTopics:c2": { loading: true, error: null } } });
    view.rerender(<MemoryRouter><TopicMap /></MemoryRouter>);
    expect(screen.getByRole("button", { name: "AI 生成课程主题" })).toBeDisabled();
  });

  it("does not let an old write failure change the new course busy or error state", async () => {
    let reject!: (reason: Error) => void;
    actions.createTopic.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    const view = renderMap();
    await userEvent.click(screen.getByRole("button", { name: "新建主题" }));
    await userEvent.type(screen.getByRole("textbox", { name: "新主题名称" }), "旧课程主题");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    reset({ selectedCourseId: "c2", courses: [{ id: "c2", title: "新课程", description: "", root_dir: "/new" }], sources: { c2: sources.map((source) => ({ ...source, course_id: "c2" })) }, topicsByCourse: { c2: [topic({ id: "new", course_id: "c2", title: "新主题" })] } });
    view.rerender(<MemoryRouter><TopicMap /></MemoryRouter>);
    reject(new Error("旧课程失败"));
    await waitFor(() => expect(screen.queryByText("旧课程失败")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "新建主题" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "新主题" })).toBeInTheDocument();
  });
});
