import { beforeEach, describe, expect, it, vi } from "vitest";

function stubReviewStatusFetch(review_pages: unknown, review_pending: unknown): void {
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
        review_pages,
        review_pending,
      }),
    }),
  );
}

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
      review_pages: [
        { page: 3, reason: "conflict", alignment_status: "conflict" },
        { page: 7, reason: "sampled", alignment_status: "consistent" },
      ],
      review_pending: 2,
    });
  });

  it.each([0, 1.5])("rejects a review page with an invalid page number (%s)", async (page) => {
    stubReviewStatusFetch([{ page, reason: "conflict", alignment_status: "conflict" }], 1);

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).rejects.toMatchObject({
      name: "SafeApiError",
      category: "protocol",
    });
  });

  it("rejects a review page with an unknown reason", async () => {
    stubReviewStatusFetch([{ page: 3, reason: "unknown", alignment_status: "conflict" }], 1);

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).rejects.toMatchObject({
      name: "SafeApiError",
      category: "protocol",
    });
  });

  it.each([-1, 1.5])("rejects an invalid review_pending count (%s)", async (review_pending) => {
    stubReviewStatusFetch(null, review_pending);

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).rejects.toMatchObject({
      name: "SafeApiError",
      category: "protocol",
    });
  });
});
