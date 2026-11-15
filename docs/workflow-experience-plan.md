# Workflow experience: engineering and delivery plan

**Proposed implementation · no runtime changes in this planning commit**

Companion: [UX audit and wireframes](workflow-experience-design.md). Baseline: `43d7a4b`, reviewed 2026-11-15. This plan covers typed/enum inputs, dynamic resource fields, and one workflow/run workspace. It does not replace Temporal or introduce a no-code workflow builder.

## What exists, and what does not

| Area | Verified implementation | Gap |
| --- | --- | --- |
| Input validation | `libs/nautionette/src/nautionette/manifest.py` uses Draft 2020-12 JSON Schema in `input_problems` | Deploy validation checks the outer manifest but does not explicitly `check_schema` the embedded input/output schemas |
| Enum enforcement | Run-start and schedule-save paths invoke JSON Schema validation | Frontend ignores enums and branch labels |
| Typed form | `WorkflowPane.vue` renders Boolean and numeric controls; tests protect false/zero/string preservation | No field titles, enum controls, structured types, explicit null/empty-string semantics or field-local errors |
| Defaults | Validation does not materialize JSON Schema defaults; workflows receive the submitted payload | UI says “Use default”; do not mistake that for runtime default application |
| Manual/schedule payload | `inputs` is initialized from schedule input; `payload()` drops `''` and nullish values | Separate drafts; model missing/null/empty explicitly |
| Agent selection | `AgentPicker.vue` searches an already loaded catalog and applies a profile to a chat | Need an input-only selector with no profile-application side effect |
| Resource access | Agents use `access.visible`; chats and runs are owned | No generic workflow-field option/resolve contract |
| History | Workflow detail embeds 25 owned runs; global API lists a bounded set and decorates it from Temporal | Need pagination, filtering, stable sorting, compact summaries, parent-aware URLs |
| Retention/deletion | `db.forget_workflow` removes settings, not runs; retained results survive expired Temporal history | Unified UI must handle removed definitions and old links |
| Lifecycle | Disable/schedule sync, durable result outbox, silent success, native chat context already implemented | Reuse these; redesign must not weaken them |

## Decision 1: JSON Schema remains the execution contract

Use the existing manifest `inputs` JSON Schema rather than inventing a second type system. Add strictly validated optional presentation/reference metadata inside property schemas. Keep manifest schema version 1 for additive optional metadata; unknown top-level extensions must still use `x_`.

Proposed authoring example (metadata is not implemented yet):

```python
MANIFEST = {
    "schema": 1,
    "name": "weekly_summary",
    "title": "Weekly summary",
    "inputs": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "report_type": {
                "title": "Report type",
                "type": "string",
                "enum": ["weekly", "release", "incident"],
                "x_ui": {"enum_labels": ["Weekly summary", "Release notes", "Incident review"]},
            },
            "agent_id": {
                "title": "Agent",
                "description": "Saved agent that prepares the report.",
                "type": "string",
                "x_resource": {"kind": "agent"},
            },
            "destination_chat_id": {
                "title": "Destination",
                "description": "Conversation passed to this workflow, not automatic delivery configuration.",
                "type": ["string", "null"],
                "x_resource": {"kind": "chat"},
            },
            "days": {
                "title": "Look back",
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 7,
            },
            "preview_only": {"title": "Preview only", "type": "boolean", "default": True},
        },
        "required": ["report_type", "agent_id"],
    },
    "outputs": {"type": "object", "properties": {"summary": {"type": "string"}}},
}
```

The workflow still implements omitted `days`/`preview_only` behavior, for example `payload.get("days", 7)`. A resource annotation does not invoke an agent, send a message or grant tool permissions. The workflow must explicitly consume that ID through existing authorized helpers.

### Supported controls and honest fallback

| Schema | Control | Serialization rule |
| --- | --- | --- |
| `enum` of scalar values | Select, searchable for a long list | Keep original JSON value, not DOM option text |
| `oneOf` of distinct scalar `const` branches + titles | Labeled choice control | Use constant values; other unions use JSON fallback |
| `const` | Read-only value | Materialize required constant or explicitly choose optional value |
| `boolean` | Clear Yes/No choice + omission/default mode | Preserve false |
| `integer` / `number` | Numeric control with bounds and help | Reject NaN, infinity and fractional integers; preserve zero |
| `string` | Text / textarea when requested | Preserve empty string and whitespace unless schema explicitly disallows it |
| `string` with date/date-time format | Typed date control with explicit format/timezone help | Only ship when conversion and server format validation agree; otherwise text + format hint |
| `type: [T, 'null']` | Base control + explicit null mode | Null is different from missing |
| Simple object / array | Nested field group / repeatable item editor | Preserve structure, validate children, never stringify into a scalar |
| Array of scalar enum or resource references | Multi-select in later increment | Preserve typed items and ordering; obey min/maxItems and uniqueness |
| Unsupported composition or reference | Validated Advanced JSON with explanation | No silent flattening, dropping keys or coercion |

Enum handling takes precedence over type-based rendering. Labels are presentation only; `enum_labels` must match enum length. Complex enum values use the JSON fallback. Keep baseline compatible: required flags, description, order from authored properties and default annotations work without `x_ui`.

Validate embedded schemas at deployment, report paths, and reject invalid metadata. Start with bounded top-level resource fields; recursively validate metadata but clearly mark nested resource lookup unsupported until the renderer and shared validator support it. Do not put metadata only in the browser; non-browser callers need the same reference checks.

### Draft and error model

Represent each field as `{mode: 'omit' | 'value' | 'null', value}`. Encode omission by key absence, not undefined in an API payload. Use a pure form adapter shared by manual, schedule and trigger-preview surfaces. Preserve `false`, `0`, `''`, `null`, empty arrays and objects exactly. Required fields cannot use omission; a default annotation alone does not satisfy required.

Separate `manualDraft` and `scheduleDraft`; explicit “Use scheduled inputs” copies, never binds them. Reopening a form must not overwrite intentional edits during resource refresh. Version requests when navigating between workflows; late responses cannot apply to a new form.

Return structured field errors alongside existing human-readable errors, additively:

```json
{
  "workflow": "weekly_summary",
  "input": ["report_type: value is not an allowed choice"],
  "field_errors": [
    {"path": "/report_type", "code": "enum", "message": "Choose an allowed report type."}
  ]
}
```

Keep existing status/error shape compatible for MCP/API consumers. Escape JSON Pointer keys correctly; do not include private resource names or secret values in errors. Client validation improves immediacy; server validation remains authoritative. Choose a compatible maintained validator after checking the supported 2020-12 subset and bundle cost; do not write an ad-hoc partial validator that claims full JSON Schema support.

**Default policy:** preserve legacy omission semantics. Do not globally apply defaults in `input_problems`; that changes scheduled, webhook and agent behavior. If runtime default application is later desired, make it an explicit versioned authoring contract and persist effective input/revision. Displayed defaults may be explicitly chosen as request values now.

## Decision 2: controlled resource providers, not arbitrary endpoints

Ship agent and chat providers first; extend a registry to projects/workflows later. Do not add arbitrary URL templates, executable expressions or workflow-provided JavaScript for options. This avoids SSRF and leaking authorization into presentation configuration.

Proposed endpoint contract:

```text
GET /api/workflows/{name}/input-options?field=/agent_id&q=release&limit=20&cursor=...
GET /api/workflows/{name}/input-options?field=/agent_id&selected=<id>
```

The backend reads the deployed schema for this field. Client cannot choose a different kind, visibility scope or filter. Exact-selection lookup is distinct from search and supports restoring a selected value without loading every option.

```json
{
  "items": [
    {"value": "agent_stable_id", "label": "Release assistant", "description": "Release automation"}
  ],
  "next_cursor": null,
  "selected": null
}
```

Specify maximum query length, field pointer depth, page size (default 20, maximum 50) and opaque cursor bounds. Sort case-insensitively by display name with stable ID tie-breaker. Search is parameterized and scoped before results/counts/cursors are returned. Selected lookup returns available/unavailable generically, without distinguishing another user's resource from deletion.

### Access and lifecycle rules

1. Authorize workflow visibility and resource visibility independently, through existing access helpers. Listing a reference grants no right to execute it.
2. Chats come only from the caller's permitted ownership scope. Agents come from the caller's allowed catalog, not the admin's full catalog. Test API-key and member restrictions, not just admin login.
3. Run-start and schedule-save revalidate IDs in a shared server service. Manual/webhook/MCP/internal entry points must all reach the validation fence. A caller cannot bypass it by submitting a guessed ID directly.
4. Schedule execution bypasses the interactive run-start path via Temporal. Before scheduled resource fields ship, trace and implement revalidation under the stored schedule's execution principal at the activity boundary before any external effect. Do not use ambient admin authority or call SQL/network from deterministic workflow replay. Existing agent helper authorization remains a second fence.
5. Until that schedule ownership/revalidation path is demonstrated by integration tests, do not enable scheduling workflows with resource fields. Show the limitation clearly; reject unsafe schedule saves server-side. Never silently downgrade reference validation.
6. Revocation/deletion blocks future effects with an actionable error. Never choose a replacement ID. Reference validity at save time is not a permanent authorization grant.
7. Names can change; IDs remain the payload. Avoid persisting chat titles into world-readable run metadata. Historical label snapshots, if added, are private and never override current authorization.
8. Bound all resource lookups and summaries. Search caches are scoped by account, workflow/schema revision and field; clear on logout/access change. Discard out-of-order search responses; debounce around 200ms. Do not cache credentials or entire chat payloads.

Prefer a dedicated `WorkflowResourceField` using existing Quasar primitives and shared avatar rendering. Do not reuse `AgentPicker` wholesale: its “Global defaults,” reset/reapply and profile-application actions belong to chat configuration. No new UI library is required for this feature.

Dependent field queries (for example project → repository branch) are explicitly out of the first increment. Add later with declarative, allowlisted dependency pointers and cycle validation, not freeform expressions. Changing a parent invalidates rather than silently clears a child selection.

## Decision 3: consolidate navigation, not persistence

Keep the existing `runs` table, Temporal execution identities, MCP run tools and `/api/runs` endpoints. The consolidation is information architecture and composition, not a database merge or breaking API rename.

Proposed canonical UI routes:

```text
/workflows                              workflow list
/workflows?view=runs                    all owned/visible runs
/workflows/:name                        Overview
/workflows/:name/runs                   workflow run list
/workflows/:name/runs/:id               execution detail
/workflows/:name/definition             definition/graph/code
/workflows/:name/settings               persistent controls
/workflows/:name?run=new                route-backed manual run form
/workflows/retained/runs/:id            removed definition's retained execution
```

Reserve static route segments such as `retained` explicitly and resolve conflicts with existing workflow names before routing; if a collision exists, use a non-name prefix such as `/workflows/-/retained/runs/:id` instead. Routes in this plan are proposed, not currently available.

Use a single shared run-detail component behind canonical and compatibility routes. Keep `/runs/:id` as a resolver: authorized lookup finds the parent, then redirects; if definition is gone, show retained context. `/runs` redirects into All runs. Access-denied/missing run does not leak a parent name. Validate that an ID in `/workflows/:name/runs/:id` actually belongs to that workflow. Parent visibility must not grant access to someone else's execution; revoked parent access must not destroy legitimate owned retained-history access.

Map old workflow query tabs (`Flow`, `Run`, `Code`, `History`) into Definition, run dialog, Definition/Code and Runs. Handle draft `Code diff` separately. Preserve browser back/forward, shared deep links, settings/search contributions, command palette entries and message-origin links. Remove the Runs rail item only after those paths work. Do not rename backend `workflow_id` just because the UI calls it “execution.”

### Run list API

Extend the existing run list API additively with workflow/status/trigger/time-range filters, bounded limit and opaque cursor. Preserve the old response fields. Return compact owned summaries, not all inputs/results for every table row. Cursor order is `(created_at DESC, id DESC)` to prevent duplicate/missing rows for equal timestamps; authorize before pagination and counts.

Do not join every listed run to a blocking Temporal call. Persisted terminal state/result is the primary source; live overlays are bounded and failures remain “status unavailable,” not falsely completed. Filter semantics must use a defined status source rather than filtering stale SQL while displaying conflicting live status. Existing event/reconciliation paths refresh active rows. Paginate older runs even when Temporal has expired.

Overview needs an additive, bounded workflow summary endpoint or list enrichment for latest execution/active count. Avoid an N+1 detail fetch per workflow and avoid exposing other users' run counts. Start with private latest-run summaries; add richer metrics only if they support a demonstrated user task.

Revision and delivery summaries must use recorded evidence. Newly started runs can retain definition revision/hash and schema snapshot through an additive migration; historical runs without those fields show “Revision unavailable.” Historical label resolution must never reinterpret old inputs as new-schema values. Do not backfill old results by rerunning anything.

## Component and file boundaries

| Existing surface | Planned responsibility |
| --- | --- |
| `libs/nautionette/.../manifest.py` | Embedded-schema checking, metadata validation, structured schema errors |
| Backend `runs.py`, workflow/run routers | Shared input/reference validation and bounded run summaries |
| Backend new `workflow_input_options.py` (proposed) | Allowlisted resource providers, search/resolve, authorization |
| Frontend new `workflow-inputs.js` (proposed) | Pure schema-to-control adapter and lossless payload modes |
| Frontend new `WorkflowInputForm.vue` (proposed) | Shared typed form, errors, JSON fallback |
| Frontend new `WorkflowResourceField.vue` (proposed) | Async accessible reference combobox |
| `WorkflowPane.vue` | Workflow shell/overview; extract schedule/settings instead of growing one monolith |
| `RunPane.vue` | Extract reusable run detail retaining statistics, graphs and expired-history notices |
| `NavRail.vue`, `SidePanel.vue`, `router.js`, `store.js` | Single workspace, link compatibility, scoped refresh/pagination |
| Settings Automation pane, registry, message-origin/command search | Correct nested links and terminology |
| Workflow authoring docs and agent instructions | Teach title, enum and safe resource annotations; existing files remain valid |

New filenames are suggested seams, not a requirement to create empty abstractions. Follow established Vue Composition API, existing CSS tokens, account fixture and backend access patterns.

## Delivery backlog and gates

Implement and stage bounded increments. Do not combine all work into an unreviewable navigation/form/security rewrite.

| Increment | Work | Exit gate |
| --- | --- | --- |
| WFX-1: typed inputs | Pure form adapter; enums/labeled constants; titles/help/type; explicit omission/null; structured field errors; embedded-schema validation; separate drafts | Existing Boolean/zero tests pass; typed enums and empty/null round-trip; manual edits cannot change schedule draft; unsupported schema fallback retains data |
| WFX-2: resource references | Metadata registry; agent/chat search + exact resolve; dedicated combobox; shared start/save validation; schedule-principal spike and fence | Member/API-key isolation, direct-ID rejection, rename/deletion/revocation, out-of-order responses and keyboard tests pass; no unattended reference execution before validated authority |
| WFX-3: unified workspace | Nested shell, Overview/Runs/Definition/Settings; run dialog; old URL resolver; paginated history; All runs; retained-definition states | Old links and back/forward work; authorized parent matching; retained runs/statistics/expired history readable; no separate Runs rail entry |
| WFX-4: operational polish | Outcome-first run detail, recorded delivery state, revision-aware Run again, status/source consistency, responsive/theme pass, authoring documentation | Live/cancel/failure states truthful; no automatic side-effect retry; user-task and staging lifecycle checks pass |
| WFX-5: structured/dependent fields | Nested arrays/objects, multi-select, additional resource kinds, allowlisted dependencies | Shared schema/reference validation covers new shapes; complexity never degrades legacy fallback |

WFX-1 can land independently. WFX-2 and the WFX-3 shell can proceed after adapter/API seams are stable, but WFX-2's authorization work is a hard release gate. WFX-4 cannot label results “delivered” without outbox evidence. No calendar estimate is asserted without the schedule-principal investigation and prototype feedback.

### Test matrix

- **Schema/unit:** enum strings/numbers/booleans; nullable scalar; const branches; default annotation versus required; `false` versus `'false'`; `0`; empty string/null/missing; min/max/pattern/length; invalid schema/metadata; unsupported `$ref`/composition fallback; escaped field paths.
- **Authorization/backend:** agent and chat visible subsets; duplicate titles; selected ID not on page; guessed ID; unauthorized workflow/field; schema changed during selection; bounded pagination; removed workflow with retained runs; cross-parent URL mismatch; API-key scopes; schedule owner revocation. No SQL/network during replay.
- **Browser:** enum choices visible without Code; field-local errors retain draft; manual/schedule independence; JSON/form toggling; missing selection stays visible; resource fetch failure has retry; rapid search discards stale results; keyboard dialog/combobox/Enter behavior; account switch cache clearing; start success navigates beneath workflow; failed start stays on form.
- **Navigation/retention:** old `/runs` links and workflow query tabs; mobile back; filters survive detail navigation; load-more stable with equal timestamps/new runs; expired history and deleted definition; live row updates without focus movement; statistics accessible.
- **Visual:** capture desktop, 390px mobile, short-height/keyboard, light/night and 125/150% sizes. Test long/localized labels, 44px targets, focus and reduced motion. Compare actual screenshots to wireframes; checked-in marketing screenshots are not acceptance tests.
- **Lifecycle integration:** retain existing Disable/Enable/schedule-sync, cancellation, silent success, delivery deduplication and native-context tests. Add a pure no-external-effect workflow exercising enum/resource validation in isolated staging; run to terminal state and inspect history/results, not just a start response.

### CI, staging and rollback

Each runtime increment: targeted tests → full relevant CI → configured local Git commit/co-author verification → push `HEAD:refs/heads/main` without force → verify exact-commit CI and pipeline-owned staging deployment → authenticated smoke and visual review. Do not bypass the pipeline with manual Coolify deployment, clone production data or start copied automation. Check deployment history and health; a finished deploy is not successful staging verification by itself.

This planning commit changes documentation only. Verify links/format and CI; inspect whether the deployment pipeline deploys or explicitly skips docs-only changes. No runtime smoke can validate an unimplemented feature, and no production release is implied.

Rollback UI by reverting the bounded increment and retaining old route/API compatibility. Prefer additive metadata and database migrations; leave old runs readable. Rollback must not reactivate paused schedules, discard history, reset inputs, broaden resource authority or replay notifications. An unsupported resource annotation must fail safely, not degrade into an arbitrary editable reference.

## Definition of done for this initiative

A nontechnical user can see input types and valid enum choices, search permitted agents/chats by display name, submit exact typed values, inspect runs in workflow context, and change a schedule without contaminating manual inputs. Old links, ownership, lifecycle, retained history, themes, keyboard operation and staged exact-commit deployment remain intact.

This commit establishes the researched design and delivery backlog. The first implementation slice is WFX-1; the separate user-visible resource/scheduling authority fence is the next technical investigation. Interactive prototype testing and measured user outcomes remain future work, not claims of this proposal.
