import { beforeEach, describe, expect, it, vi } from "vitest";

describe("chapter workbench API contract", () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.resetModules(); });

  it("loads chapter runs and validates the response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [{ id: "r1", chapter_id: "ch1", round_key: "review", executor: "codex", status: "FAILED", output: "", error: "审核失败", stale: false, created_at: 1, updated_at: 2 }] }));
    const api = await import("./workbench");
    await expect(api.listChapterRuns("ch1")).resolves.toHaveLength(1);
    expect(fetch).toHaveBeenCalledWith("http://127.0.0.1:8000/api/workbench/chapters/ch1/runs", undefined);
  });

  it("patches a Mermaid block with optimistic concurrency", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: "b1", chapter_id: "ch1", kind: "knowledge_mermaid", title: "知识结构图", body: "flowchart LR\nX-->Y", seq: 5, updated_at: 2 }) }));
    const api = await import("./workbench");
    await api.saveChapterBlock("ch1", "knowledge_mermaid", "flowchart LR\nX-->Y", "flowchart LR\nA-->B");
    expect(fetch).toHaveBeenCalledWith("http://127.0.0.1:8000/api/workbench/chapters/ch1/note-blocks/knowledge_mermaid", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ body: "flowchart LR\nX-->Y", expected_body: "flowchart LR\nA-->B" }) }));
  });
});
