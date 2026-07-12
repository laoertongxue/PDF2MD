import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import TopicFusion from "./TopicFusion";

vi.mock("../MermaidBlock", () => ({ default: ({ code }: { code: string }) => <div data-testid="mermaid">{code}</div> }));
const actions = { loadTopics: vi.fn(), loadTopicBlocks: vi.fn(), loadTopicCards: vi.fn(), loadTopicRuns: vi.fn(), loadSources: vi.fn(), loadChapters: vi.fn(), runTopicHybrid: vi.fn(), recoverTopic: vi.fn(), retryTopicSync: vi.fn(), saveTopicBlock: vi.fn() };
let state: Record<string, unknown>;
vi.mock("../../store/useWorkbenchStore", () => ({ useWorkbenchStore: () => state }));

const kinds = ["overview", "linked_sources", "core_concepts", "viewpoint_comparison", "consensus_disagreements", "complementary_views", "plain_explanation", "textbook_cases", "real_world_problem_solving", "integrated_framework", "application_methods", "further_thinking", "knowledge_mermaid", "application_mermaid"];
const topic = (status = "COMPLETED") => ({ id: "t1", course_id: "c1", seq: 0, title: "战略融合", description: "", generation_reason: "", status, confirmed: true, stale_reason: status === "STALE" ? "章节 ch1 已更新" : "", chapter_ids: ["ch1"], blocking_chapter_ids: status === "NOT_READY" ? ["ch1"] : [], sync_status: "SYNCED", sync_error: "" });

function reset(status = "COMPLETED") {
  state = { selectedCourseId: "c1", courses: [{ id: "c1", title: "MBA", description: "", root_dir: "/mba" }], sources: { c1: [{ id: "s1", course_id: "c1", kind: "main", file_path: "/a", title: "战略教材", status: "READY" }] }, chapters: { s1: [{ id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "竞争战略", status: "COMPLETED" }] }, topicsByCourse: { c1: [topic(status)] }, topicBlocksById: { t1: kinds.map((kind, i) => ({ id: `b${i}`, topic_id: "t1", kind, content: kind.includes("mermaid") ? "flowchart LR\nA-->B" : `正文 ${kind} [《战略教材》·第 1 章]`, updated_at: i + 1 })) }, topicCardsById: { t1: Array.from({ length: 8 }, (_, i) => ({ id: `card${i}`, topic_id: "t1", card_type: "观点", title: `卡片 ${i + 1}`, content: "卡片正文", source_refs: ["[《战略教材》·第 1 章]"], created_at: i })) }, topicRunsById: { t1: [{ id: "r1", topic_id: "t1", round_key: "review", status: status === "FAILED" ? "FAILED" : "COMPLETED", input_fingerprint: "x", output: "", error: status === "FAILED" ? "topic round execution failed" : "", started_at: 1, finished_at: 2 }] }, topicActions: {}, ...actions };
}

describe("TopicFusion", () => {
  beforeEach(() => { vi.clearAllMocks(); Object.values(actions).forEach((fn) => fn.mockResolvedValue(undefined)); reset(); });
  afterEach(cleanup);

  it("renders the fixed 15 sections, source routes, cards and editable Mermaid", async () => {
    render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    expect(screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent)).toEqual([
      "1. 主题概要", "2. 关联教材与章节", "3. 核心概念", "4. 教材观点对照",
      "5. 共识与分歧", "6. 互补视角", "7. 通俗、有趣、生活化的解释", "8. 教材案例解读",
      "9. 现实案例与问题解决", "10. 综合分析框架", "11. 实际应用方法", "12. 延伸思考",
      "13. Mermaid 知识结构图", "14. Mermaid 应用流程图", "15. 写作卡片",
    ]);
    expect(screen.getAllByTestId("mermaid")).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "[《战略教材》·第 1 章]" })[0]).toHaveAttribute("href", "/workbench/chapter?chapterId=ch1");
    expect(screen.getByText("卡片 8")).toBeInTheDocument();
    await userEvent.clear(screen.getAllByRole("textbox", { name: /Mermaid 源码/ })[0]);
    await userEvent.type(screen.getAllByRole("textbox", { name: /Mermaid 源码/ })[0], "flowchart LR\nX-->Y");
    await userEvent.click(screen.getAllByRole("button", { name: "保存 Mermaid" })[0]);
    expect(actions.saveTopicBlock).toHaveBeenCalledWith(
      "t1", "knowledge_mermaid", "flowchart LR\nX-->Y", "flowchart LR\nA-->B",
    );
  });

  it("disables NOT_READY, allows STALE, and keeps old content with a safe FAILED error", () => {
    reset("NOT_READY"); const first = render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    expect(screen.getByRole("button", { name: "运行融合精读" })).toBeDisabled();
    expect(screen.getByText(/竞争战略/)).toBeInTheDocument(); first.unmount();
    reset("STALE"); const second = render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled(); second.unmount();
    reset("FAILED"); render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    expect(screen.getByRole("alert")).toHaveTextContent("topic round execution failed");
    expect(screen.getByRole("button", { name: "检查并恢复" })).toBeEnabled();
    expect(screen.getByText("正文 overview", { exact: false })).toBeInTheDocument();
  });

  it("checks a RUNNING lease, reports an active conflict, and recovers an expired run", async () => {
    reset("RUNNING");
    actions.recoverTopic.mockRejectedValueOnce(new Error("任务仍在运行"));
    const view = render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    const recover = screen.getByRole("button", { name: "检查并恢复" });
    expect(recover).toHaveAttribute("title", "检查运行状态并恢复过期任务");
    await userEvent.click(recover);
    expect(await screen.findByRole("alert")).toHaveTextContent("任务仍在运行");

    actions.recoverTopic.mockImplementationOnce(async () => {
      state = { ...state, topicsByCourse: { c1: [topic("FAILED")] } };
      return topic("FAILED");
    });
    await userEvent.click(recover);
    view.rerender(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled();
  });

  it("ignores an old recovery failure after switching course and topic", async () => {
    let rejectOld!: (reason: Error) => void;
    actions.recoverTopic.mockReturnValueOnce(new Promise((_, reject) => { rejectOld = reject; }));
    reset("RUNNING");
    const view = render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "检查并恢复" }));
    state = { ...state, selectedCourseId: "c2", courses: [{ id: "c2", title: "新课程", description: "", root_dir: "/new" }], topicsByCourse: { c2: [{ ...topic("RUNNING"), id: "t2", course_id: "c2", title: "新主题" }] }, topicBlocksById: { t2: [] }, topicCardsById: { t2: [] }, topicRunsById: { t2: [] }, sources: { c2: [] } };
    view.rerender(<MemoryRouter><TopicFusion courseId="c2" topicId="t2" /></MemoryRouter>);
    rejectOld(new Error("旧任务仍在运行"));
    await waitFor(() => expect(screen.queryByText("旧任务仍在运行")).not.toBeInTheDocument());
  });

  it("stops the loading skeleton and retries after an initial load failure", async () => {
    reset();
    state = { ...state, topicsByCourse: { c1: [] } };
    actions.loadTopics.mockRejectedValueOnce(new Error("主题加载失败"));
    render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);

    expect(await screen.findByRole("alert")).toHaveTextContent("主题加载失败");
    expect(screen.queryByText("正在加载主题…")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试加载" }));
    expect(actions.loadTopics).toHaveBeenCalledTimes(2);
  });

  it("refreshes persisted content after a 507 without an unhandled save rejection", async () => {
    actions.saveTopicBlock.mockRejectedValueOnce(new Error("编辑已保存到数据库，Markdown同步失败，可重试"));
    render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    const editor = screen.getAllByRole("textbox", { name: /Mermaid 源码/ })[0];
    await userEvent.clear(editor); await userEvent.type(editor, "flowchart LR\nX-->Y");
    await userEvent.click(screen.getAllByRole("button", { name: "保存 Mermaid" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("编辑已保存到数据库，Markdown同步失败，可重试");
    expect(actions.loadTopicBlocks).toHaveBeenCalledWith("t1");
    expect(actions.loadTopics).toHaveBeenCalledWith("c1");
  });

  it("ignores an old run failure after switching page", async () => {
    let rejectOld!: (reason: Error) => void;
    actions.runTopicHybrid.mockReturnValueOnce(new Promise((_, reject) => { rejectOld = reject; }));
    reset("READY");
    const view = render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "运行融合精读" }));
    state = { ...state, topicsByCourse: { c2: [{ ...topic("READY"), id: "t2", course_id: "c2", title: "新主题" }] }, topicBlocksById: { t2: [] }, topicCardsById: { t2: [] }, topicRunsById: { t2: [] }, sources: { c2: [] } };
    view.rerender(<MemoryRouter><TopicFusion courseId="c2" topicId="t2" /></MemoryRouter>);
    rejectOld(new Error("旧运行失败"));
    await waitFor(() => expect(screen.queryByText("旧运行失败")).not.toBeInTheDocument());
  });

  it("keeps Mermaid save loading independent and ignores its late success after switching page", async () => {
    let resolveOld!: () => void;
    actions.saveTopicBlock.mockReturnValueOnce(new Promise<void>((resolve) => { resolveOld = resolve; }));
    const view = render(<MemoryRouter><TopicFusion courseId="c1" topicId="t1" /></MemoryRouter>);
    const editor = screen.getAllByRole("textbox", { name: /Mermaid 源码/ })[0];
    await userEvent.clear(editor); await userEvent.type(editor, "flowchart LR\nX-->Y");
    const save = screen.getAllByRole("button", { name: "保存 Mermaid" })[0];
    await userEvent.click(save);
    expect(save).toBeDisabled();
    state = { ...state, topicsByCourse: { c2: [{ ...topic("COMPLETED"), id: "t2", course_id: "c2", title: "新主题" }] }, topicBlocksById: { t2: [] }, topicCardsById: { t2: [] }, topicRunsById: { t2: [] }, sources: { c2: [] } };
    view.rerender(<MemoryRouter><TopicFusion courseId="c2" topicId="t2" /></MemoryRouter>);
    resolveOld();
    await waitFor(() => expect(screen.queryByText("已保存并同步 Markdown")).not.toBeInTheDocument());
  });
});
