import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/workbench";
import type {
  CourseTopic,
  TopicCard,
  TopicNoteBlock,
  TopicRun,
} from "../api/workbenchTypes";
import { useWorkbenchStore } from "./useWorkbenchStore";

vi.mock("../api/workbench", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/workbench")>();
  return {
    ...actual,
    listTopics: vi.fn(),
    generateTopics: vi.fn(),
    mergeTopics: vi.fn(),
    splitTopic: vi.fn(),
    updateTopicMapping: vi.fn(),
    confirmTopics: vi.fn(),
    reorderTopics: vi.fn(),
    runTopic: vi.fn(),
    runTopicHybrid: vi.fn(),
    listTopicNoteBlocks: vi.fn(),
    listTopicCards: vi.fn(),
    listTopicRuns: vi.fn(),
    retryTopicSync: vi.fn(),
    recoverTopic: vi.fn(),
    saveTopicBlock: vi.fn(),
    deleteTopic: vi.fn(),
  };
});

const topic = (overrides: Partial<CourseTopic> = {}): CourseTopic => ({
  id: "topic-1",
  course_id: "course-1",
  seq: 1,
  title: "竞争战略",
  description: "跨教材融合",
  generation_reason: "覆盖共同主题",
  status: "READY",
  confirmed: false,
  stale_reason: "",
  chapter_ids: ["chapter-1"],
  blocking_chapter_ids: [],
  sync_status: "SYNCED",
  sync_error: "",
  ...overrides,
});

const block: TopicNoteBlock = {
  id: "block-1",
  topic_id: "topic-1",
  kind: "summary",
  content: "主题摘要",
  updated_at: 1,
};

const card: TopicCard = {
  id: "card-1",
  topic_id: "topic-1",
  card_type: "insight",
  title: "取舍",
  content: "战略意味着取舍",
  source_refs: ["chapter-1"],
  created_at: 1,
};

const run: TopicRun = {
  id: "run-1",
  topic_id: "topic-1",
  round_key: "review",
  status: "COMPLETED",
  input_fingerprint: "fingerprint",
  output: "通过",
  error: "",
  started_at: 1,
  finished_at: 2,
};

const mocked = vi.mocked(api);

function actionKey(action: string, resourceId: string) {
  return `${action}:${resourceId}`;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  vi.clearAllMocks();
  useWorkbenchStore.setState(useWorkbenchStore.getInitialState(), true);
});

describe("主题工作流 Store", () => {
  it("恢复检查遇到有效租约时保留任务仍在运行安全文案", async () => {
    mocked.recoverTopic.mockRejectedValueOnce(new api.SafeApiError("task_running"));
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic({ status: "RUNNING" })] } });

    await expect(useWorkbenchStore.getState().recoverTopic("topic-1")).rejects.toThrow("任务仍在运行");

    expect(useWorkbenchStore.getState().topicActions["recoverTopic:topic-1"]).toEqual({
      loading: false,
      error: "任务仍在运行",
    });
  });

  it("保存主题 block 后只更新对应缓存内容", async () => {
    const updated = { ...block, content: "flowchart LR\nA-->B", updated_at: 2 };
    mocked.saveTopicBlock.mockResolvedValueOnce(updated);
    useWorkbenchStore.setState({ topicBlocksById: { "topic-1": [block] } });

    await useWorkbenchStore.getState().saveTopicBlock("topic-1", "summary", updated.content, block.content);

    expect(mocked.saveTopicBlock).toHaveBeenCalledWith("topic-1", "summary", updated.content, block.content);
    expect(useWorkbenchStore.getState().topicBlocksById["topic-1"]).toEqual([updated]);
  });

  it.each([
    ["conflict", "当前状态不允许此操作，请刷新后重试"],
    ["storage", "文件同步失败，请检查存储空间后重试"],
    ["protocol", "服务返回数据格式异常，请稍后重试"],
  ] as const)("Store 保留 API 的 %s 安全错误分类", async (category, message) => {
    mocked.listTopics.mockRejectedValueOnce(new api.SafeApiError(category));

    await expect(useWorkbenchStore.getState().loadTopics("course-1")).rejects.toThrow(message);

    expect(useWorkbenchStore.getState().topicActions[actionKey("loadTopics", "course-1")]).toEqual({
      loading: false,
      error: message,
    });
  });

  it("Store 对未知异常统一兜底且不泄露路径或密钥", async () => {
    mocked.listTopics.mockRejectedValueOnce(new Error("/Users/张三 sk-secret"));

    await expect(useWorkbenchStore.getState().loadTopics("course-1")).rejects.toThrow("操作失败，请稍后重试");

    const error = useWorkbenchStore.getState().topicActions[actionKey("loadTopics", "course-1")].error;
    expect(error).toBe("操作失败，请稍后重试");
    expect(error).not.toMatch(/Users|secret/i);
  });

  it("API 将 422 转换为准确中文且不泄露响应详情", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "invalid /Users/张三 sk-secret" }), { status: 422 }),
    );

    await expect(actualApi.listTopics("course-1")).rejects.toThrow("请求内容或格式无效，请检查后重试");
  });

  it.each([
    [409, "当前状态不允许此操作，请刷新后重试"],
    [507, "文件同步失败，请检查存储空间后重试"],
  ] as const)("API 将 HTTP %s 转换为固定安全文案", async (status, message) => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ detail: "/Users/张三 sk-secret" }, status));

    await expect(actualApi.getTopic("topic-1")).rejects.toThrow(message);
  });

  it("deleteTopic 使用 DELETE 并接受 204 空响应", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(actualApi.deleteTopic("topic-1")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/workbench/topics/topic-1",
      { method: "DELETE" },
    );
  });

  it("Topic API 拒绝对象代替数组", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse(topic()));

    await expect(actualApi.listTopics("course-1")).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it("Topic API 拒绝缺少 course_id 的对象", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    const { course_id: _courseId, ...invalidTopic } = topic();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse(invalidTopic));

    await expect(actualApi.getTopic("topic-1")).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it("Topic API 拒绝未知主题状态", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ ...topic(), status: "UNKNOWN" }));

    await expect(actualApi.getTopic("topic-1")).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it("Topic API 拒绝未知同步状态", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ ...topic(), sync_status: "UNKNOWN" }));

    await expect(actualApi.getTopic("topic-1")).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it.each([
    ["blocks", (actualApi: typeof api) => actualApi.listTopicNoteBlocks("topic-1"), { id: 1, topic_id: "topic-1", kind: "summary", content: "x", updated_at: 1 }],
    ["cards", (actualApi: typeof api) => actualApi.listTopicCards("topic-1"), { id: "card-1", topic_id: "topic-1", card_type: "insight", title: "x", content: "x", source_refs: "chapter-1", created_at: 1 }],
    ["runs", (actualApi: typeof api) => actualApi.listTopicRuns("topic-1"), { id: "run-1", topic_id: "topic-1", round_key: "review", status: "DONE", input_fingerprint: "x", output: "x", error: "", started_at: 1, finished_at: 2 }],
  ] as const)("Topic %s API 拒绝核心字段类型错误", async (_name, invoke, invalidItem) => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse([invalidItem]));

    await expect(invoke(actualApi)).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it.each([
    ["非 JSON", new Response("not-json", { status: 200 })],
    ["空 200", new Response(null, { status: 200 })],
  ] as const)("成功响应为%s时报告协议错误", async (_name, response) => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response);

    await expect(actualApi.getTopic("topic-1")).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it("AbortError 与网络不可达使用不同安全文案", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new DOMException("secret path", "AbortError"))
      .mockRejectedValueOnce(new TypeError("fetch failed /Users/secret"));

    await expect(actualApi.getTopic("topic-1")).rejects.toThrow("操作已取消");
    await expect(actualApi.getTopic("topic-1")).rejects.toThrow("无法连接本地服务，请确认服务已启动");
  });

  it("API 网络错误不泄露中文路径或密钥", async () => {
    const actualApi = await vi.importActual<typeof import("../api/workbench")>("../api/workbench");
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(
      new TypeError("无法读取 /Users/张三/课程，密钥 sk-secret"),
    );

    await expect(actualApi.listTopics("course-1")).rejects.toThrow("无法连接本地服务，请确认服务已启动");
  });

  it("加载课程主题并按课程保存", async () => {
    mocked.listTopics.mockResolvedValue([topic()]);

    await useWorkbenchStore.getState().loadTopics("course-1");

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toEqual([topic()]);
    expect(useWorkbenchStore.getState().topicActions[actionKey("loadTopics", "course-1")]).toEqual({
      loading: false,
      error: null,
    });
  });

  it("生成课程主题并替换该课程主题列表", async () => {
    mocked.generateTopics.mockResolvedValue([topic({ status: "DRAFT" })]);

    await useWorkbenchStore.getState().generateTopics("course-1", "hybrid");

    expect(mocked.generateTopics).toHaveBeenCalledWith("course-1", "hybrid");
    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].status).toBe("DRAFT");
  });

  it("更新主题章节映射并更新所属课程中的主题", async () => {
    mocked.updateTopicMapping.mockResolvedValue(topic({ chapter_ids: ["chapter-2"] }));

    await useWorkbenchStore.getState().updateTopicMapping("topic-1", ["chapter-2"]);

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].chapter_ids).toEqual(["chapter-2"]);
  });

  it("原子合并后 tombstone 旧主题并保存新主题", async () => {
    const merged = topic({ id: "merged", title: "Merged", chapter_ids: ["chapter-1", "chapter-2"] });
    mocked.mergeTopics.mockResolvedValue(merged);
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic(), topic({ id: "topic-2" })] } });
    await useWorkbenchStore.getState().mergeTopics("course-1", { topic_ids: ["topic-1", "topic-2"], title: "Merged" });
    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toEqual([merged]);
    expect(useWorkbenchStore.getState().deletedTopics).toMatchObject({ "topic-1": true, "topic-2": true });
  });

  it("merge 晚响应不能覆盖响应所属课程的较新列表", async () => {
    const merging = deferred<CourseTopic>();
    mocked.mergeTopics.mockReturnValueOnce(merging.promise);
    mocked.listTopics.mockResolvedValueOnce([topic({ course_id: "course-2", title: "Course 2 fresh" })]);
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic(), topic({ id: "topic-2" })] } });
    const pending = useWorkbenchStore.getState().mergeTopics("course-1", { topic_ids: ["topic-1", "topic-2"], title: "Merged" });
    await useWorkbenchStore.getState().loadTopics("course-2");
    merging.resolve(topic({ id: "merged", course_id: "course-2", title: "Late" }));
    await pending;
    expect(useWorkbenchStore.getState().topicsByCourse["course-2"][0].title).toBe("Course 2 fresh");
    expect(useWorkbenchStore.getState().deletedTopics["topic-1"]).toBeUndefined();
  });

  it("原子拆分同时保存原主题和新主题", async () => {
    const original = topic({ chapter_ids: ["chapter-1"] });
    const created = topic({ id: "topic-2", chapter_ids: ["chapter-2"] });
    mocked.splitTopic.mockResolvedValue([original, created]);
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic({ chapter_ids: ["chapter-1", "chapter-2"] })] } });
    await useWorkbenchStore.getState().splitTopic("topic-1", { title: "New", new_chapter_ids: ["chapter-2"] });
    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toEqual([original, created]);
  });

  it("确认课程主题目录并保存全部主题", async () => {
    mocked.confirmTopics.mockResolvedValue([topic({ confirmed: true })]);

    await useWorkbenchStore.getState().confirmTopics("course-1");

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].confirmed).toBe(true);
  });

  it("重排课程主题并保存后端返回顺序", async () => {
    const reordered = [topic({ id: "topic-2", seq: 1 }), topic({ seq: 2 })];
    mocked.reorderTopics.mockResolvedValue(reordered);

    await useWorkbenchStore.getState().reorderTopics("course-1", ["topic-2", "topic-1"]);

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toEqual(reordered);
  });

  it("删除主题后清理课程列表及该主题全部缓存", async () => {
    mocked.deleteTopic.mockResolvedValue(undefined);
    useWorkbenchStore.setState({
      topicsByCourse: { "course-1": [topic(), topic({ id: "topic-2", seq: 2 })] },
      topicBlocksById: { "topic-1": [block] },
      topicCardsById: { "topic-1": [card] },
      topicRunsById: { "topic-1": [run] },
    });

    await useWorkbenchStore.getState().deleteTopic("course-1", "topic-1");

    const state = useWorkbenchStore.getState();
    expect(state.topicsByCourse["course-1"].map((item) => item.id)).toEqual(["topic-2"]);
    expect(state.topicBlocksById["topic-1"]).toBeUndefined();
    expect(state.topicCardsById["topic-1"]).toBeUndefined();
    expect(state.topicRunsById["topic-1"]).toBeUndefined();
  });

  it.each([
    ["普通融合", "runTopic", () => useWorkbenchStore.getState().runTopic("topic-1"), mocked.runTopic],
    ["hybrid 融合", "runTopicHybrid", () => useWorkbenchStore.getState().runTopicHybrid("topic-1"), mocked.runTopicHybrid],
  ] as const)("运行%s后更新主题", async (_label, _action, invoke, mock) => {
    mock.mockResolvedValue(topic({ status: "COMPLETED" }));

    await invoke();

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].status).toBe("COMPLETED");
  });

  it("分别加载主题 blocks、cards 和 runs", async () => {
    mocked.listTopicNoteBlocks.mockResolvedValue([block]);
    mocked.listTopicCards.mockResolvedValue([card]);
    mocked.listTopicRuns.mockResolvedValue([run]);

    await Promise.all([
      useWorkbenchStore.getState().loadTopicBlocks("topic-1"),
      useWorkbenchStore.getState().loadTopicCards("topic-1"),
      useWorkbenchStore.getState().loadTopicRuns("topic-1"),
    ]);

    const state = useWorkbenchStore.getState();
    expect(state.topicBlocksById["topic-1"]).toEqual([block]);
    expect(state.topicCardsById["topic-1"]).toEqual([card]);
    expect(state.topicRunsById["topic-1"]).toEqual([run]);
  });

  it.each([
    ["同步重试", "retryTopicSync", () => useWorkbenchStore.getState().retryTopicSync("topic-1"), mocked.retryTopicSync],
    ["中断恢复", "recoverTopic", () => useWorkbenchStore.getState().recoverTopic("topic-1"), mocked.recoverTopic],
  ] as const)("支持%s并更新主题", async (_label, _action, invoke, mock) => {
    mock.mockResolvedValue(topic({ sync_status: "SYNCED" }));

    await invoke();

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].sync_status).toBe("SYNCED");
  });

  it.each([
    ["loadTopics", "course-1", () => useWorkbenchStore.getState().loadTopics("course-1"), mocked.listTopics],
    ["generateTopics", "course-1", () => useWorkbenchStore.getState().generateTopics("course-1"), mocked.generateTopics],
    ["mergeTopics", "course-1", () => useWorkbenchStore.getState().mergeTopics("course-1", { topic_ids: ["topic-1", "topic-2"], title: "Merged" }), mocked.mergeTopics],
    ["splitTopic", "topic-1", () => useWorkbenchStore.getState().splitTopic("topic-1", { title: "New", new_chapter_ids: ["chapter-1"] }), mocked.splitTopic],
    ["updateTopicMapping", "topic-1", () => useWorkbenchStore.getState().updateTopicMapping("topic-1", ["chapter-1"]), mocked.updateTopicMapping],
    ["confirmTopics", "course-1", () => useWorkbenchStore.getState().confirmTopics("course-1"), mocked.confirmTopics],
    ["reorderTopics", "course-1", () => useWorkbenchStore.getState().reorderTopics("course-1", ["topic-1"]), mocked.reorderTopics],
    ["runTopic", "topic-1", () => useWorkbenchStore.getState().runTopic("topic-1"), mocked.runTopic],
    ["runTopicHybrid", "topic-1", () => useWorkbenchStore.getState().runTopicHybrid("topic-1"), mocked.runTopicHybrid],
    ["loadTopicBlocks", "topic-1", () => useWorkbenchStore.getState().loadTopicBlocks("topic-1"), mocked.listTopicNoteBlocks],
    ["loadTopicCards", "topic-1", () => useWorkbenchStore.getState().loadTopicCards("topic-1"), mocked.listTopicCards],
    ["loadTopicRuns", "topic-1", () => useWorkbenchStore.getState().loadTopicRuns("topic-1"), mocked.listTopicRuns],
    ["retryTopicSync", "topic-1", () => useWorkbenchStore.getState().retryTopicSync("topic-1"), mocked.retryTopicSync],
    ["recoverTopic", "topic-1", () => useWorkbenchStore.getState().recoverTopic("topic-1"), mocked.recoverTopic],
    ["deleteTopic", "topic-1", () => useWorkbenchStore.getState().deleteTopic("course-1", "topic-1"), mocked.deleteTopic],
  ] as const)("%s 失败时清除自己的 loading 并保存中文错误", async (action, resourceId, invoke, mock) => {
    mock.mockRejectedValue(new Error("HTTP 500 /Users/me/private sk-secret"));

    await expect(invoke()).rejects.toThrow("操作失败，请稍后重试");

    const status = useWorkbenchStore.getState().topicActions[actionKey(action, resourceId)];
    expect(status.loading).toBe(false);
    expect(status.error).toBe("操作失败，请稍后重试");
    expect(status.error).not.toMatch(/HTTP|Users|secret/i);
  });

  it("相同课程并发加载时旧响应不会覆盖新响应或清除较新 loading", async () => {
    const oldRequest = deferred<CourseTopic[]>();
    const newRequest = deferred<CourseTopic[]>();
    mocked.listTopics.mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(newRequest.promise);

    const oldLoad = useWorkbenchStore.getState().loadTopics("course-1");
    const newLoad = useWorkbenchStore.getState().loadTopics("course-1");
    oldRequest.resolve([topic({ title: "旧主题" })]);
    await oldLoad;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toBeUndefined();
    expect(useWorkbenchStore.getState().topicActions[actionKey("loadTopics", "course-1")].loading).toBe(true);

    newRequest.resolve([topic({ title: "新主题" })]);
    await newLoad;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].title).toBe("新主题");
    expect(useWorkbenchStore.getState().topicActions[actionKey("loadTopics", "course-1")].loading).toBe(false);
  });

  it("旧 loadTopics 晚于 generateTopics 返回时不能覆盖生成结果", async () => {
    const oldLoad = deferred<CourseTopic[]>();
    mocked.listTopics.mockReturnValueOnce(oldLoad.promise);
    mocked.generateTopics.mockResolvedValueOnce([topic({ title: "新生成主题" })]);

    const loading = useWorkbenchStore.getState().loadTopics("course-1");
    await useWorkbenchStore.getState().generateTopics("course-1");
    oldLoad.resolve([topic({ title: "旧加载主题" })]);
    await loading;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].title).toBe("新生成主题");
  });

  it("旧 runTopic 晚于 updateTopicMapping 返回时不能覆盖新主题", async () => {
    const oldRun = deferred<CourseTopic>();
    mocked.runTopic.mockReturnValueOnce(oldRun.promise);
    mocked.updateTopicMapping.mockResolvedValueOnce(topic({ chapter_ids: ["chapter-2"] }));

    const running = useWorkbenchStore.getState().runTopic("topic-1");
    await useWorkbenchStore.getState().updateTopicMapping("topic-1", ["chapter-2"]);
    oldRun.resolve(topic({ chapter_ids: ["chapter-1"], status: "COMPLETED" }));
    await running;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].chapter_ids).toEqual(["chapter-2"]);
  });

  it("旧 loadTopics 晚于 updateTopicMapping 返回时不能覆盖新映射", async () => {
    const oldLoad = deferred<CourseTopic[]>();
    mocked.listTopics.mockReturnValueOnce(oldLoad.promise);
    mocked.updateTopicMapping.mockResolvedValueOnce(topic({ chapter_ids: ["chapter-2"] }));
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic()] } });

    const loading = useWorkbenchStore.getState().loadTopics("course-1");
    await useWorkbenchStore.getState().updateTopicMapping("topic-1", ["chapter-2"]);
    oldLoad.resolve([topic({ chapter_ids: ["chapter-1"] })]);
    await loading;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].chapter_ids).toEqual(["chapter-2"]);
  });

  it("缓存中无主题时 recoverTopic 响应仍阻止旧课程列表随后覆盖", async () => {
    const oldLoad = deferred<CourseTopic[]>();
    mocked.listTopics.mockReturnValueOnce(oldLoad.promise);
    mocked.recoverTopic.mockResolvedValueOnce(topic({ status: "FAILED" }));

    const loading = useWorkbenchStore.getState().loadTopics("course-1");
    await useWorkbenchStore.getState().recoverTopic("topic-1");
    oldLoad.resolve([topic({ status: "RUNNING" })]);
    await loading;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"][0].status).toBe("FAILED");
  });

  it("无缓存的旧 recoverTopic 不得在较新 loadTopics 完成后抢占课程列表", async () => {
    const oldRecover = deferred<CourseTopic>();
    mocked.recoverTopic.mockReturnValueOnce(oldRecover.promise);
    mocked.listTopics.mockResolvedValueOnce([topic({ title: "新加载主题", status: "READY" })]);

    const recovering = useWorkbenchStore.getState().recoverTopic("topic-1");
    await useWorkbenchStore.getState().loadTopics("course-1");
    oldRecover.resolve(topic({ title: "旧恢复主题", status: "FAILED" }));
    await recovering;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toEqual([
      topic({ title: "新加载主题", status: "READY" }),
    ]);
  });

  it("无缓存的旧 runTopic 失败不污染较新课程加载状态", async () => {
    const oldRun = deferred<CourseTopic>();
    mocked.runTopic.mockReturnValueOnce(oldRun.promise);
    mocked.listTopics.mockResolvedValueOnce([topic({ title: "新加载主题" })]);

    const running = useWorkbenchStore.getState().runTopic("topic-1");
    await useWorkbenchStore.getState().loadTopics("course-1");
    oldRun.reject(new Error("old failure"));
    await expect(running).rejects.toThrow("操作失败，请稍后重试");

    const state = useWorkbenchStore.getState();
    expect(state.topicsByCourse["course-1"][0].title).toBe("新加载主题");
    expect(state.topicActions[actionKey("loadTopics", "course-1")]).toEqual({ loading: false, error: null });
  });

  it("旧 loadTopics 失败不污染较新 mapping 状态或其 action 状态", async () => {
    const oldLoad = deferred<CourseTopic[]>();
    mocked.listTopics.mockReturnValueOnce(oldLoad.promise);
    mocked.updateTopicMapping.mockResolvedValueOnce(topic({ chapter_ids: ["chapter-2"] }));
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic()] } });

    const loading = useWorkbenchStore.getState().loadTopics("course-1");
    await useWorkbenchStore.getState().updateTopicMapping("topic-1", ["chapter-2"]);
    oldLoad.reject(new Error("old failure"));
    await expect(loading).rejects.toThrow("操作失败，请稍后重试");

    const state = useWorkbenchStore.getState();
    expect(state.topicsByCourse["course-1"][0].chapter_ids).toEqual(["chapter-2"]);
    expect(state.topicActions[actionKey("updateTopicMapping", "topic-1")]).toEqual({
      loading: false,
      error: null,
    });
  });

  it("旧请求失败不清除同资源较新 action 的 loading 或 error", async () => {
    const oldLoad = deferred<CourseTopic[]>();
    const generating = deferred<CourseTopic[]>();
    mocked.listTopics.mockReturnValueOnce(oldLoad.promise);
    mocked.generateTopics.mockReturnValueOnce(generating.promise);

    const loading = useWorkbenchStore.getState().loadTopics("course-1");
    const generation = useWorkbenchStore.getState().generateTopics("course-1");
    oldLoad.reject(new Error("old failure"));
    await expect(loading).rejects.toThrow("操作失败，请稍后重试");

    expect(useWorkbenchStore.getState().topicActions[actionKey("generateTopics", "course-1")]).toEqual({
      loading: true,
      error: null,
    });

    generating.resolve([topic({ title: "新主题" })]);
    await generation;
  });

  it("删除完成后删除前发起的 blocks 加载不能恢复旧缓存", async () => {
    const oldBlocks = deferred<TopicNoteBlock[]>();
    mocked.listTopicNoteBlocks.mockReturnValueOnce(oldBlocks.promise);
    mocked.deleteTopic.mockResolvedValueOnce(undefined);
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic()] } });

    const loading = useWorkbenchStore.getState().loadTopicBlocks("topic-1");
    await useWorkbenchStore.getState().deleteTopic("course-1", "topic-1");
    oldBlocks.resolve([block]);
    await loading;

    expect(useWorkbenchStore.getState().topicBlocksById["topic-1"]).toBeUndefined();
  });

  it("delete pending 后发 run 失败也不能阻止 204 最终清理", async () => {
    const deleting = deferred<void>();
    const laterRun = deferred<CourseTopic>();
    mocked.deleteTopic.mockReturnValueOnce(deleting.promise);
    mocked.runTopic.mockReturnValueOnce(laterRun.promise);
    useWorkbenchStore.setState({
      topicsByCourse: { "course-1": [topic()] },
      topicBlocksById: { "topic-1": [block] },
      topicCardsById: { "topic-1": [card] },
      topicRunsById: { "topic-1": [run] },
    });

    const deletion = useWorkbenchStore.getState().deleteTopic("course-1", "topic-1");
    const running = useWorkbenchStore.getState().runTopic("topic-1");
    deleting.resolve();
    await deletion;
    laterRun.reject(new Error("topic no longer exists"));
    await expect(running).rejects.toThrow("操作失败，请稍后重试");

    const state = useWorkbenchStore.getState();
    expect(state.topicsByCourse["course-1"]).toEqual([]);
    expect(state.topicBlocksById["topic-1"]).toBeUndefined();
    expect(state.topicCardsById["topic-1"]).toBeUndefined();
    expect(state.topicRunsById["topic-1"]).toBeUndefined();
  });

  it("delete 204 后晚到的 recover 成功响应不能复活主题", async () => {
    const deleting = deferred<void>();
    const laterRecover = deferred<CourseTopic>();
    mocked.deleteTopic.mockReturnValueOnce(deleting.promise);
    mocked.recoverTopic.mockReturnValueOnce(laterRecover.promise);
    useWorkbenchStore.setState({ topicsByCourse: { "course-1": [topic()] } });

    const deletion = useWorkbenchStore.getState().deleteTopic("course-1", "topic-1");
    const recovering = useWorkbenchStore.getState().recoverTopic("topic-1");
    deleting.resolve();
    await deletion;
    laterRecover.resolve(topic({ status: "FAILED" }));
    await recovering;

    expect(useWorkbenchStore.getState().topicsByCourse["course-1"]).toEqual([]);
  });
});
