import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { chromium } from "@playwright/test";

const port = Number(process.env.TASK12_PORT || 4178);
const baseUrl = `http://127.0.0.1:${port}`;
const outputDir = path.resolve("../docs/acceptance/task-12");
const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }];
const scenarios = [
  {
    id: "import-queue",
    assertions: async (page) => ({
      twoQueuedFiles: await page.locator('[data-testid="import-row"]').count() === 2,
      independentStatuses: await page.getByText("等待导入", { exact: true }).count() === 1 && await page.getByText("章节识别中", { exact: true }).count() === 1,
      existingTextbooksVisible: await page.locator('[data-testid="existing-source"]').count() === 2,
    }),
  },
  {
    id: "topic-map",
    assertions: async (page) => ({
      duplicateChaptersSeparated: await page.getByText("第一章 管理导论", { exact: true }).count() === 2,
      twoSourceGroups: await page.locator('[data-testid="source-chapter-group"]').count() === 2,
      mappingsVisible: await page.locator('input[type="checkbox"]:checked').count() === 3,
      saveActionAvailable: await page.getByRole("button", { name: "保存章节映射" }).isEnabled(),
    }),
  },
  {
    id: "fusion-sources",
    assertions: async (page) => ({
      twoSourceLinks: await page.locator('[data-testid="source-link"]').count() === 2,
      strategicRoute: await page.getByRole("link", { name: "《战略管理》·第一章 管理导论" }).getAttribute("href") === "/workbench/chapter?chapterId=chapter-strategy-1",
      organizationRoute: await page.getByRole("link", { name: "《组织行为学》·第一章 管理导论" }).getAttribute("href") === "/workbench/chapter?chapterId=chapter-org-1",
    }),
  },
  {
    id: "card-filter",
    assertions: async (page) => ({
      fusionFilterSelected: await page.getByRole("button", { name: "融合精读" }).getAttribute("aria-pressed") === "true",
      onlyTopicCardsVisible: await page.locator('[data-testid="writing-card"]').count() === 3 && await page.locator('[data-testid="writing-card"][data-origin="topic"]').count() === 3,
      chapterCardsHidden: await page.locator('[data-testid="writing-card"][data-origin="chapter"]').count() === 0,
    }),
  },
  {
    id: "error-stop",
    assertions: async (page) => ({
      errorVisible: await page.getByRole("alert").getByText("主题融合失败：模型服务暂时不可用").count() === 1,
      loadingStopped: await page.getByText("正在生成融合精读", { exact: true }).count() === 0,
      recoveryAvailable: await page.getByRole("button", { name: "检查并恢复" }).isEnabled(),
      failedRoundRecorded: await page.locator('[data-testid="round-status"]').getAttribute("data-status") === "FAILED",
    }),
  },
  {
    id: "recovery-complete",
    assertions: async (page) => ({
      failedStateRestored: await page.locator('[data-testid="topic-status"]').getAttribute("data-status") === "FAILED",
      recoveryMessageVisible: await page.getByText("已结束过期任务，可重新生成").count() === 1,
      regenerateEnabled: await page.getByRole("button", { name: "重新生成" }).isEnabled(),
      recoveryActionRemoved: await page.getByRole("button", { name: "检查并恢复" }).count() === 0,
    }),
  },
];

async function waitForServer(url) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Vite did not start at ${url}`);
}

await mkdir(outputDir, { recursive: true });
const server = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
  stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (chunk) => { serverLog += chunk; });
server.stderr.on("data", (chunk) => { serverLog += chunk; });

try {
  const fixtureBaseUrl = `${baseUrl}/acceptance/task-12.html`;
  await waitForServer(fixtureBaseUrl);
  const browser = await chromium.launch();
  try {
    for (const viewport of viewports) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const viewportReport = { fixtureBaseUrl, screenshotMode: "fullPage=false", viewport, scenarios: {} };
      for (const scenario of scenarios) {
        const url = `${fixtureBaseUrl}?scenario=${scenario.id}`;
        await page.goto(url, { waitUntil: "networkidle" });
        await page.locator(`[data-scenario="${scenario.id}"]`).waitFor();
        const assertions = await scenario.assertions(page);
        const layout = await page.evaluate(() => ({
          exactViewport: innerWidth === document.documentElement.clientWidth && innerHeight === window.innerHeight,
          noHorizontalOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
          viewport: { width: innerWidth, height: innerHeight },
        }));
        assertions.exactViewport = layout.viewport.width === viewport.width && layout.viewport.height === viewport.height;
        assertions.noHorizontalOverflow = layout.noHorizontalOverflow;
        const failed = Object.entries(assertions).filter(([, passed]) => !passed).map(([name]) => name);
        await page.screenshot({ path: path.join(outputDir, `${scenario.id}-${viewport.width}x${viewport.height}.png`), fullPage: false });
        viewportReport.scenarios[scenario.id] = { url, assertions, passed: failed.length === 0 };
        console.log(`${scenario.id} ${viewport.width}x${viewport.height}: ${failed.length ? `FAIL (${failed.join(", ")})` : "PASS"}`);
        if (failed.length) throw new Error(`${scenario.id} ${viewport.width}x${viewport.height} failed: ${failed.join(", ")}`);
      }
      await writeFile(path.join(outputDir, `business-${viewport.width}x${viewport.height}.json`), `${JSON.stringify(viewportReport, null, 2)}\n`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
} catch (error) {
  if (serverLog) process.stderr.write(serverLog);
  throw error;
} finally {
  server.kill("SIGTERM");
}
