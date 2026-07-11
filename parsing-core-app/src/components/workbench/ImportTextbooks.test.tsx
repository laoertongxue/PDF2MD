import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as workbenchApi from "../../api/workbench";
import ImportTextbooks from "./ImportTextbooks";

const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

let dragDropHandler: ((event: { payload: { type: string; paths?: string[] } }) => void) | undefined;
const unlistenDragDrop = vi.fn();
const onDragDropEvent = vi.fn(async (handler) => {
  dragDropHandler = handler;
  return unlistenDragDrop;
});
vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({ onDragDropEvent }),
}));

const importSources = vi.fn();
const detectChapters = vi.fn();
const loadSources = vi.fn();

function renderImporter(courseId = "course-1", currentSources = []) {
  return render(
    <ImportTextbooks
      courseId={courseId}
      currentSources={currentSources}
      importSources={importSources}
      detectChapters={detectChapters}
      loadSources={loadSources}
    />,
  );
}

describe("ImportTextbooks", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    dragDropHandler = undefined;
    loadSources.mockResolvedValue([]);
    detectChapters.mockResolvedValue([]);
  });

  it("registers Tauri v2 drag-drop only in desktop, handles hover/drop, and unlistens on unmount", async () => {
    Object.defineProperty(globalThis, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    invoke.mockImplementation(async (command) => command === "textbook_path_is_file");
    const view = renderImporter();
    await waitFor(() => expect(onDragDropEvent).toHaveBeenCalledTimes(1));
    const zone = screen.getByTestId("textbook-drop-zone");

    dragDropHandler?.({ payload: { type: "enter", paths: ["/books/A.pdf"] } });
    await waitFor(() => expect(zone).toHaveAttribute("data-drag-active", "true"));
    dragDropHandler?.({ payload: { type: "drop", paths: ["/books/A.pdf", "/books/folder", "/books/readme.txt"] } });
    await waitFor(() => expect(screen.getByText("A.pdf")).toBeInTheDocument());
    expect(zone).toHaveAttribute("data-drag-active", "false");
    expect(screen.getByText("folder：不支持此文件类型")).toBeInTheDocument();
    expect(screen.getByText("readme.txt：不支持此文件类型")).toBeInTheDocument();

    view.unmount();
    await waitFor(() => expect(unlistenDragDrop).toHaveBeenCalledTimes(1));
    delete (globalThis as typeof globalThis & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("does not register Tauri drag-drop in a normal browser", async () => {
    renderImporter();
    await Promise.resolve();
    expect(onDragDropEvent).not.toHaveBeenCalled();
  });

  it("calls the import endpoint with paths and rejects malformed responses safely", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ source_id: "source-a", title: "战略管理", stored_path: "/course/战略管理.pdf" }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ source_id: 1, title: "bad", stored_path: "/bad.pdf" }] }), { status: 200 }));

    await expect(workbenchApi.importSources("course-1", ["/books/战略管理.pdf"], ["战略管理（第 5 版）"])).resolves.toEqual([
      { source_id: "source-a", title: "战略管理", stored_path: "/course/战略管理.pdf" },
    ]);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8000/api/workbench/courses/course-1/sources/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: ["/books/战略管理.pdf"], titles: ["战略管理（第 5 版）"] }),
    });
    await expect(workbenchApi.importSources("course-1", ["/books/bad.pdf"])).rejects.toThrow("服务返回数据格式异常，请稍后重试");
  });

  it("prefills an editable textbook title and sends the edited title with its path", async () => {
    invoke.mockResolvedValue(["/books/战略管理.pdf"]);
    importSources.mockResolvedValue([{ source_id: "source-a", title: "战略管理（MBA版）", stored_path: "/course/战略管理.pdf" }]);
    renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    const titleInput = screen.getByRole("textbox", { name: "教材名称" });
    expect(titleInput).toHaveValue("战略管理");
    await userEvent.clear(titleInput);
    await userEvent.type(titleInput, "  战略管理（MBA版）  ");
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));

    await waitFor(() => expect(importSources).toHaveBeenCalledWith(
      "course-1",
      ["/books/战略管理.pdf"],
      ["战略管理（MBA版）"],
    ));
    expect(titleInput).toBeDisabled();
  });

  it("blocks an empty textbook title with a Chinese error", async () => {
    invoke.mockResolvedValue(["/books/A.pdf"]);
    renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.clear(screen.getByRole("textbox", { name: "教材名称" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));

    expect(screen.getByText("教材名称不能为空")).toBeInTheDocument();
    expect(importSources).not.toHaveBeenCalled();
  });

  it("imports two native absolute paths independently before detecting each unique source id", async () => {
    const sequence: string[] = [];
    invoke.mockResolvedValue(["/books/战略管理.pdf", "/books/组织行为.docx"]);
    importSources
      .mockImplementationOnce(async (_courseId, paths) => {
        sequence.push(`import:${paths[0]}`);
        return [{ source_id: "source-a", title: "战略管理", stored_path: "/course/战略管理.pdf" }];
      })
      .mockImplementationOnce(async (_courseId, paths) => {
        sequence.push(`import:${paths[0]}`);
        return [{ source_id: "source-b", title: "组织行为", stored_path: "/course/组织行为.docx" }];
      });
    detectChapters.mockImplementation(async (sourceId) => {
      sequence.push(`detect:${sourceId}`);
      return [];
    });
    renderImporter();

    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));

    await waitFor(() => expect(screen.getAllByText("成功")).toHaveLength(2));
    expect(importSources).toHaveBeenNthCalledWith(1, "course-1", ["/books/战略管理.pdf"], ["战略管理"]);
    expect(importSources).toHaveBeenNthCalledWith(2, "course-1", ["/books/组织行为.docx"], ["组织行为"]);
    expect(sequence).toEqual([
      "import:/books/战略管理.pdf",
      "detect:source-a",
      "import:/books/组织行为.docx",
      "detect:source-b",
    ]);
    expect(loadSources).toHaveBeenCalledWith("course-1");
  });

  it("shows a browser path error per file and sends no request for standard File objects", async () => {
    invoke.mockRejectedValue(new Error("not running in Tauri"));
    const { container } = renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toHaveAttribute("multiple");

    await userEvent.upload(input, new File(["book"], "营销管理.pdf", { type: "application/pdf" }));

    expect(screen.getByText("营销管理.pdf")).toBeInTheDocument();
    expect(screen.getByText("浏览器无法读取本地路径，请使用桌面客户端选择教材")).toBeInTheDocument();
    expect(importSources).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "导入全部" })).not.toBeInTheDocument();
  });

  it("accepts a compatible File carrying an absolute path and uses the import API", async () => {
    invoke.mockRejectedValue(new Error("not running in Tauri"));
    importSources.mockResolvedValue([{ source_id: "source-webview", title: "营销管理", stored_path: "/course/营销管理.pdf" }]);
    const { container } = renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    const file = new File(["book"], "营销管理.pdf", { type: "application/pdf" }) as File & { path: string };
    Object.defineProperty(file, "path", { value: "/books/营销管理.pdf" });
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));

    await waitFor(() => expect(importSources).toHaveBeenCalledWith("course-1", ["/books/营销管理.pdf"], ["营销管理"]));
    expect(detectChapters).toHaveBeenCalledWith("source-webview");
  });

  it("accepts dropped documents and rejects unsupported files and directories item by item", () => {
    renderImporter();
    const zone = screen.getByTestId("textbook-drop-zone");
    const pdf = Object.assign(new File(["pdf"], "财务管理.pdf", { type: "application/pdf" }), { path: "/books/财务管理.pdf" });
    const txt = Object.assign(new File(["txt"], "说明.txt", { type: "text/plain" }), { path: "/books/说明.txt" });
    const directory = new File([], "课程目录", { type: "" });
    fireEvent.drop(zone, { dataTransfer: { files: [pdf, txt, directory], items: [
      { kind: "file", getAsFile: () => pdf, webkitGetAsEntry: () => ({ isDirectory: false }) },
      { kind: "file", getAsFile: () => txt, webkitGetAsEntry: () => ({ isDirectory: false }) },
      { kind: "file", getAsFile: () => directory, webkitGetAsEntry: () => ({ isDirectory: true }) },
    ] } });
    expect(screen.getByText("财务管理.pdf")).toBeInTheDocument();
    expect(screen.getByText("说明.txt：不支持此文件类型")).toBeInTheDocument();
    expect(screen.getByText("课程目录：不支持导入文件夹")).toBeInTheDocument();
  });

  it("retries chapter detection from the saved source id without importing again", async () => {
    invoke.mockResolvedValue(["/books/A.pdf"]);
    importSources.mockResolvedValue([{ source_id: "source-a", title: "A", stored_path: "/course/A.pdf" }]);
    detectChapters.mockRejectedValueOnce(new Error("章节识别失败")).mockResolvedValueOnce([]);
    renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));
    await waitFor(() => expect(screen.getByText("章节识别失败")).toBeInTheDocument());
    const row = screen.getByText("A.pdf").closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: "重试 A.pdf" }));

    await waitFor(() => expect(within(row).getByText("成功")).toBeInTheDocument());
    expect(importSources).toHaveBeenCalledTimes(1);
    expect(detectChapters).toHaveBeenCalledTimes(2);
    expect(detectChapters).toHaveBeenNthCalledWith(2, "source-a");
  });

  it("isolates pending work when courseId changes and ignores the old response", async () => {
    let resolveImport!: (items: Array<{ source_id: string; title: string; stored_path: string }>) => void;
    invoke.mockResolvedValue(["/books/A.pdf"]);
    importSources.mockImplementation(() => new Promise((resolve) => { resolveImport = resolve; }));
    const view = renderImporter("course-old");
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));
    view.rerender(
      <ImportTextbooks courseId="course-new" currentSources={[]} importSources={importSources} detectChapters={detectChapters} loadSources={loadSources} />,
    );
    expect(screen.queryByText("A.pdf")).not.toBeInTheDocument();

    resolveImport([{ source_id: "source-old", title: "A", stored_path: "/course-old/A.pdf" }]);
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText("A.pdf")).not.toBeInTheDocument();
    expect(detectChapters).not.toHaveBeenCalled();
    expect(loadSources).not.toHaveBeenCalledWith("course-new");
  });

  it("reconciles an uncertain committed import and continues detection without importing twice", async () => {
    const source = { id: "source-a", course_id: "course-1", kind: "main", file_path: "/course/A.pdf", title: "A", status: "IMPORTED" };
    invoke.mockResolvedValue(["/books/A.pdf"]);
    loadSources.mockResolvedValueOnce([]).mockResolvedValueOnce([source]);
    importSources.mockRejectedValueOnce(new workbenchApi.SafeApiError("network"));
    renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));

    await waitFor(() => expect(screen.getByText("成功")).toBeInTheDocument());
    expect(importSources).toHaveBeenCalledTimes(1);
    expect(detectChapters).toHaveBeenCalledWith("source-a");
  });

  it("locks an ambiguous uncertain result and does not offer blind retry", async () => {
    const source = (id: string) => ({ id, course_id: "course-1", kind: "main", file_path: `/course/${id}/A.pdf`, title: "A", status: "IMPORTED" });
    invoke.mockResolvedValue(["/books/A.pdf"]);
    loadSources.mockResolvedValueOnce([]).mockResolvedValueOnce([source("source-a"), source("source-b")]);
    importSources.mockRejectedValueOnce(new workbenchApi.SafeApiError("protocol"));
    renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));

    await waitFor(() => expect(screen.getByText("结果待确认")).toBeInTheDocument());
    expect(screen.getByText("请刷新教材列表核对导入结果，系统不会自动重复导入")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试 A.pdf" })).not.toBeInTheDocument();
    expect(detectChapters).not.toHaveBeenCalled();
  });

  it("allows retry after an explicit atomic 400 failure", async () => {
    invoke.mockResolvedValue(["/books/A.pdf"]);
    loadSources.mockResolvedValue([]);
    importSources
      .mockRejectedValueOnce(new workbenchApi.SafeApiError("invalid_request"))
      .mockResolvedValueOnce([{ source_id: "source-a", title: "A", stored_path: "/course/A.pdf" }]);
    renderImporter();
    await userEvent.click(screen.getByRole("button", { name: "选择教材" }));
    await userEvent.click(screen.getByRole("button", { name: "导入全部" }));
    const row = await screen.findByText("A.pdf").then((node) => node.closest("li")!);
    await waitFor(() => expect(within(row).getByRole("button", { name: "重试 A.pdf" })).toBeInTheDocument());
    await userEvent.click(within(row).getByRole("button", { name: "重试 A.pdf" }));

    await waitFor(() => expect(within(row).getByText("成功")).toBeInTheDocument());
    expect(importSources).toHaveBeenCalledTimes(2);
  });
});
