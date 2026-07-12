import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { buildSearchResults, ServiceStatusView } from "./Layout";

it("shows an actionable sidecar failure and retries", () => {
  const retry = vi.fn();
  render(
    <ServiceStatusView
      service={{
        state: "failed",
        port: 43127,
        error: { category: "startup", message: "Python runtime missing" },
        logPath: "/tmp/sidecar.log",
      }}
      onRetry={retry}
    />,
  );

  expect(screen.getByText("服务启动失败")).toBeInTheDocument();
  expect(screen.getByText("Python runtime missing")).toBeInTheDocument();
  expect(screen.getByText("日志：/tmp/sidecar.log")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试启动" }));
  expect(retry).toHaveBeenCalledOnce();
});

it("builds keyboard-searchable destinations for courses, textbooks, chapters and cards", () => {
  const results = buildSearchResults({
    courses: [{ id: "c1", title: "战略管理", description: "MBA", root_dir: "/mba" }],
    sources: { c1: [{ id: "s1", course_id: "c1", kind: "main", file_path: "/mba/a.pdf", title: "竞争战略教材", status: "READY" }] },
    chapters: { s1: [{ id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "行业结构", status: "COMPLETED" }] },
    cardsByCourse: { c1: [{ id: "card1", origin_type: "chapter", origin_id: "ch1", origin_title: "行业结构", card_type: "观点", title: "五力模型", content: "竞争分析", source_refs: [], tags: [], status: "ACTIVE", favorite: false, updated_at: 1 }] },
  }, "战略");
  expect(results.map((result) => result.label)).toEqual(["战略管理", "竞争战略教材"]);
  expect(buildSearchResults({ courses: [], sources: {}, chapters: { s1: [{ id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "行业结构", status: "COMPLETED" }] }, cardsByCourse: {} }, "行业")[0].to).toBe("/workbench/chapter?chapterId=ch1");
  expect(buildSearchResults({ courses: [], sources: {}, chapters: {}, cardsByCourse: { c1: [{ id: "card1", origin_type: "chapter", origin_id: "ch1", origin_title: "行业结构", card_type: "观点", title: "五力模型", content: "竞争分析", source_refs: [], tags: [], status: "ACTIVE", favorite: false, updated_at: 1 }] } }, "五力")[0].to).toBe("/workbench/cards?cardId=card1");
});
