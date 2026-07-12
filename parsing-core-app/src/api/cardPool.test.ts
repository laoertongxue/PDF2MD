import { beforeEach, describe, expect, it, vi } from "vitest";

const card = { id: "c1", origin_type: "chapter", origin_id: "ch1", origin_title: "竞争战略", card_type: "viewpoint", title: "定位", content: "定位是选择", source_refs: ["ch1"], tags: ["战略"], status: "ACTIVE", favorite: false, updated_at: 3 };

describe("card pool API contract", () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.resetModules(); });

  it("validates card metadata and sends CAS edits", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => card }));
    const api = await import("./workbench");
    await expect(api.updateCourseCard("c1", { title: "定位", content: "定位是选择", tags: ["战略"], status: "ACTIVE", expected_updated_at: 2 })).resolves.toEqual(card);
    expect(fetch).toHaveBeenCalledWith("http://127.0.0.1:8000/api/workbench/cards/c1", expect.objectContaining({ method: "PATCH" }));
  });

  it("sends favorite changes with the current version", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ...card, favorite: true, updated_at: 4 }) }));
    const api = await import("./workbench");
    await api.setCourseCardFavorite("c1", true, 3);
    expect(fetch).toHaveBeenCalledWith("http://127.0.0.1:8000/api/workbench/cards/c1/favorite", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ favorite: true, expected_updated_at: 3 }) }));
  });
});
