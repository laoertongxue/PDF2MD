import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { chromium, expect } from "@playwright/test";
import { startFixtureServer } from "./task-12-fixture-server.mjs";

const port = Number(process.env.TASK12_PORT || 4178);
const apiPort = port + 1;
const baseUrl = `http://127.0.0.1:${port}`;
const apiUrl = `http://127.0.0.1:${apiPort}`;
const outputDir = path.resolve("../docs/acceptance/task-12");
const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }];

const fixture = await startFixtureServer(apiPort);
const vite = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
  stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, VITE_API_BASE_URL: apiUrl },
});
let serverLog = ""; vite.stdout.on("data", c => serverLog += c); vite.stderr.on("data", c => serverLog += c);

try {
  await waitForServer(`${baseUrl}/acceptance/task-12.html`);
  await mkdir(outputDir, { recursive: true });
  const browser = await chromium.launch();
  try {
    for (const viewport of viewports) {
      await fetch(`${apiUrl}/__fixture/reset`, { method: "POST" });
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const failures = [];
      page.on("pageerror", error => failures.push(`pageerror: ${error.message}`));
      page.on("console", msg => { if (msg.type() === "error") failures.push(`console: ${msg.text()}`); });
      await exercise(page);
      const layout = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth }));
      if (layout.width !== viewport.width || layout.height !== viewport.height || layout.overflow) failures.push(`layout ${JSON.stringify(layout)}`);
      const requests = await fetch(`${apiUrl}/__fixture/requests`).then(r => r.json());
      const required = [
        ["POST", "/api/workbench/courses"], ["PUT", "/api/workbench/sources/source-1/chapter-drafts"],
        ["POST", "/api/workbench/sources/source-1/chapter-drafts/confirm"], ["POST", "/api/workbench/chapters/chapter-1/run-hybrid"],
        ["PATCH", "/api/workbench/chapters/chapter-1/note-blocks/knowledge_mermaid"], ["PATCH", "/api/workbench/topics/topic-1"],
        ["PUT", "/api/workbench/topics/topic-1/chapters"], ["POST", "/api/workbench/topics/topic-1/recover"],
        ["POST", "/api/workbench/topics/topic-1/run-hybrid"], ["PATCH", "/api/workbench/topics/topic-1/note-blocks/knowledge_mermaid"],
        ["PATCH", "/api/workbench/cards/card-1"], ["PATCH", "/api/workbench/cards/card-1/favorite"],
      ];
      for (const [method, requestPath] of required) if (!requests.some(r => r.method === method && r.path === requestPath)) failures.push(`missing request ${method} ${requestPath}`);
      const report = { passed: failures.length === 0, viewport, route: page.url(), requests, assertions: { realProductionRoute: page.url().includes("#/workbench/cards?cardId=card-1"), refreshPersistence: true, twoMermaidNoErrors: true, noHorizontalOverflow: !layout.overflow }, failures };
      await page.screenshot({ path: path.join(outputDir, `real-workflow-${viewport.width}x${viewport.height}.png`), fullPage: false });
      await writeFile(path.join(outputDir, `business-${viewport.width}x${viewport.height}.json`), `${JSON.stringify(report, null, 2)}\n`);
      console.log(`real workflow ${viewport.width}x${viewport.height}: ${failures.length ? `FAIL (${failures.join("; ")})` : "PASS"}`);
      await context.close();
      if (failures.length) throw new Error(failures.join("; "));
    }
  } finally { await browser.close(); }
} catch (error) {
  if (serverLog) process.stderr.write(serverLog);
  throw error;
} finally { vite.kill("SIGTERM"); fixture.server.close(); }

async function exercise(page) {
  const app = `${baseUrl}/acceptance/task-12.html`;
  await page.goto(`${app}#/workbench`, { waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: "课程精读" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("浏览器版不支持选择本地课程目录");
  await page.getByPlaceholder("战略管理").fill("验收新课程");
  await page.getByPlaceholder("选择或粘贴课程资料所在文件夹").fill("/fixture/new-course");
  await page.getByRole("button", { name: "创建课程" }).click();
  await expect(page.getByRole("button", { name: /验收新课程 \/fixture\/new-course/ })).toBeVisible();
  await page.getByRole("button", { name: /企业战略与组织协同 MBA \/fixture\/mba/ }).click();

  await page.goto(`${app}#/workbench/source`, { waitUntil: "networkidle" });
  await page.getByLabel("选择教材文件").setInputFiles([{ name: "战略管理.pdf", mimeType: "application/pdf", buffer: Buffer.from("pdf") }, { name: "组织行为学.docx", mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: Buffer.from("docx") }]);
  await expect(page.getByText("导入队列 · 2 本")).toBeVisible();
  await expect(page.getByText("无法读取本地文件路径")).toHaveCount(2);

  await page.goto(`${app}#/workbench/chapters`, { waitUntil: "networkidle" });
  const editor = page.getByRole("region", { name: "战略管理" });
  await editor.getByLabel("章节名称").first().fill("第一章 战略管理导论");
  await editor.getByRole("button", { name: /拆分 第一章 战略管理导论/ }).click();
  await editor.getByRole("button", { name: "保存章节草稿" }).click();
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.locator('input[aria-label="章节名称"][value="第一章 战略管理导论"]')).toBeVisible();
  await page.getByRole("region", { name: "战略管理" }).getByRole("button", { name: "确认章节目录" }).click();
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.getByText("章节目录已确认并锁定").first()).toBeVisible();

  await page.goto(`${app}#/workbench/chapter?chapterId=chapter-1`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "从审核轮重跑" }).click();
  await expect(page.getByLabel("精读轮次历史").getByText("已完成").last()).toBeVisible();
  const chapterMermaid = page.getByRole("textbox", { name: "知识结构图 Mermaid 源码" });
  await chapterMermaid.fill("flowchart LR\nA[战略]-->B[组织协同]");
  await page.getByRole("button", { name: "保存 Mermaid" }).first().click();
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.getByRole("textbox", { name: "知识结构图 Mermaid 源码" })).toHaveValue(/组织协同/);

  await page.goto(`${app}#/workbench/courses/course-1/topics`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "编辑主题" }).click();
  await page.getByLabel("主题名称").fill("战略选择与组织协同");
  await page.getByRole("button", { name: "保存主题" }).click();
  await page.getByRole("region", { name: "章节映射" }).getByText("第二章 竞争战略").click();
  await page.getByRole("button", { name: "保存章节映射" }).click();
  await expect(page.getByText("战略选择与组织协同", { exact: true }).first()).toBeVisible();

  await page.goto(`${app}#/workbench/courses/course-1/fusion/topic-1`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "检查并恢复" }).click();
  await expect(page.getByRole("button", { name: "检查并恢复" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "运行融合精读" })).toBeEnabled();
  await page.getByRole("button", { name: "运行融合精读" }).click();
  await expect(page.getByText("已完成", { exact: true }).first()).toBeVisible();
  await page.getByRole("link", { name: "[《组织行为学》·第 1 章]" }).first().click();
  await expect(page).toHaveURL(/#\/workbench\/chapter\?chapterId=chapter-3/);
  await page.goBack({ waitUntil: "networkidle" });
  await expect(page.locator("svg").filter({ has: page.locator("g") })).toHaveCount(2);
  const topicMermaid = page.getByRole("textbox", { name: "knowledge_mermaid Mermaid 源码" });
  await topicMermaid.fill("flowchart LR\nK[知识]-->A[行动]");
  await page.getByRole("button", { name: "保存 Mermaid" }).first().click();
  await page.goto(`${app}#/workbench/courses/course-1/fusion/topic-1`, { waitUntil: "networkidle" });
  await expect(page.getByRole("textbox", { name: "knowledge_mermaid Mermaid 源码" })).toHaveValue(/行动/);
  await expect(page.getByRole("alert")).toHaveCount(0);

  await page.goto(`${app}#/workbench/cards?cardId=card-1`, { waitUntil: "networkidle" });
  await expect(page.getByRole("status")).toContainText("已定位到");
  await page.getByLabel("搜索课程卡片").fill("竞争优势");
  await page.getByRole("button", { name: "编辑 竞争优势不是单点能力" }).click();
  await page.getByRole("dialog").getByLabel("标题").fill("竞争优势来自系统能力");
  await page.getByRole("dialog").getByRole("button", { name: "保存" }).click();
  await page.getByRole("button", { name: "收藏", exact: true }).click();
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.getByText("竞争优势来自系统能力", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "取消收藏" })).toBeVisible();
  await expect(page.locator("#card-card-1")).toHaveAttribute("data-highlighted", "true");
}

async function waitForServer(url) { for (let i=0;i<80;i++){try{if((await fetch(url)).ok)return;}catch{} await new Promise(r=>setTimeout(r,200));} throw new Error(`Vite did not start at ${url}`); }
