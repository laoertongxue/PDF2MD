import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ServiceStatusView } from "./Layout";

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
