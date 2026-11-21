# Actual application screenshot tour

These are untouched Chromium screenshots of the repository's **actual Vue/Vite application**, not a wireframe or a recreated interface. All chats, agents, output metadata, workflows, runs and usage statistics are **fictional demonstration data** supplied through the existing Playwright `mockAgents` → `mockDesign` → SSE fixtures, extended in `services/frontend/tests/website-product-fixture.js`. No model is called, no output document is generated, and no real account or API backend is contacted. The token is an inert fixture string. Read-state mutations are intercepted in memory; other API writes and external traffic are forbidden. The briefing scene uses the real loopback MCP Apps sandbox (`127.0.0.1:8092`), the App SDK, and a fictional saved workflow result. The capture selects the app’s Decision filter and verifies that it reduces the list from three updates to two.

The frontend source revision is recorded in `capture-report.json`. No styles or markup are injected into the host app for these captures.

- Viewport: 1280 × 840 CSS pixels; DPR 1.5; PNG and WebP: 1920 × 1260.
- Theme: default Denkzeug light, interface size 125%, no skin, original solid logo.
- Clock: 2026-06-15 10:30:00 UTC; locale en-US; reduced motion.
- PNG files are original browser output, never cropped/resized/composited.
- WebP files use Pillow/libwebP **lossless** compression. Each decoded RGB pixel is checked against its PNG, with hashes and exact byte sizes in `capture-report.json`. These are not native PhotoCraft exports.
- The capture script waits for fonts, checks no horizontal document overflow, no broken images, no visible errors/loading/draft-sync notices, and two consecutive identical browser frames. The original idle meter can vary slightly across browser/OS rasterizers. Vue Flow emits an upstream deprecation warning in the developer console; there are no browser errors or user-visible warnings.

## Reproduce

From the repository root (Node compatible with frontend package, Python with Pillow):

```sh
cd services/frontend
npm ci
PLAYWRIGHT_BROWSERS_PATH=/workspace/playwright npx playwright install chromium
python3 -m venv /workspace/pillow
/workspace/pillow/bin/pip install pillow
PLAYWRIGHT_BROWSERS_PATH=/workspace/playwright PYTHON=/workspace/pillow/bin/python node scripts/capture-website.mjs
PLAYWRIGHT_BROWSERS_PATH=/workspace/playwright npx playwright test tests/run-statistics.spec.js --workers=1
```

The script starts its own local Vite server, renders every scene, converts and verifies WebPs, and regenerates `services/website/product-tour-data.mjs` and the capture report. On recapture, the report/data source commit records the current checked-out frontend SHA.

## Routes and actual controls

All selectors below are actual Playwright selectors; routes after initial chat are reached by normal UI clicks, not substituted HTML:

| Scene | Route | Navigation / selected UI |
| --- | --- | --- |
| chat | `/chats/alpha` | Initial route; `getByRole('textbox', { name: 'Message', exact: true })`; real Chat tab |
| outputs | `/chats/alpha` | `getByRole('tab', { name: /^Workspace/ }).click()` |
| show | `/chats/briefing` | Open the Weekly briefing chat; select Decision in the sandboxed MCP App |
| workflows | `/workflows/digest` | Main navigation Workflows, then `locator('a[href="/workflows/digest"]').click()`; select Definition, then the real Flow view; `getByRole('button', { name: 'Fit all steps', exact: true }).click()` |
| runs | `/workflows/digest/runs/digest-record` | Workflows sidebar All runs, then `locator('a[href^="/workflows/digest/runs/digest-record"]').click()`; `getByRole('button', { name: 'Details', exact: true }).click()` |
| statistics | `/workflows/digest/runs/digest-record` | `getByRole('button', { name: 'Statistics', exact: true }).click()`; real Run statistics component |
| settings | `/settings/general` | Main navigation Settings; actual General category (Appearance is visible in the real category navigation) |

Main navigation selector:
`page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('link', { name: label, exact: true })` with labels Chats, Workflows, Settings. Run history is under Workflows.

Hotspots are measured with `getBoundingClientRect()` immediately before capture and divided by 1280/840. No positions are guessed. Per-scene source selectors and raw CSS-pixel rectangles are in the report; normalized rectangles and scene targets are in `product-tour-data.mjs`.

- Main navigation targets other captured scenes; self-targets are omitted.
- Chat Workspace tab → outputs; Workspace Chat tab → chat.
- Weekly briefing chat → show; product brief chat in the briefing sidebar → chat.
- Runs Statistics button → statistics; Statistics Details button → runs.
- Settings Back to workspace button → statistics, the actual workspace used before entering settings.
- The static tour returns to captured detail states, rather than representing live API interaction.

The website tour loads other scenes as visitors select them. The Show section also reuses the briefing WebP as a lazy-loaded still image; the public website never runs the demo MCP App. The phone screenshot is refreshed by the same script and remains separate.
