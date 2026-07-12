# Task 12 Acceptance Evidence

Task 12 visual acceptance passed at both required desktop viewports. Workflow screenshots below
remain historical full-page evidence. Mermaid security evidence is reproducible and explicitly
separates viewport captures (`fullPage=false`) from full-page captures (`fullPage=true`).

## Evidence Matrix

| Scenario | 1440x900 | 1024x768 | Result |
| --- | --- | --- | --- |
| Multi-textbook import queue | [PNG](import-queue-1440x900.png) | [PNG](import-queue-1024x768.png) | Two queued files and two existing textbooks are visible |
| Duplicate chapter names and topic mapping editor | [PNG](topic-map-1440x900.png) | [PNG](topic-map-1024x768.png) | Textbook A/B keep separate same-name chapters; mapping controls do not overlap |
| Topic-fusion source navigation | [PNG](fusion-sources-1440x900.png) | [PNG](fusion-sources-1024x768.png) | Source labels for both textbooks render as chapter links |
| Course-card source filter | [PNG](card-filter-1440x900.png) | [PNG](card-filter-1024x768.png) | `融合精读` is selected and only topic-origin cards remain |
| Backend error stops the run | [PNG](error-stop-1440x900.png) | [PNG](error-stop-1024x768.png) | Error text replaces the loading state and exposes `检查并恢复` |
| Recovery completes | [PNG](recovery-complete-1440x900.png) | [PNG](recovery-complete-1024x768.png) | Status becomes `失败` and the enabled `重新生成` action is restored |
| Mermaid viewport capture (`fullPage=false`) | [PNG](mermaid-viewport-1440x900.png) | [PNG](mermaid-viewport-1024x768.png) | Browser viewport is exactly the filename dimensions; Chinese SVG text is visible |
| Mermaid full-page capture (`fullPage=true`) | [PNG](mermaid-full-1440x900.png) | [PNG](mermaid-full-1024x768.png) | Full fixture page, intentionally taller than the viewport |
| Mermaid machine assertions | [JSON](mermaid-1440x900.json) | [JSON](mermaid-1024x768.json) | Two diagrams, exact viewport, XSS/CSP, external-request, dialog, and sanitizer assertions pass |

## Mermaid Result

Run from `parsing-core-app`:

```bash
npm run accept:task-12
```

The script starts Vite on `http://127.0.0.1:4178` and opens the committed fixture at
`/acceptance/task-12-mermaid.html`. Set `TASK12_PORT` to override the port. It renders two real
Mermaid 11 diagrams with Chinese labels and an adversarial SVG containing event handlers,
`foreignObject`, `style`, `use`, external resources, and an entity-encoded dangerous URL.

Both JSON reports record `passed: true` and the exact browser viewport. Assertions require two
Chinese-labeled SVG diagrams, zero render alerts, zero forbidden nodes/attributes, no dialogs,
no external requests, no CSP violations, and a CSP containing `object-src 'none'` and
`base-uri 'none'`. The safe Chinese label in the adversarial SVG must remain visible.

## Release Summary

- Release version consistency and network E2E gates are enforced by commit `fa14d47`.
- Mermaid uses root-level `htmlLabels:false`, DOMPurify's SVG profile, and an explicit tag/attribute
  allowlist. `foreignObject`, `style`, `use`, external resources, navigation attributes, event
  handlers, and non-local `url(...)` values are rejected; local marker references remain allowed.
- Task 12 desktop workflow evidence now covers import, mapping, fusion sources, card filtering,
  failure recovery, and two direct Mermaid previews at 1440x900 and 1024x768.
