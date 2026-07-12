import { afterEach, expect, it, vi } from "vitest";
import { confirmChapterDrafts, getChapterDrafts, replaceChapterDrafts } from "./workbench";

afterEach(() => vi.restoreAllMocks());

const state = { chapters: [{ id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "战略", status: "DRAFT", start: 0, end: 100 }], fingerprint: "fp-1" };

it("reads, replaces and confirms chapter drafts using the backend snapshot contract", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(state), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...state, fingerprint: "fp-2" }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ chapters: state.chapters.map((chapter) => ({ ...chapter, status: "CONFIRMED" })), fingerprint: "fp-3" }), { status: 200 }));

  await expect(getChapterDrafts("s1")).resolves.toEqual(state);
  await expect(replaceChapterDrafts("s1", "fp-1", [{ id: "ch1", title: "战略管理", start: 0, end: 100 }])).resolves.toMatchObject({ fingerprint: "fp-2" });
  await expect(confirmChapterDrafts("s1", "fp-2")).resolves.toMatchObject({ fingerprint: "fp-3" });

  expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8000/api/workbench/sources/s1/chapter-drafts", undefined);
  expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8000/api/workbench/sources/s1/chapter-drafts", expect.objectContaining({ method: "PUT", body: JSON.stringify({ expected_fingerprint: "fp-1", chapters: [{ id: "ch1", title: "战略管理", start: 0, end: 100 }] }) }));
  expect(fetchMock).toHaveBeenNthCalledWith(3, "http://127.0.0.1:8000/api/workbench/sources/s1/chapter-drafts/confirm", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_fingerprint: "fp-2" }) }));
});

it("rejects malformed chapter boundaries from the service", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({ chapters: [{ ...state.chapters[0], start: 100, end: 10 }], fingerprint: "fp" }), { status: 200 }));
  await expect(getChapterDrafts("s1")).rejects.toThrow("服务返回数据格式异常");
});
