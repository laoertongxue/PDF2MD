import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { chromium } from "@playwright/test";

const port = Number(process.env.TASK12_PORT || 4178);
const baseUrl = `http://127.0.0.1:${port}`;
const fixtureUrl = `${baseUrl}/acceptance/task-12-mermaid.html`;
const outputDir = path.resolve("../docs/acceptance/task-12");
const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }];

async function waitForServer() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(fixtureUrl);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Vite did not start at ${fixtureUrl}`);
}

await mkdir(outputDir, { recursive: true });
const server = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
  stdio: ["ignore", "pipe", "pipe"],
});

let serverLog = "";
server.stdout.on("data", (chunk) => { serverLog += chunk; });
server.stderr.on("data", (chunk) => { serverLog += chunk; });

try {
  await waitForServer();
  const browser = await chromium.launch();
  try {
    for (const viewport of viewports) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const dialogs = [];
      const externalRequests = [];
      const cspViolations = [];
      page.on("dialog", async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });
      page.on("request", (request) => {
        if (!request.url().startsWith(baseUrl)) externalRequests.push(request.url());
      });
      await page.exposeFunction("recordCspViolation", (detail) => cspViolations.push(detail));
      await page.addInitScript(() => {
        document.addEventListener("securitypolicyviolation", (event) => {
          window.recordCspViolation({ blockedURI: event.blockedURI, violatedDirective: event.violatedDirective });
        });
      });
      await page.goto(fixtureUrl, { waitUntil: "networkidle" });
      await page.locator(".mermaid-preview svg").first().waitFor();

      const result = await page.evaluate(() => {
        const previews = [...document.querySelectorAll(".mermaid-preview svg")];
        const adversarial = document.querySelector('[data-testid="adversarial-svg"]');
        const forbidden = adversarial?.querySelectorAll("script, style, foreignObject, use, image, iframe, object, embed").length ?? -1;
        const dangerousAttributes = [...(adversarial?.querySelectorAll("*") ?? [])].flatMap((node) =>
          [...node.attributes]
            .filter((attribute) => attribute.name !== "xmlns" && (/^(?:on|style$|href$|xlink:href$)/i.test(attribute.name) || /(?:javascript|data:|https?:|\/\/|url\(\s*[^#])/i.test(attribute.value)))
            .map((attribute) => ({ element: node.tagName, name: attribute.name, value: attribute.value })),
        );
        return {
          title: document.title,
          viewport: { width: innerWidth, height: innerHeight },
          page: { scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight },
          svgCount: previews.length,
          labels: previews.map((svg) => svg.textContent?.replace(/\s+/g, " ").trim()),
          alerts: document.querySelectorAll('[role="alert"]').length,
          forbidden,
          dangerousAttributes,
          safeChineseLabel: adversarial?.textContent?.includes("安全中文标签") ?? false,
          csp: document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute("content") ?? "",
        };
      });

      const assertions = {
        exactViewport: result.viewport.width === viewport.width && result.viewport.height === viewport.height,
        diagramsRendered: result.svgCount === 2 && result.labels.every((label) => /[\u4e00-\u9fff]/.test(label || "")),
        noRenderAlerts: result.alerts === 0,
        sanitizerBlockedActiveContent: result.forbidden === 0 && result.dangerousAttributes.length === 0,
        safeChineseLabelPreserved: result.safeChineseLabel,
        noDialogs: dialogs.length === 0,
        noExternalRequests: externalRequests.length === 0,
        noCspViolations: cspViolations.length === 0,
        strictCspPresent: result.csp.includes("object-src 'none'") && result.csp.includes("base-uri 'none'"),
      };
      const failed = Object.entries(assertions).filter(([, passed]) => !passed).map(([name]) => name);
      const suffix = `${viewport.width}x${viewport.height}`;
      await page.screenshot({ path: path.join(outputDir, `mermaid-viewport-${suffix}.png`), fullPage: false });
      await page.screenshot({ path: path.join(outputDir, `mermaid-full-${suffix}.png`), fullPage: true });
      const report = { fixtureUrl, screenshotModes: { viewport: "fullPage=false", full: "fullPage=true" }, ...result, dialogs, externalRequests, cspViolations, assertions, passed: failed.length === 0 };
      await writeFile(path.join(outputDir, `mermaid-${suffix}.json`), `${JSON.stringify(report, null, 2)}\n`);
      console.log(`${suffix}: ${report.passed ? "PASS" : `FAIL (${failed.join(", ")})`}`);
      await context.close();
      if (failed.length) throw new Error(`${suffix} failed: ${failed.join(", ")}`);
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
