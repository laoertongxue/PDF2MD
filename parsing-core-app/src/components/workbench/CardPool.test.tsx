import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import CardPool from "./CardPool";

const actions = { loadCourses: vi.fn(), loadCourseCards: vi.fn() };
let state: Record<string, unknown>;
vi.mock("../../store/useWorkbenchStore", () => ({ useWorkbenchStore: () => state }));

const cards = [
  { id: "c1", origin_type: "chapter", origin_id: "ch1", origin_title: "竞争战略", card_type: "观点", title: "定位与取舍", content: "战略意味着放弃", source_refs: ["ch1"], tags: ["战略"], status: "ACTIVE", favorite: true, updated_at: 3 },
  { id: "c2", origin_type: "topic", origin_id: "t1", origin_title: "增长主题", card_type: "案例", title: "增长飞轮", content: "复利增长", source_refs: ["ch2"], tags: ["增长"], status: "ARCHIVED", favorite: false, updated_at: 2 },
];

describe("CardPool", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    actions.loadCourses.mockResolvedValue(undefined); actions.loadCourseCards.mockResolvedValue(cards);
    state = { selectedCourseId: "course", cardsByCourse: { course: cards }, ...actions };
  });
  afterEach(cleanup);

  it("searches and combines source, tag and favorite filters", async () => {
    render(<MemoryRouter><CardPool /></MemoryRouter>);
    await userEvent.type(screen.getByRole("searchbox", { name: "搜索课程卡片" }), "增长");
    expect(screen.queryByText("定位与取舍")).not.toBeInTheDocument();
    expect(screen.getByText("增长飞轮")).toBeInTheDocument();
    await userEvent.clear(screen.getByRole("searchbox", { name: "搜索课程卡片" }));
    await userEvent.click(screen.getByRole("button", { name: "仅看收藏" }));
    expect(screen.getByText("定位与取舍")).toBeInTheDocument();
    expect(screen.queryByText("增长飞轮")).not.toBeInTheDocument();
  });

  it("distinguishes no cards from no filter results", async () => {
    const { rerender } = render(<MemoryRouter><CardPool /></MemoryRouter>);
    await userEvent.type(screen.getByRole("searchbox", { name: "搜索课程卡片" }), "不存在");
    expect(screen.getByText("没有符合条件的卡片")).toBeInTheDocument();
    state = { ...state, cardsByCourse: { course: [] } };
    rerender(<MemoryRouter><CardPool /></MemoryRouter>);
    expect(screen.getByText("本课程还没有卡片")).toBeInTheDocument();
  });

  it("focuses, scrolls and highlights a card from the real cardId route", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<MemoryRouter initialEntries={["/workbench/cards?cardId=c2"]}><Routes><Route path="/workbench/cards" element={<CardPool />} /></Routes></MemoryRouter>);

    const target = await screen.findByRole("article", { name: "卡片：增长飞轮" });
    expect(target).toHaveFocus();
    expect(target).toHaveAttribute("data-highlighted", "true");
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(screen.getByRole("link", { name: "增长主题" })).toHaveAttribute("href", "/workbench/courses/course/fusion/t1");
  });

  it("clears filters that hide a route target and announces the adjustment", async () => {
    function RouteTarget() {
      const navigate = useNavigate();
      return <><button onClick={() => navigate("/workbench/cards?cardId=c2")}>定位增长卡片</button><CardPool /></>;
    }
    render(<MemoryRouter initialEntries={["/workbench/cards"]}><Routes><Route path="/workbench/cards" element={<RouteTarget />} /></Routes></MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "仅看收藏" }));
    expect(screen.queryByText("增长飞轮")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "定位增长卡片" }));

    expect(await screen.findByRole("article", { name: "卡片：增长飞轮" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("已调整筛选并定位到“增长飞轮”");
    expect(screen.getByRole("link", { name: "竞争战略" })).toHaveAttribute("href", "/workbench/chapter?chapterId=ch1");
  });

  it("announces a missing route target without moving focus", async () => {
    render(<MemoryRouter initialEntries={["/workbench/cards?cardId=missing"]}><Routes><Route path="/workbench/cards" element={<CardPool />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("status")).toHaveTextContent("未找到指定卡片");
    expect(screen.queryByRole("article", { name: /missing/ })).not.toBeInTheDocument();
  });

  it("asks for a course before resolving a card route", async () => {
    state = { ...state, selectedCourseId: null, cardsByCourse: {} };
    render(<MemoryRouter initialEntries={["/workbench/cards?cardId=c2"]}><Routes><Route path="/workbench/cards" element={<CardPool />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("status")).toHaveTextContent("请先选择课程");
  });
});
