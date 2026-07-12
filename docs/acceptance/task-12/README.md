# Task 12 Acceptance Evidence

Task 12 visual acceptance is reproducible at both required desktop viewports. The business and
Mermaid fixtures use deterministic local routes, execute DOM assertions, and regenerate every
PNG/JSON artifact listed below.

## Evidence Matrix

| Scenario | 1440x900 | 1024x768 | Result |
| --- | --- | --- | --- |
| Multi-textbook import queue | [PNG](import-queue-1440x900.png) | [PNG](import-queue-1024x768.png) | Two independent queue rows/statuses and two existing textbooks |
| Duplicate chapter names and topic mapping | [PNG](topic-map-1440x900.png) | [PNG](topic-map-1024x768.png) | Same-name chapters stay in two source groups; three mappings and save action are asserted |
| Topic-fusion source navigation | [PNG](fusion-sources-1440x900.png) | [PNG](fusion-sources-1024x768.png) | Both source labels resolve to their exact chapter routes |
| Course-card source filter | [PNG](card-filter-1440x900.png) | [PNG](card-filter-1024x768.png) | `融合精读` is pressed; three topic cards and zero chapter cards are visible |
| Backend error stops the run | [PNG](error-stop-1440x900.png) | [PNG](error-stop-1024x768.png) | FAILED round, stopped loading, error alert, and recovery action are asserted |
| Recovery completes | [PNG](recovery-complete-1440x900.png) | [PNG](recovery-complete-1024x768.png) | FAILED state is restored, recovery action is removed, and regenerate is enabled |
| Business DOM assertions | [JSON](business-1440x900.json) | [JSON](business-1024x768.json) | All scenario assertions, exact viewport, route, and overflow results |
| Mermaid viewport capture (`fullPage=false`) | [PNG](mermaid-viewport-1440x900.png) | [PNG](mermaid-viewport-1024x768.png) | Browser viewport is exactly the filename dimensions; Chinese SVG text is visible |
| Mermaid full-page capture (`fullPage=true`) | [PNG](mermaid-full-1440x900.png) | [PNG](mermaid-full-1024x768.png) | Full fixture page, intentionally taller than the viewport |
| Mermaid machine assertions | [JSON](mermaid-1440x900.json) | [JSON](mermaid-1024x768.json) | Two diagrams, exact viewport, XSS/CSP, external-request, dialog, and sanitizer assertions pass |

## Mermaid Result

From a clean checkout, run:

```bash
npm ci
npx playwright install chromium
npm run accept:task-12
```

The command starts and stops Vite itself on `http://127.0.0.1:4178`; set `TASK12_PORT` to override
the port. Business scenarios use `/acceptance/task-12.html?scenario=<scenario-id>`. The six IDs are
`import-queue`, `topic-map`, `fusion-sources`, `card-filter`, `error-stop`, and
`recovery-complete`. Screenshots use `fullPage=false`, and every JSON report records the exact
1440x900 or 1024x768 browser viewport plus horizontal-overflow and scenario-specific assertions.

The Mermaid fixture remains `/acceptance/task-12-mermaid.html`. It renders two real Mermaid 11
diagrams with Chinese labels and an adversarial SVG containing event handlers, `foreignObject`,
`style`, `use`, external resources, and an entity-encoded dangerous URL.

Both JSON reports record `passed: true` and the exact browser viewport. Assertions require two
Chinese-labeled SVG diagrams, zero render alerts, zero forbidden nodes/attributes, no dialogs,
no external requests, no CSP violations, and a CSP containing `object-src 'none'` and
`base-uri 'none'`. The safe Chinese label in the adversarial SVG must remain visible.

## Release Summary

- Release version consistency and network E2E gates are enforced by commit `fa14d47`.
- Mermaid uses root-level `htmlLabels:false`, DOMPurify's SVG profile, and an explicit tag/attribute
  allowlist. `foreignObject`, `style`, `use`, external resources, navigation attributes, event
  handlers, and non-local `url(...)` values are rejected; local marker references remain allowed.
- Task 12 desktop workflow evidence covers import, duplicate chapter boundaries and mapping,
  fusion source routing, card filtering, error stop and recovery, and Mermaid security at both
  required viewports.
