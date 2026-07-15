import { beforeEach, describe, expect, it, vi } from "vitest";

describe("OCR status publication gate", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it("normalizes an invalid completed payload to blocked before the UI sees it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: "completed",
        source_path: "/tmp/book.pdf",
        state_path: "/tmp/state/batch-state.json",
        error: null,
        publishable: false,
        markdown_path: null,
        chapter_tree_path: null,
      }),
    }));

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).resolves.toMatchObject({
      status: "blocked",
      publishable: false,
    });
  });
});
