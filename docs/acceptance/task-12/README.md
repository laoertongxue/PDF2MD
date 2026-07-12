# Task 12 Acceptance Evidence

Task 12 visual acceptance passed at both required desktop viewports. All captures were reviewed at
their original resolution. They contain no loading skeleton, Mermaid syntax error, overlapping UI,
or unrelated debug controls.

## Evidence Matrix

| Scenario | 1440x900 | 1024x768 | Result |
| --- | --- | --- | --- |
| Multi-textbook import queue | [PNG](import-queue-1440x900.png) | [PNG](import-queue-1024x768.png) | Two queued files and two existing textbooks are visible |
| Duplicate chapter names and topic mapping editor | [PNG](topic-map-1440x900.png) | [PNG](topic-map-1024x768.png) | Textbook A/B keep separate same-name chapters; mapping controls do not overlap |
| Topic-fusion source navigation | [PNG](fusion-sources-1440x900.png) | [PNG](fusion-sources-1024x768.png) | Source labels for both textbooks render as chapter links |
| Course-card source filter | [PNG](card-filter-1440x900.png) | [PNG](card-filter-1024x768.png) | `融合精读` is selected and only topic-origin cards remain |
| Backend error stops the run | [PNG](error-stop-1440x900.png) | [PNG](error-stop-1024x768.png) | Error text replaces the loading state and exposes `检查并恢复` |
| Recovery completes | [PNG](recovery-complete-1440x900.png) | [PNG](recovery-complete-1024x768.png) | Status becomes `失败` and the enabled `重新生成` action is restored |
| Two direct Mermaid previews on one page | [PNG](mermaid-full-1440x900.png) | [PNG](mermaid-full-1024x768.png) | Both labeled SVG diagrams are complete and non-empty |
| Mermaid machine assertions | [JSON](mermaid-1440x900.json) | [JSON](mermaid-1024x768.json) | `svg=2`, alerts/errors are zero, document and diagram widths fit |

## Mermaid Result

Both viewport result files record exactly two `.mermaid-preview svg` elements. Each SVG has a
non-zero rendered size and three visible Chinese labels. `role_alert` and `syntax_error` are zero,
and every recorded `scrollWidth` is less than or equal to its corresponding `clientWidth`.

## Release Summary

- Release version consistency and network E2E gates are enforced by commit `fa14d47`.
- Mermaid 11 runtime parsing, safe rendering, temporary-error cleanup, and responsive containment
  are covered by commit `b919a5b`.
- Task 12 desktop workflow evidence now covers import, mapping, fusion sources, card filtering,
  failure recovery, and two direct Mermaid previews at 1440x900 and 1024x768.
