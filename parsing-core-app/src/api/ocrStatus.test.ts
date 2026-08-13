import { beforeEach, describe, expect, it, vi } from "vitest";

describe("OCR status publication gate", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it.each(["idle", "running", "completed", "blocked", "failed", "cancelled"])(
    "preserves the backend %s status after protocol validation",
    async (status) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          status: 200,
          json: async () => ({
            status,
            source_path: "/tmp/book.pdf",
            state_path: "/tmp/state/batch-state.json",
            error: null,
            publishable: false,
            markdown_path: null,
            chapter_tree_path: null,
          }),
        }),
      );

      const { getSourceOcrStatus } = await import("./workbench");
      await expect(getSourceOcrStatus("source-1")).resolves.toMatchObject({
        status,
        publishable: false,
      });
    },
  );

  it("rejects an unknown backend status as a protocol error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          status: "future-status",
          source_path: "/tmp/book.pdf",
          state_path: "/tmp/state/batch-state.json",
          error: null,
          publishable: false,
          markdown_path: null,
          chapter_tree_path: null,
        }),
      }),
    );

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).rejects.toMatchObject({
      name: "SafeApiError",
      category: "protocol",
    });
  });
});
