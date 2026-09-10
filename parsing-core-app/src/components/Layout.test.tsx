import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { requireAt } from "../test/requireValue";
import { buildSearchResults } from "./layoutSearch";
import CourseList from "./workbench/CourseList";
import Layout, { ServiceStatusView } from "./Layout";

const mocks = vi.hoisted(() => ({
  getServiceStatus: vi.fn(),
  requestForceExitConfirmation: vi.fn(),
  retryExitCleanup: vi.fn(),
  retryService: vi.fn(),
  loadChapters: vi.fn(),
  loadCourseCards: vi.fn(),
  loadCourses: vi.fn(),
  loadSources: vi.fn(),
  selectCourse: vi.fn(),
  selectedCourseId: null as string | null,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

vi.mock("../api/runtime", () => ({
  getServiceStatus: mocks.getServiceStatus,
  requestForceExitConfirmation: mocks.requestForceExitConfirmation,
  retryExitCleanup: mocks.retryExitCleanup,
  retryService: mocks.retryService,
  isTauriRuntime: () => false,
}));

vi.mock("../store/useWorkbenchStore", () => ({
  useWorkbenchStore: () => ({
    chapters: {},
    cardsByCourse: {},
    courses: [],
    loadChapters: mocks.loadChapters,
    loadCourseCards: mocks.loadCourseCards,
    loadCourses: mocks.loadCourses,
    loadSources: mocks.loadSources,
    selectCourse: mocks.selectCourse,
    selectedCourseId: mocks.selectedCourseId,
    sources: {},
    createCourse: vi.fn(),
  }),
}));

beforeEach(() => {
  mocks.getServiceStatus.mockReset();
  mocks.requestForceExitConfirmation.mockReset().mockResolvedValue("force_exit_cancelled");
  mocks.retryExitCleanup.mockReset().mockResolvedValue("exit_requested");
  mocks.retryService.mockReset().mockResolvedValue(undefined);
  mocks.loadChapters.mockReset().mockResolvedValue([]);
  mocks.loadCourseCards.mockReset().mockResolvedValue([]);
  mocks.loadCourses.mockReset().mockResolvedValue(undefined);
  mocks.loadSources.mockReset().mockResolvedValue([]);
  mocks.selectCourse.mockReset();
  mocks.selectedCourseId = null;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

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

it("shows shutdown recovery controls only when force exit is available", () => {
  const retry = vi.fn();
  const retryCleanup = vi.fn();
  const forceQuit = vi.fn();
  const { rerender } = render(
    <ServiceStatusView
      service={{
        state: "failed",
        port: 43127,
        forceExitAvailable: true,
        error: { category: "shutdown", message: "sidecar cleanup failed" },
      }}
      onRetry={retry}
      onRetryExitCleanup={retryCleanup}
      onForceQuit={forceQuit}
    />,
  );

  expect(screen.getByText("退出清理失败 / Shutdown Cleanup Failed")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "再次清理并退出 / Retry Cleanup & Exit" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "强制退出 / Force Quit" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试启动" })).not.toBeInTheDocument();

  rerender(
    <ServiceStatusView
      service={{
        state: "failed",
        port: 43127,
        error: { category: "startup", message: "backend exited" },
      }}
      onRetry={retry}
      onRetryExitCleanup={retryCleanup}
      onForceQuit={forceQuit}
    />,
  );

  expect(screen.getByRole("button", { name: "重试启动" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Retry Cleanup & Exit/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Force Quit/ })).not.toBeInTheDocument();
});

it("keeps exit cleanup single-flight, blocks force quit, and allows retry after rejection", async () => {
  const cleanupAttempt = deferred<string>();
  mocks.getServiceStatus.mockResolvedValue({
    state: "failed",
    port: 43127,
    forceExitAvailable: true,
    error: { category: "shutdown", message: "cleanup incomplete" },
  });
  mocks.retryExitCleanup.mockReturnValueOnce(cleanupAttempt.promise).mockResolvedValueOnce("exit_requested");

  render(
    <MemoryRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div>content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  const cleanupButton = await screen.findByRole("button", { name: "再次清理并退出 / Retry Cleanup & Exit" });
  fireEvent.click(cleanupButton);
  expect(mocks.retryExitCleanup).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "正在清理 / Cleaning Up" })).toBeDisabled();
  const forceButton = screen.getByRole("button", { name: "强制退出 / Force Quit" });
  expect(forceButton).toBeDisabled();
  fireEvent.click(forceButton);
  expect(mocks.requestForceExitConfirmation).not.toHaveBeenCalled();

  await act(async () => {
    cleanupAttempt.reject(new Error("cleanup command failed"));
    await Promise.resolve();
  });
  expect(screen.getByText("cleanup command failed")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "再次清理并退出 / Retry Cleanup & Exit" }));
  await waitFor(() => expect(mocks.retryExitCleanup).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("cleanup command failed")).not.toBeInTheDocument();
});

it("surfaces a synchronous cleanup error and permits a new cleanup attempt", async () => {
  mocks.getServiceStatus.mockResolvedValue({
    state: "failed",
    port: 43127,
    forceExitAvailable: true,
    error: { category: "shutdown", message: "cleanup incomplete" },
  });
  mocks.retryExitCleanup
    .mockImplementationOnce(() => {
      throw new Error("cleanup invocation failed synchronously");
    })
    .mockResolvedValueOnce("exit_requested");

  render(
    <MemoryRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div>content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "再次清理并退出 / Retry Cleanup & Exit" }));
  expect(screen.getByText("cleanup invocation failed synchronously")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "再次清理并退出 / Retry Cleanup & Exit" }));
  await waitFor(() => expect(mocks.retryExitCleanup).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("cleanup invocation failed synchronously")).not.toBeInTheDocument();
});

it("reports force-quit rejection but treats native cancellation as a non-error", async () => {
  mocks.getServiceStatus.mockResolvedValue({
    state: "failed",
    port: 43127,
    forceExitAvailable: true,
    error: { category: "shutdown", message: "cleanup incomplete" },
  });
  mocks.requestForceExitConfirmation
    .mockRejectedValueOnce(new Error("confirmation command failed"))
    .mockResolvedValueOnce("force_exit_cancelled");

  render(
    <MemoryRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div>content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "强制退出 / Force Quit" }));
  expect(screen.getByRole("button", { name: "等待确认 / Awaiting Confirmation" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "再次清理并退出 / Retry Cleanup & Exit" })).toBeDisabled();
  expect(await screen.findByText("confirmation command failed")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "强制退出 / Force Quit" }));
  await waitFor(() => expect(mocks.requestForceExitConfirmation).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("confirmation command failed")).not.toBeInTheDocument();
});

it.each([
  ["failed", { state: "failed" as const, port: 0, error: { category: "startup", message: "backend exited" } }],
  ["offline", { state: "offline" as const, port: 0, error: { category: "offline", message: "not reachable" } }],
])("exits the course skeleton while the service remains %s", async (_name, status) => {
  mocks.getServiceStatus.mockResolvedValue(status);

  render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<CourseList />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  expect(await screen.findByText("服务不可用，请先重试启动")).toBeInTheDocument();
  expect(screen.queryByTestId("course-list-skeleton")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试课程列表" })).not.toBeInTheDocument();
  expect(screen.queryByText("还没有课程")).not.toBeInTheDocument();
  expect(mocks.loadCourses).not.toHaveBeenCalled();
});

it("renders an identifiable course skeleton only while a running service is loading courses", async () => {
  let resolveLoad: (() => void) | undefined;
  mocks.getServiceStatus.mockResolvedValue({ state: "running", port: 43127 });
  mocks.loadCourses.mockReturnValue(
    new Promise<void>((resolve) => {
      resolveLoad = resolve;
    }),
  );

  render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<CourseList />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  expect(await screen.findByTestId("course-list-skeleton")).toBeInTheDocument();
  resolveLoad?.();
  await waitFor(() => expect(screen.queryByTestId("course-list-skeleton")).not.toBeInTheDocument());
});

it("starts one fresh course generation after service restart and ignores stale settlements", async () => {
  vi.useFakeTimers();
  mocks.selectedCourseId = "course-1";
  const oldCourses = deferred<void>();
  const oldSources = deferred<Array<{ id: string }>>();
  const oldCards = deferred<never[]>();
  const courseSignals: AbortSignal[] = [];
  const sourceSignals: AbortSignal[] = [];
  const cardSignals: AbortSignal[] = [];

  mocks.getServiceStatus
    .mockResolvedValueOnce({ state: "running", port: 43127 })
    .mockResolvedValueOnce({ state: "restarting", port: 43127 })
    .mockResolvedValue({ state: "running", port: 43128 });
  mocks.loadCourses
    .mockImplementationOnce((signal?: AbortSignal) => {
      if (signal) courseSignals.push(signal);
      return oldCourses.promise;
    })
    .mockImplementationOnce((signal?: AbortSignal) => {
      if (signal) courseSignals.push(signal);
      return Promise.resolve();
    });
  mocks.loadSources
    .mockImplementationOnce((_courseId: string, signal?: AbortSignal) => {
      if (signal) sourceSignals.push(signal);
      return oldSources.promise;
    })
    .mockImplementationOnce((_courseId: string, signal?: AbortSignal) => {
      if (signal) sourceSignals.push(signal);
      return Promise.resolve([{ id: "source-new" }]);
    });
  mocks.loadCourseCards
    .mockImplementationOnce((_courseId: string, signal?: AbortSignal) => {
      if (signal) cardSignals.push(signal);
      return oldCards.promise;
    })
    .mockImplementationOnce((_courseId: string, signal?: AbortSignal) => {
      if (signal) cardSignals.push(signal);
      return Promise.resolve([]);
    });

  render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<CourseList />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(mocks.loadCourses).toHaveBeenCalledOnce();
  expect(mocks.loadSources).toHaveBeenCalledOnce();
  expect(mocks.loadCourseCards).toHaveBeenCalledOnce();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_500);
  });
  expect(courseSignals[0]?.aborted).toBe(true);
  expect(sourceSignals[0]?.aborted).toBe(true);
  expect(cardSignals[0]?.aborted).toBe(true);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_500);
    await Promise.resolve();
  });
  expect(mocks.loadCourses).toHaveBeenCalledTimes(2);
  expect(mocks.loadSources).toHaveBeenCalledTimes(2);
  expect(mocks.loadCourseCards).toHaveBeenCalledTimes(2);
  expect(mocks.loadChapters).toHaveBeenCalledOnce();
  expect(mocks.loadChapters).toHaveBeenCalledWith("source-new", courseSignals[1]);

  await act(async () => {
    oldCourses.reject(new Error("stale course failure"));
    oldSources.resolve([{ id: "source-old" }]);
    oldCards.reject(new Error("stale card failure"));
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(screen.queryByText(/stale course failure|stale card failure/)).not.toBeInTheDocument();
  expect(mocks.loadChapters).toHaveBeenCalledOnce();
  expect(screen.queryByTestId("course-list-skeleton")).not.toBeInTheDocument();
});

it.each([
  ["starting", { state: "starting" as const, port: 0 }],
  [
    "failed",
    {
      state: "failed" as const,
      port: 0,
      error: { category: "startup", message: "backend exited" },
    },
  ],
])("loads courses once after a %s service recovers to running", async (_state, initialStatus) => {
  vi.useFakeTimers();
  let resolveLoad: (() => void) | undefined;
  mocks.loadCourses.mockReturnValue(
    new Promise<void>((resolve) => {
      resolveLoad = resolve;
    }),
  );
  mocks.getServiceStatus.mockResolvedValueOnce(initialStatus).mockResolvedValue({ state: "running", port: 43127 });

  render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<CourseList />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  await act(async () => {
    await Promise.resolve();
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();
  expect(mocks.loadCourses).not.toHaveBeenCalled();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1499);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(2);
  expect(mocks.loadCourses).toHaveBeenCalledOnce();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(3);
  expect(mocks.loadCourses).toHaveBeenCalledOnce();

  resolveLoad?.();
  await act(async () => {
    await Promise.resolve();
  });
});

it("releases a timed out course request and exposes a working retry", async () => {
  vi.useFakeTimers();
  mocks.getServiceStatus.mockResolvedValue({ state: "running", port: 43127 });
  mocks.loadCourses
    .mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          window.setTimeout(() => reject(new DOMException("API 请求超时，请重试", "TimeoutError")), 30_000);
        }),
    )
    .mockResolvedValueOnce(undefined);

  render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<CourseList />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(mocks.loadCourses).toHaveBeenCalledOnce();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(29_999);
  });
  expect(screen.queryByText("API 请求超时，请重试")).not.toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByText("API 请求超时，请重试")).toBeInTheDocument();
  expect(mocks.loadCourses).toHaveBeenCalledOnce();

  fireEvent.click(screen.getByRole("button", { name: "重试课程列表" }));
  await act(async () => {
    await Promise.resolve();
  });
  expect(mocks.loadCourses).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("API 请求超时，请重试")).not.toBeInTheDocument();
});

it("does not overlap slow service status refreshes", async () => {
  vi.useFakeTimers();
  let resolveStatus: ((status: { state: "starting"; port: number }) => void) | undefined;
  mocks.getServiceStatus.mockReturnValue(
    new Promise((resolve) => {
      resolveStatus = resolve;
    }),
  );

  const view = render(
    <StrictMode>
      <MemoryRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<div>content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4500);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();

  view.unmount();
  resolveStatus?.({ state: "starting", port: 0 });
  await act(async () => {
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();
});

it("times out a hung service status request and recovers on the next polling slot", async () => {
  vi.useFakeTimers();
  mocks.getServiceStatus
    .mockReturnValueOnce(new Promise(() => undefined))
    .mockResolvedValue({ state: "running", port: 43127 });

  const view = render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<CourseList />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5999);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();
  expect(mocks.loadCourses).not.toHaveBeenCalled();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(2);
  expect(mocks.loadCourses).toHaveBeenCalledOnce();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1499);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(2);

  view.unmount();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(2);
});

it("caps hung raw service status calls and recovers when a late call settles", async () => {
  vi.useFakeTimers();
  const resolveStatuses: Array<(status: { state: "running"; port: number }) => void> = [];
  mocks.getServiceStatus.mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveStatuses.push(resolve);
      }),
  );

  render(
    <StrictMode>
      <MemoryRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<div>content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  await act(async () => {
    await Promise.resolve();
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledOnce();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(2);
  expect(screen.getByText(/已有后台查询仍在执行，请重启应用/)).toBeInTheDocument();

  await act(async () => {
    resolveStatuses[0]?.({ state: "running", port: 43127 });
    await Promise.resolve();
  });
  expect(screen.getByText("服务运行中 :43127")).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(mocks.getServiceStatus).toHaveBeenCalledTimes(3);
});

it("keeps service retry single-flight, reports rejection, and allows another attempt", async () => {
  let rejectRetry: ((reason: Error) => void) | undefined;
  mocks.getServiceStatus.mockResolvedValue({
    state: "failed",
    port: 0,
    error: { category: "startup", message: "backend exited" },
  });
  mocks.retryService
    .mockReturnValueOnce(
      new Promise<void>((_resolve, reject) => {
        rejectRetry = reject;
      }),
    )
    .mockResolvedValueOnce(undefined);

  render(
    <StrictMode>
      <MemoryRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<div>content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  const retryButton = await screen.findByRole("button", { name: "重试启动" });
  fireEvent.click(retryButton);
  fireEvent.click(retryButton);
  expect(mocks.retryService).toHaveBeenCalledOnce();

  await act(async () => {
    rejectRetry?.(new Error("restart command failed"));
    await Promise.resolve();
  });
  expect(screen.getByText("restart command failed")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "重试启动" }));
  await waitFor(() => expect(mocks.retryService).toHaveBeenCalledTimes(2));
  expect(await screen.findByText("服务正在重启")).toBeInTheDocument();
  expect(screen.queryByText("restart command failed")).not.toBeInTheDocument();
});

it("bounds retry UI while keeping the unsettled raw retry single-flight", async () => {
  vi.useFakeTimers();
  let rejectRawRetry: ((reason: Error) => void) | undefined;
  mocks.getServiceStatus.mockResolvedValue({
    state: "failed",
    port: 0,
    error: { category: "startup", message: "backend exited" },
  });
  mocks.retryService
    .mockReturnValueOnce(
      new Promise<void>((_resolve, reject) => {
        rejectRawRetry = reject;
      }),
    )
    .mockResolvedValueOnce(undefined);

  render(
    <StrictMode>
      <MemoryRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<div>content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  await act(async () => {
    await Promise.resolve();
  });
  fireEvent.click(screen.getByRole("button", { name: "重试启动" }));
  expect(mocks.retryService).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "正在重试" })).toBeDisabled();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(screen.queryByRole("button", { name: "正在重试" })).not.toBeInTheDocument();
  expect(screen.getByText(/重试仍在执行.*重启应用/)).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(screen.getByText(/重试仍在执行.*重启应用/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "重试启动" }));
  expect(mocks.retryService).toHaveBeenCalledOnce();
  expect(screen.getByText(/重试仍在执行.*重启应用/)).toBeInTheDocument();

  await act(async () => {
    rejectRawRetry?.(new Error("late retry failure"));
    await Promise.resolve();
  });
  expect(screen.getByText("late retry failure")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "重试启动" }));
  await act(async () => {
    await Promise.resolve();
  });
  expect(mocks.retryService).toHaveBeenCalledTimes(2);
  expect(screen.getByText("服务正在重启")).toBeInTheDocument();
});

it.each(["resolve", "reject", "timeout"] as const)(
  "does not let a pre-retry status %s overwrite the retry barrier",
  async (settlement) => {
    vi.useFakeTimers();
    let resolveStatus:
      ((status: { state: "failed"; port: number; error: { category: string; message: string } }) => void) | undefined;
    let rejectStatus: ((error: Error) => void) | undefined;
    const staleStatus = new Promise<{ state: "failed"; port: number; error: { category: string; message: string } }>(
      (resolve, reject) => {
        resolveStatus = resolve;
        rejectStatus = reject;
      },
    );
    mocks.getServiceStatus
      .mockResolvedValueOnce({
        state: "failed",
        port: 0,
        error: { category: "startup", message: "backend exited" },
      })
      .mockReturnValueOnce(staleStatus)
      .mockReturnValue(new Promise(() => undefined));

    render(
      <StrictMode>
        <MemoryRouter>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<div>content</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    );

    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(mocks.getServiceStatus).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "重试启动" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("服务正在重启")).toBeInTheDocument();

    await act(async () => {
      if (settlement === "resolve") {
        resolveStatus?.({
          state: "failed",
          port: 0,
          error: { category: "stale", message: "stale failed status" },
        });
      } else if (settlement === "reject") {
        rejectStatus?.(new Error("stale status rejection"));
      } else {
        await vi.advanceTimersByTimeAsync(5_000);
      }
      await Promise.resolve();
    });

    expect(screen.getByText("服务正在重启")).toBeInTheDocument();
    expect(screen.queryByText(/stale failed status|stale status rejection|服务状态查询超时/)).not.toBeInTheDocument();
  },
);

it("surfaces selected course data load failures and clears the error after retry", async () => {
  mocks.selectedCourseId = "course-1";
  mocks.getServiceStatus.mockResolvedValue({ state: "running", port: 43127 });
  mocks.loadSources.mockResolvedValue([{ id: "source-1" }]);
  mocks.loadCourseCards.mockRejectedValueOnce(new Error("card library unavailable")).mockResolvedValueOnce([]);

  render(
    <StrictMode>
      <MemoryRouter initialEntries={["/workbench"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/workbench" element={<div>content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );

  expect(await screen.findByText("card library unavailable")).toBeInTheDocument();
  expect(mocks.loadSources).toHaveBeenCalledOnce();
  expect(mocks.loadChapters).toHaveBeenCalledOnce();
  expect(mocks.loadCourseCards).toHaveBeenCalledOnce();

  fireEvent.click(screen.getByRole("button", { name: "重试课程资料" }));
  await waitFor(() => expect(screen.queryByText("card library unavailable")).not.toBeInTheDocument());
  expect(mocks.loadSources).toHaveBeenCalledTimes(2);
  expect(mocks.loadChapters).toHaveBeenCalledTimes(2);
  expect(mocks.loadCourseCards).toHaveBeenCalledTimes(2);
});

it("builds keyboard-searchable destinations for courses, textbooks, chapters and cards", () => {
  const results = buildSearchResults(
    {
      courses: [{ id: "c1", title: "战略管理", description: "MBA", root_dir: "/mba" }],
      sources: {
        c1: [
          { id: "s1", course_id: "c1", kind: "main", file_path: "/mba/a.pdf", title: "竞争战略教材", status: "READY" },
        ],
      },
      chapters: {
        s1: [{ id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "行业结构", status: "COMPLETED" }],
      },
      cardsByCourse: {
        c1: [
          {
            id: "card1",
            origin_type: "chapter",
            origin_id: "ch1",
            origin_title: "行业结构",
            card_type: "观点",
            title: "五力模型",
            content: "竞争分析",
            source_refs: [],
            tags: [],
            status: "ACTIVE",
            favorite: false,
            updated_at: 1,
          },
        ],
      },
    },
    "战略",
  );
  expect(results.map((result) => result.label)).toEqual(["战略管理", "竞争战略教材"]);
  expect(
    requireAt(
      buildSearchResults(
        {
          courses: [],
          sources: {},
          chapters: {
            s1: [{ id: "ch1", source_id: "s1", course_id: "c1", seq: 0, title: "行业结构", status: "COMPLETED" }],
          },
          cardsByCourse: {},
        },
        "行业",
      ),
      0,
      "chapter search result",
    ).to,
  ).toBe("/workbench/chapter?chapterId=ch1");
  expect(
    requireAt(
      buildSearchResults(
        {
          courses: [],
          sources: {},
          chapters: {},
          cardsByCourse: {
            c1: [
              {
                id: "card1",
                origin_type: "chapter",
                origin_id: "ch1",
                origin_title: "行业结构",
                card_type: "观点",
                title: "五力模型",
                content: "竞争分析",
                source_refs: [],
                tags: [],
                status: "ACTIVE",
                favorite: false,
                updated_at: 1,
              },
            ],
          },
        },
        "五力",
      ),
      0,
      "card search result",
    ).to,
  ).toBe("/workbench/cards?cardId=card1");
});
