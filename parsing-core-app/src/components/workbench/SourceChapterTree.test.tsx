import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import SourceChapterTree, { type SourceChapterGroup } from "./SourceChapterTree";

const groups: SourceChapterGroup[] = [
  {
    source: { id: "source-a", course_id: "course-1", kind: "main", file_path: "/a.pdf", title: "战略管理", status: "READY" },
    chapters: [
      { id: "chapter-a", source_id: "source-a", course_id: "course-1", seq: 0, title: "第1章", status: "COMPLETED" },
    ],
    completedCount: 1,
  },
  {
    source: { id: "source-b", course_id: "course-1", kind: "main", file_path: "/b.pdf", title: "组织行为", status: "READY" },
    chapters: [
      { id: "chapter-b", source_id: "source-b", course_id: "course-1", seq: 0, title: "第1章", status: "CONFIRMED" },
      { id: "chapter-c", source_id: "source-b", course_id: "course-1", seq: 1, title: "激励", status: "PENDING" },
    ],
    completedCount: 1,
  },
];

describe("SourceChapterTree", () => {
  afterEach(cleanup);
  it("keeps same-name chapters under their source groups and selects by unique chapter id", async () => {
    const onSelect = vi.fn();
    render(<SourceChapterTree groups={groups} onSelectChapter={onSelect} />);
    expect(screen.getByText("战略管理")).toBeInTheDocument();
    expect(screen.getByText("组织行为")).toBeInTheDocument();
    expect(screen.getByText("1/1")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
    const sameChapters = screen.getAllByRole("button", { name: /《.*》 \/ 第1章 \/ 第1章/ });
    expect(sameChapters).toHaveLength(2);
    await userEvent.click(sameChapters[1]);
    expect(onSelect).toHaveBeenCalledWith("chapter-b");
  });

  it("collapses one source without hiding chapters in another source", async () => {
    render(<SourceChapterTree groups={groups} />);
    const firstGroup = screen.getByTestId("source-group-source-a");
    await userEvent.click(within(firstGroup).getByRole("button", { name: "折叠《战略管理》" }));
    expect(within(firstGroup).queryByText("第1章")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("source-group-source-b")).getByText("第1章")).toBeInTheDocument();
    expect(within(firstGroup).getByRole("button", { name: "展开《战略管理》" })).toHaveAttribute("aria-expanded", "false");
  });
});
