# Task 12 Real Interaction Acceptance

`/acceptance/task-12.html` mounts the production `App`, `HashRouter`, routes, Zustand store,
components, and API client. It contains no scenario-only UI. The business run starts a deterministic
local HTTP fixture server whose in-memory state survives browser reloads for the duration of each
viewport run. The fixture records every production API request and rejects unknown endpoints.

## Covered workflow

- Create a course and assert the browser-only local-directory limitation.
- Select two textbooks and assert the real two-item browser import queue behavior.
- Rename and split a chapter draft, save, reload, confirm, reload, and assert locking.
- Rerun a failed chapter round through the existing whole-chapter hybrid API contract.
- Save chapter body/Mermaid content and assert it after reload.
- Edit a topic and its chapter mapping through production routes.
- Recover FAILED topic fusion, rerun it, follow both source-chapter routes, save Mermaid, and reload.
- Search, edit, favorite, reload, and locate a card through `cardId`.
- Assert two Mermaid SVGs without render alerts and no horizontal document overflow.

## Evidence

| Viewport | Request/state report | Screenshot | Result |
| --- | --- | --- | --- |
| 1440x900 | [JSON](business-1440x900.json) | [PNG](real-workflow-1440x900.png) | PASS |
| 1024x768 | [JSON](business-1024x768.json) | [PNG](real-workflow-1024x768.png) | PASS |

The JSON reports include the final production route, viewport/overflow assertions, and the complete
fixture request log. Missing requests, route/state assertion failures, browser errors, Mermaid
errors, or overflow cause a non-zero exit.

Run from `parsing-core-app`:

```bash
npm test
npm run build
npm run accept:task-12
```

The separate Mermaid security fixture remains part of `accept:task-12` and still validates two
sanitized diagrams, CSP, forbidden SVG content, external requests, and both required viewports.
