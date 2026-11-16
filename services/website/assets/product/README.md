# Website product screenshots

These are **real Chromium screenshots of the Vue application**, rendered from
`services/frontend` at app revision `8ea92d5c6723de102140dec769c8904427e4f422`.
They are not illustrations, composites or hand-built HTML facsimiles. No injected
styles, hidden controls or retouching were used. The website supplies its own
responsive device framing.

## Sample-content disclosure

All conversations, profile names, timestamps, completed tool-call records and
published-output metadata are deterministic **fictional demonstration data**.
Desktop shows Chat; phone shows Outputs. Profiles are Research, Builder and
Writing. No customer data, model calls, backend mutations or generated document
contents are involved. File names, byte counts and ready states illustrate the
real output components, not an end-to-end file-generation/download test.

`tests/website-product-fixture.js` reuses the app's account/agent/design fixtures.
API requests are intercepted and read-state acknowledgements handled locally.
Unexpected mutations fail; remote requests are blocked. The localStorage token
is an inert fixture string, never a credential. Vite listens only on loopback.

## Files

| File | Natural pixels | Bytes | Encoding |
| --- | --- | ---: | --- |
| `desktop.webp` | 1920 × 1260 | 54,808 | Lossless WebP |
| `desktop-small.webp` | 1280 × 840 | 35,950 | Lossless WebP |
| `phone.webp` | 780 × 1688 | 39,036 | Lossless WebP |
| `desktop.png` | 1920 × 1260 | 162,152 | Original browser capture |
| `desktop-small.png` | 1280 × 840 | 100,323 | Original browser capture |
| `phone.png` | 780 × 1688 | 127,793 | Original browser capture |

Combined distribution WebPs: **129,794 bytes**. Each WebP was decoded and checked
pixel-for-pixel against its original PNG. Desktop-small is a native DPR 1 capture,
not a resize. Desktop uses viewport 1280 × 840 / DPR 1.5; phone 390 × 844 / DPR 2.
All use the unmodified Denkzeug light theme, 125% interface size, local fonts,
`en-US`, UTC, reduced motion and fixed clock `2026-06-15T10:30:00Z`.
Capture environment: Node 24.22.3, Playwright 1.65.1 / Chromium 159, Linux;
lossless encoding: Pillow 13.0.0 / libwebp 1.6.0.

## Refresh

From the repository root, install frontend dependencies and the workspace Python
packages (the capture script uses Pillow):

```sh
uv sync --locked --all-packages
cd services/frontend
npm ci --ignore-scripts
npx playwright install chromium
PYTHON=../../.venv/bin/python npm run capture:website
```

The script starts/stops a loopback Vite server and refreshes both these device
views and `../product-tour/`. It checks actual rendered controls, overflow,
unexpected requests and accidental writes. Every image requires two consecutive
identical browser frames. `capture-report.json` records source revision, clock,
viewports, requests, image hashes and pixel-identical lossless conversions.

After UI changes, update the fixture/capture selectors to the real app contracts,
never inject layout overrides to preserve an old look. Inspect the resulting PNGs
and update the source revision and byte table here. The dotted header ring with
a dash is the idle context meter, not a loading spinner.
