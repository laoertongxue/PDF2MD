import { spawnSync } from "node:child_process";
import process from "node:process";

for (const script of ["scripts/accept-task-12-business.mjs", "scripts/accept-task-12-mermaid.mjs"]) {
  const result = spawnSync(process.execPath, [script], { stdio: "inherit", env: process.env });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
