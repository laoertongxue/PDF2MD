import { beforeEach, describe, expect, it, vi } from "vitest";

describe("OCR status publication gate", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it.each(["idle", "running", "completed", "review_required", "blocked", "failed", "cancelled"])(
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
            review_pages: null,
            review_pending: 0,
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
          review_pages: null,
          review_pending: 0,
        }),
      }),
    );

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).rejects.toMatchObject({
      name: "SafeApiError",
      category: "protocol",
    });
  });

  it("accepts review_required with a review page list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
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
        }),
      }),
    );

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).resolves.toMatchObject({
      status: "review_required",
      review_pending: 2,
    });
  });
});
