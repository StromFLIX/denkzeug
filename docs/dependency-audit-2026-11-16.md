# Dependency audit — 2026-11-16

Upstreams were queried anew on **2026-11-16 UTC**, not inferred from the November
13 decisions. Initial clean detached HEAD and `origin/main` were both
`34fdcb56d6ad7ff7b5b35caf6f2a9e01eee6dc66`. No existing work was reset or rewritten.
Scope: workspace/transitive Python lock; separate hash-locked speech appliance;
web/mobile/Electron and transitive/optional npm lock;
gosu/Go compiler/modules; every Dockerfile, Compose/Bake image and copied binary;
uv/npm/Pi/Playwright; all Actions; all 13 ArtCraft releases and 26 architecture
hashes; pinned models; duplicate pins, fingerprints, upgrade/deployment tooling,
and documentation. User projects/apps/workflows/packages/MCP configurations were
not upgraded or republished. No production release or data migration is performed.

## Applied updates and compatibility

| Dependency | Before | After |
| --- | --- | --- |
| FastAPI, workspace and speech | 0.162.7 | 0.163.1 |
| NumPy, workspace and speech | 2.5.4 | 2.5.5 |
| Temporal Python SDK | 1.36.0 | 1.36.1 |
| DOMPurify | 3.4.17 | 3.4.18 |
| Playwright client, agent pin, CI browser image | 1.65.0 | 1.65.1 |
| PostCSS, transitive npm lock | 8.5.32 | 8.5.33 |
| setup-python Action | v6 | v7 (current v7.1.0) |

Python and speech graphs were re-resolved with uv; speech hashes were regenerated.
`npm update` refreshed the compatible graph and `npm ci` verified the resulting
integrities. Canonical source
fingerprints were regenerated with `image_context.py --write-lock`, not invented.
The existing version-qualified Electron minimatch overrides are unchanged.

Reviewed upstream changes:

- [FastAPI 0.163.0](https://github.com/fastapi/fastapi/releases/tag/0.163.0)
  adds optional dependency concurrency limits; [0.163.1](https://github.com/fastapi/fastapi/releases/tag/0.163.1)
  fixes OpenAPI generation for nested computed fields. Existing endpoint and
  dependency contracts remain exercised by the complete Python suite.
- [SDK 1.36.1](https://github.com/temporalio/sdk-python/releases/tag/1.36.1)
  disables single-batch activation by default for **unflagged** histories,
  restoring legacy signal/condition/cancellation behavior; already-flagged
  histories retain their recorded behavior. No history flags are rewritten.
  This is not a Temporal server/schema jump; real replay/migration/restore CI
  remains mandatory.
- [DOMPurify 3.4.18](https://github.com/cure53/DOMPurify/releases/tag/3.4.18)
  fixes template-scrubbing/clobbering and hook allow-list persistence, and removes
  invalid SVG elements. No sanitizer configuration or security check was weakened.
- [Playwright 1.65.1](https://github.com/microsoft/playwright/releases/tag/v1.65.1)
  fixes decorated-class transforms and Bun locations. Its browser cohort is
  Chromium **159.0.8154.6**, Firefox **159.0**, WebKit **27.2**. All three
  repository browser pins move together; Firefox is not a claimed test target.
- [setup-python v7](https://github.com/actions/setup-python/releases/tag/v7.0.0)
  migrates to ESM and removes `pip-install`. Our sole consumer only supplies
  `python-version: '3.13'`, on the supported hosted runner/container; it does not
  use the removed input. v7.1 updates Action dependencies. setup-java's floating
  v6 already consumes the current **6.3.0**, so no redundant pin edit is needed.

Repository frontend skills/source/license guidance was read before frontend
manifest changes. No UI redesign or vendor skill rewriting is part of this audit.

## Fresh upstream inventory and intentional holds

The [machine-readable upstream inventory](dependency-audit-2026-11-16-upstream.json)
records all queried workspace Python versions, direct npm versions,
upstream Action/runtime releases, ArtCraft digests and **257 installed
parent/dependency outdated observations** with their exact parent and next action.
Absent optional-platform packages are not misrepresented as installed updates.
This inventory is not an override list. In particular:

- **Collector 0.165.1:** a new contrib *source release* exists (consul detection
  dependency revert), but its official Docker Hub **0.165.1 tag returns HTTP 404**,
  while 0.165.0 returns 200. The linked core v0.165.1 release also returns 404.
  Keep **0.165.0** rather than deploy a nonexistent tag or a nightly. Next action:
  consume the official image once published, verify OTLP/config behavior, and
  inspect its SBOM/Grype embedded module/compiler inventory. This re-evaluates,
  rather than silently inherits, the old Collector hold.
- **FastEmbed 0.9.1** still constrains huggingface-hub to `<2` and loguru to
  `<0.8`; current compatible **1.33.0 / 0.7.3** remain. Latest **2.4.0 / 0.8.0**
  require parent compatibility changes. **botocore 1.44.3** still constrains
  python-dateutil `<3`; retain **2.9.0.post0**, not latest **4.0.0**. Consume tested
  compatible parent releases; do not bypass their resolver constraints.
- All direct npm packages are current after the update. Parent-owned exact/range
  pins keep older Babel, Electron helper, ASAR, glob/rimraf, etc. versions; exact
  versions/parents are in the JSON. For example Dagre 3.1.3 pins graphlib **4.0.6**
  exactly (latest **4.0.7**); Vite 8.3.8 requires Rolldown **~1.2.18**, so its
  resolved **1.2.19** must not be overridden to **1.3.2**, even though unplugin's
  deduplicated peer would permit it. Next action for each JSON row: upgrade the
  named parent when compatible, then rerun Electron ASAR/filter/native-module,
  web, and relevant native-platform tests. Deprecated inflight/boolean/glob7/
  rimraf2 warnings remain upstream debt, not silently removed API dependencies.
  Full application `npm audit`, including dev tools, currently reports **zero**.
- **npm 12.2.2 / Node 26:** npm's latest supported 11 remains **11.21.2**;
  Node 24 remains **24.22.3**. npm12 changes lifecycle-script approval, remote/git
  source policy and CLI/JSON behavior in the arbitrary trusted stdio/Pi package
  runtime, not only the frontend. Next step is explicit per-package script and
  source policy/fixtures plus native/Pi/MCP two-architecture validation, not a
  global allow-all. Retain the supported Node24/npm11 contract meanwhile.
- **Python 3.15.2 / distribution upgrades:** 3.13's current patch remains
  **3.13.18**. The project still targets 3.13; major runtime/ABI changes require
  speech native wheels, search/ONNX, browser and two-architecture coverage.
  Retain supported Debian Bookworm/Trixie and compiler Alpine3.22 contracts;
  consume vendor fixes in those lines rather than replace libc/OpenSSL manually.
- **PostgreSQL 18:** no unauthorized major/directory/data migration. **17.12**
  is current on the existing `17-alpine` line. Preserve data mounts, role secrets,
  backup/restore and entrypoint privilege dropping. A major migration requires
  separately approved, rehearsed backup/schema/rollback work.
- **Unchanged/current:** uv **0.13.8** (six copies), Pi **1.5.2** and exact runtime
  guard, agentgateway **1.7.0** and checksum-pinned CI binary, MaxIO **0.4.2**,
  Temporal server/admin-tools **1.32.2**, Temporal UI **2.58.1**, nginx **1.31.8**,
  copied BusyBox **1.38.0-musl**, Docker CLI **29.9.3**, Buildx **0.39.0**, Rust
  **1.100.0**, gosu **1.20**, Go **1.27.4**, moby/sys/user **0.5.0**, x/sys
  **0.50.0**. Other Actions: checkout **7.0.1**, setup-node **7.1.0**,
  upload-artifact **7.0.2**, setup-android **4.0.4**, action-gh-release **3.0.4**,
  setup-buildx **4.4.1**, build-push **7.4.0**, bake **7.5.0**, login **4.8.0**,
  setup-uv **10.3.3**, sbom-action **0.24.3**, scan-action **7.4.2**.
- **ArtCraft:** all 13 latest releases are unchanged: Photo **0.23.1**, Vector
  **0.24.1**, Film **0.20.1**, Light **0.21.1**, PDF CLI **0.21.1**, Effect
  **0.24.1**, Design **0.20.1**, Fig **0.18.1**, Word **0.18.1**, Sound **0.20.1**,
  Deck **0.18.1**, CAD **0.20.1**, Grid **0.19.1**. All **26** exact Linux asset
  SHA-256 values were checked against freshly fetched release API metadata.
  Existing installed x86_64 apps passed native schema and document/formula/CAD
  round-trip tests. aarch64 metadata is checked, **not executed**. Immutable old
  selections and queued turns keep their existing exact releases.
- **Models:** freshly queried `Qdrant/bge-small-en-v1.5-onnx-Q/main` remains
  **aa8f8b060edb00e03bfdd08813a2949946c8ba55**; actual download/load and offline
  semantic retrieval passed. Moonshine Voice **0.1.6** remains current. No model
  substitution, vector identity change or dated experimental result rewrite.
- **Waymark spike / skills:** dated experimental dependency/image pins reproduce
  existing evidence, not deployed services. Evaluate a new version by rerunning
  the experiment separately. Reviewed revision-pinned guidance/assets retain
  their complete source/license inventory, not automatic executable updates.

## Actual image security artifacts

Downloaded and inspected **all 40 SPDX/Grype JSON files** (38 artifact archives)
from successful [baseline CI 43729833456](https://github.com/StromFLIX/denkzeug/actions/runs/43729833456),
source **34fdcb56d6ad7ff7b5b35caf6f2a9e01eee6dc66**. Then downloaded and inspected
all **40** JSON files from successful [implementation CI 43798227269](https://github.com/StromFLIX/denkzeug/actions/runs/43798227269),
source **ab6feac8489c3d404378536a949d8a262fbb31ae**. The
[retained image finding ledger](dependency-audit-2026-11-16-images.json) excludes
retired image reports: **19** inventories and **1,897** distinct findings remain.
The original run had **20** inventories and **1,916** findings; its complete
artifacts remain available at the CI link above. No new advisory tuple appeared relative to baseline. No severity or
unfixed/CPE match was filtered. Multiple images/binaries can duplicate a finding.
This is actual CI image evidence, **not** a claim about every architecture or
the live staging/production filesystem. The baseline ledger remains in commit
ab6feac's original revision of this file; it was not rewritten in Git history.

Actual inventories confirm Python **3.13.18**, Node **24.22.3**, npm **11.21.2**,
uv **0.13.8**, PostgreSQL **17.12**, gosu embedded Go **1.27.4**, distro
BusyBox **1.37.0-r31** (legacy **-r30**), copied BusyBox
**1.38.0**, nginx **1.31.8-r1**, OpenSSL **3.0.22-1~deb12u2** /
**3.5.7-1~deb13u3** and Alpine libssl3 **3.5.9-r0**.

### Advisory dispositions (exact tuples remain in the ledger)

- **Temporal maintained server/tools:** embedded Go **1.26.10** still reports
  **GO-2026-6859**; Thrift 0.24 needs 0.25, compress 1.18.7 needs 1.20.2,
  x/net 0.60 needs 0.62, and x/crypto/OTel report additional fixes. Echo's
  **GHSA-246p-cpwv-v3jq / GHSA-99jh-6h7p-pp36** are still unfixed. No newer stable
  server/tools exist. Await official rebuilt images, then rerun schema/replay/
  restore gates. Do not binary-patch a stateful service or skip consecutive minors.
- **Collector/UI:** Go **1.27.3** still reports **GO-2026-6859**; UI has x/net
  0.60; Collector includes x/crypto/OTel findings. Source-only Collector 0.165.1
  is not an available deployed-image fix. Await official images and verify their
  actual embedded versions. An OS package upgrade does not rebuild Go modules.
- **Controller Docker/Buildx:** CLI29.9.3 uses Go1.27.4, but current Buildx0.39.0
  embeds Go1.26.10, Docker28.5.2 and go-archive0.2.0. Retain visible stdlib,
  Docker, x/crypto and **GHSA-hfg8-hc9c-6c3h** findings until official tool rebuilds;
  test the exact candidate-bundle controller build/rehearsal afterward. This is
  not authority to change the host daemon or privileged tool permissions.
- **Legacy server/tools:** old embedded Go1.25.11/1.26.4, SDK/Thrift/Echo/gRPC/
  JOSE/OTel/crypto/net/text findings remain. The exact1.29.7 images intentionally
  reproduce old-schema recovery, not normal deployed services. Await backports
  or separately rehearse a replacement recovery protocol. Retain ambiguous
  pseudo-version CPE matches rather than declaring them fixed; use only private
  disposable recovery networks. Do not invalidate the original restore test.
- **Newly observed upstream tuples** include OTel
  **GHSA-q7gw-24fr-5p69** (SDK/trace1.43–1.45, advertised fix1.47) and the legacy
  tools' AWS Lambda **GHSA-xmrv-pmrh-hhx2** (1.88.0 →1.88.5). These are embedded
  upstream module graphs, not workspace dependencies. Track the corresponding
  maintained/recovery parent images using the same verification above.
- **npm tool bundle:** nested glob13.0.6 **GHSA-3g3m-2fjr-6pw4** and
  minimatch10.2.5 **GHSA-g3hv-75wg-m249** persist in latest npm11, separate from
  the zero-finding frontend graph. Await npm11 patch or implement the explicit
  npm12 policy migration above; do not patch files inside npm or globally force
  a non-callable minimatch API into ASAR.
- **Distro packages:** all Debian/Alpine tuples remain visible. Debian vendor
  tracker data for curl/glibc/sqlite3/git/expat/OpenSSL was fetched anew. Most
  reports have no fixed supported-distribution revision; upstream version or
  future-release labels are not proof of an applicable vendor backport. Existing
  apt/apk upgrades stay enabled. Consume vendor fixes with pulled builds and
  inspect new reports; separately test a distribution migration if required.
- **Copied BusyBox1.38.0:** **CVE-2026-38753/38754/38755** lack reported fixes;
  Docker Hub/upstream still publish1.38.0, not1.38.1. Distro BusyBox has its own
  revisions/findings. Await the corresponding upstream/vendor patches, verify
  copied-binary fingerprints and health checks; do not conflate the two binaries.
- **Generic Python CPE:** **CVE-2025-15367 / CVE-2026-103995 / CVE-2026-116566**
  still cite3.15.0a6/3.16.0 fixes, not an available3.13 backport. Reconcile with
  upstream/distro trackers; retain the finding rather than selecting a future/
  prerelease interpreter. Clean pip-audit does not clear interpreter/OS findings.

No scanner policy, authentication, backup/schema gate, volume, credential or
runtime isolation setting was suppressed or weakened.

## Tests and coverage

Local Linux x86_64 evidence:

- Ruff lint and format; complete workspace pytest: **2,530 passed, 43 skipped**.
  The initial fingerprint mismatch was fixed with the canonical generator and
  the entire suite rerun, not ignored.
- Frontend **318 passed**, website **21 passed**, production Vite build; full
  `npm audit` **zero findings**. The initial concurrent install hit this sandbox's
  2GiB memory limit; serialized `npm ci` and tests completed successfully.
- Real agent/Pi transport/session/output/package/native suite: **327 passed,
  2 skipped**. Includes all five native schema/CAD editable-source+PNG/restore,
  Word DOCX/PDF/PNG and Grid formula tests. Inherited turn model-selection env
  polluted an initial invocation; rerunning serially without those turn-only
  overrides passed. No runtime policy was changed to obtain this result.
- Hash-verified speech install and **11 passed** PCM/session tests; isolated
  speech outdated list is empty. Workspace pip-audit: **92 external packages**,
  no known findings (five local workspace distributions are not on PyPI);
  speech pip-audit: **28 packages**, no known findings.
- Exact pinned search-model download/offline semantic retrieval: **1 passed**.
- Local Chromium159 ran real browser tests, not just resolution: initial subset
  **17 passed/2 timed out**; isolated account/compaction recheck **6 passed/1 timed
  out**; additional browser subset **33 passed/1 timed out**. These are **not a green
  browser certification**. Five-second reload/navigation readiness assertions
  were intermittent in this constrained sandbox. Full normal CI Chromium/WebKit
  remains mandatory; no skips, retries or weakened assertions were added.

Local Docker daemon/build/isolation, real PostgreSQL/MaxIO/Temporal migration,
WebKit OS libraries, physical mobile/macOS signing and aarch64 execution are not
available/verified here. Normal CI must exercise real images, offline compiler,
Docker isolation, PG17 existing-volume/non-root reuse, MaxIO signed transfers,
Temporal history/replay/backup/restore, all browser tests, and native ArtCraft.
Only the final pushed SHA's successful CI and normal staging Deploy (including
all smokes/post-hook/component checks) authorize completion. Verify that same
SHA and successful deployment UUID in Coolify. A queued job, old successful SHA
or HTTP200 alone is insufficient. Production stays separately release-gated.
Final run identifiers are reported after actual verification, never guessed here.

### Verified implementation candidate

The implementation commit **ab6feac8489c3d404378536a949d8a262fbb31ae** passed
all **24 CI jobs** in [43798227269](https://github.com/StromFLIX/denkzeug/actions/runs/43798227269),
including the full Chromium/WebKit suites, native ArtCraft, locked offline
compiler/runtime isolation, PostgreSQL volume/privilege reuse, MaxIO, and real
Temporal migration/history/replay/restore rehearsals. No browser skips, assertion
loosening or automatic retries were introduced to get this result. PostgreSQL's
actual inventory still records **17.12 / Go1.27.4**, and Pi's image records
**Pi1.5.2 / Playwright1.65.1**. The 1,916 retained image findings are **not**
claimed fixed by a clean application dependency audit.

The same SHA passed the normal [staging Deploy 43800154294](https://github.com/StromFLIX/denkzeug/actions/runs/43800154294),
including authenticated component/output-storage readiness, usage analytics,
workflow disable/result delivery, CLI/HTTPS MCP OAuth compatibility, real HTTPS/
WebSocket streaming dictation and creative-app artifact readiness. Coolify staging
application `tstmjujqyyl3zkvtwb0sx8nc` finished deployment
**oleflb3xnfvy65ef5o67rh3h** at that exact SHA. Android, iOS and Desktop build
workflows also succeeded for this SHA; these do not certify physical mobile
hardware, production signing/distribution or aarch64 execution.

This evidence-only follow-up updates the audit/index and artifact ledger, not the
implementation or image fingerprints. Its own final pushed SHA must still pass
normal CI and staging again; candidate success is not a substitute for that final
gate. Final SHA/run/deployment identifiers are returned by the maintenance task.
No stable release or production promotion was created.
