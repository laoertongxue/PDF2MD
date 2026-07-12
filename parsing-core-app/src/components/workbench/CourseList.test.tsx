import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import CourseList from "./CourseList";

const loadCourses = vi.fn().mockResolvedValue(undefined);
vi.mock("../../store/useWorkbenchStore", () => ({
  useWorkbenchStore: () => ({
    courses: [], createCourse: vi.fn(), loadCourseCards: vi.fn(), loadCourses,
    loadSources: vi.fn(), selectCourse: vi.fn(), selectedCourseId: null,
  }),
}));

beforeEach(() => {
  delete (globalThis as typeof globalThis & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

it("disables the desktop folder picker in a browser and explains the limitation as an alert", async () => {
  render(<MemoryRouter><CourseList /></MemoryRouter>);
  const picker = screen.getByRole("button", { name: "选择文件夹" });
  expect(picker).toBeDisabled();
  expect(screen.getByText("浏览器版不支持选择本地课程目录，请使用桌面客户端或粘贴目录路径。")).toBeInTheDocument();
  await userEvent.click(picker);
  expect(screen.queryByText(/invoke|Tauri/i)).not.toBeInTheDocument();
});
