# A human workflow workspace

**Design proposal · not shipped functionality**

Reviewed against repository commit `43d7a4b` on 2026-11-15. Companion: [engineering plan](workflow-experience-plan.md).

## Product decision

Workflows are the place to understand a process, run it, and inspect what happened. A run is an execution of a workflow, not a second product area.

Keep one global **Workflows** destination. Within a workflow use **Overview / Runs / Definition / Settings**. Make **Run workflow** a persistent primary action, not a tab. Keep cross-workflow monitoring inside Workflows as **All runs**.

Success means a person can choose “Weekly summary,” select “Release assistant” and “Product updates,” run it, and understand the outcome without copying IDs, writing JSON, or losing the workflow context.

## Audit: what needs improvement

This is a source-level audit plus review of the checked-in product-tour screenshot, not a claim of a live-browser usability study. Relevant sources: `WorkflowPane.vue`, `RunPane.vue`, `NavRail.vue`, `SidePanel.vue`, `AgentPicker.vue`, `tests/workflow-inputs.spec.js`, backend workflow/run routers, and shared `manifest.py`.

| Before | After | Why |
| --- | --- | --- |
| Separate Workflows and Runs rail destinations | Runs inside the workflow workspace | Preserve process context and remove duplicate navigation |
| Flow is the default landing view | Overview: purpose, next execution, latest outcome, recent runs | Answer operational questions before exposing graph machinery |
| Run tab combines manual inputs, recurrence, delivery and trigger snippets | Run action opens a focused form; Settings owns persistent configuration | Avoid confusing one execution with unattended automation |
| Boolean/number controls, otherwise a text box | Schema-driven controls with visible types and allowed choices | Types and enums become discoverable before submission |
| Property keys are labels; descriptions often placeholders | Human title, persistent help, type, Required/Optional, explicit default | Guidance stays visible while typing |
| History rows emphasize generated execution IDs | Outcome, started time, trigger, duration and safe input summary | Let people recognize the execution they wanted |
| Run detail leaves the workflow and starts on Flow | Nested detail starts on outcome; Steps and Diagnostics are secondary | Explain success/failure before asking users to debug |
| Definition/run source selector duplicates run navigation | Definition is static; executed steps live in the selected run | Remove two competing ways to browse executions |
| Schedule editor uses the same input object as the manual form | Separate schedule and manual drafts, with an explicit copy action | Manual experimentation must not silently alter scheduled inputs |
| Successful start navigates to standalone Runs | Navigate to the new execution beneath the same workflow | Started is not completed; status remains visible |
| Existing agent picker also applies chat defaults | Dedicated resource combobox stores a reference only | Selecting an input must not mutate chat configuration |
| Recent history is limited to 25 entries | Filtered, paginated runs with recoverable detail links | Older executions remain discoverable |

## Information architecture

```text
Workflows
  Workflow list                 default: searchable list, latest outcome
  All runs                      monitor across visible workflows
  Weekly summary
    Overview                    purpose + next run + latest result
    Runs                        history + active executions
      Execution                 Summary / Steps / Diagnostics
    Definition                  Steps / Code / Manifest
    Settings                    Schedule / Delivery / Integrations / Controls
  Retained runs                 definitions removed; history still readable
```

“All runs” is an in-area view, not a new rail item. The sidebar continues selecting workflows while opening their runs. Do not render every execution in a permanently expanded sidebar tree. Draft review remains a separate state with its existing validation/diff tools; new deployment does not acquire an approval gate.

## Layout alternatives and recommendation

| Direction | Layout/interaction axis | Benefit | Cost |
| --- | --- | --- | --- |
| Context-first workspace — recommended | Workflow list plus full-width detail | Fits existing shell, works at 125% interface scale, preserves navigable URLs | One extra click to inspect a run |
| Operations table | Cross-workflow execution table as default | Best for operators monitoring many concurrent jobs | Makes the process and its inputs secondary |
| Three-pane inspector | Workflow list, run list, persistent run inspector | Fast comparison of many runs | Too cramped on laptops and at enlarged text sizes |

Adopt context-first, with All runs available for the operator case. Use full-width nested run detail rather than a mandatory third pane. This is one recommended direction with alternatives evaluated, not an interactive prototype or user-tested final design.

## Wireframe: workflow overview

```text
Workflows            | Weekly summary                         [Run workflow]
[Search workflows]   | Collect product updates into a focused weekly report.
                     | Overview   Runs   Definition   Settings
All workflows        | ---------------------------------------------------
All runs             | Next execution
                     | Monday, 09:00 Europe/Berlin            [Edit schedule]
Weekly summary       | Enabled. Schedule synchronized.
Morning digest       |
Release preparation  | Latest result
                     | Completed · Today, 09:02 · Scheduled
                     | 12 updates summarized. Delivered to Product updates.
                     |                                            [View run]
                     | Recent runs
                     | Completed  Today, 09:00     Scheduled       2m 14s
                     | Failed     Yesterday       Manual             8s
                     | Completed  Monday          Scheduled       1m 52s
                     |                                            [View all]
```

Do not imply delivery succeeded merely because execution completed. Display “Delivery pending” or “No reply requested” when that is the recorded state. No invented outcome summary for old runs lacking one; show “Completed” and link to saved output.

## Wireframe: run form

A route-backed dialog on desktop; a full-height sheet/page on mobile. An open form is not an invitation to start execution. Closing a dirty form warns before discarding changes.

```text
Run Weekly summary                                      [Close]
Run once with these inputs. Scheduled inputs are unchanged.

Report type                         String · Required
[ Weekly summary                                      v ]
Allowed choices: Weekly summary, Release notes, Incident review

Agent                               Agent · Required
[ Release assistant                                   v ]
Uses this saved agent for the report.     [Search by name]

Destination                         Chat · Optional
[ Product updates                                     v ]
Send the report to this conversation.

Look back                           Integer · Optional
[ Use workflow default (7)                            v ]
Days of activity to include. Range: 1–30.

Preview only                        Boolean · Optional
[ Use workflow default (true)                         v ]
No external changes while previewing.

[Advanced input JSON]            [Use scheduled inputs]
                                       [Cancel] [Run workflow]
```

Required fields show “Required,” not only an asterisk. Optional values expose **Use workflow default**, **Not supplied**, **Set value**, and **Set null** only where the schema permits those states. Empty text is a value, not a reset action. Enum labels can be friendly, but selection retains the exact JSON value, including numeric or Boolean choices.

Explicitly choosing a displayed schema default materializes that value in this request; merely leaving “Use workflow default” selected omits the key. Help must explain that JSON Schema defaults are descriptive in the existing runtime: workflows must implement their own omitted-input behavior. A required field must be supplied even if it declares a default. Warn during authoring when a default is misleading; do not change legacy execution semantics silently.

Advanced JSON edits and generated fields use the same draft, with parse/validation errors beside the editor. Unsupported schemas get a clear JSON fallback, never a silently lossy text field. Fields and raw JSON retain data when toggling views. Do not include credentials in previews or human-readable summaries.

## Wireframe: resource search

```text
Agent                               Agent · Required
[Release assistant                                    v]
+-------------------------------------------------------+
| Search agents: [release_____________________________]  |
| Release assistant                 Release automation  |
| Release assistant                 Docs team · …7a91    |
| Release reviewer                  Reviews only        |
|                                                       |
| More results                                 [Load more]|
+-------------------------------------------------------+
```

Search names first; descriptions and safe ID suffixes disambiguate duplicates. Full IDs can be copied from details but are not the primary text. Fetch options on demand with bounded server-side search; do not preload every chat. Preserve the selected item even when it is outside search results. A rename updates the label, not the selection.

Loading, no matches, failure/retry, access revoked, and deleted selections have distinct messages. Never erase a stale selection or choose a different resource automatically. A failed lookup blocks reference validation but keeps the draft. “Unavailable selection — choose another” must not reveal the title of another user's resource.

## Wireframe: execution inside its workflow

```text
Weekly summary / Runs / Today, 09:02                 [Run again…]
Failed · Manual · Started 09:02 · Duration 8s
Summary   Steps   Diagnostics
--------------------------------------------------------------
Could not read the update source.
The connected source rejected the request before a report was sent.
[Inspect failed step]                 [Edit inputs and run again]

Inputs
Report type     Weekly summary
Agent           Release assistant
Look back       7 days

Output
No report produced.

Delivery
Failure saved to Product updates.             [Open conversation]
```

“Run again…” opens a new form with historical inputs; it does not retry side effects immediately. Validate against the current definition and flag changed/removed fields. Show “Definition changed since this execution” where revision evidence exists. Saved result remains readable after detailed Temporal history expires. Cancellation means “Cancellation requested,” not “Cancelled”; hard termination stays in privileged diagnostics with its consequences explained.

## Settings separation

- **Schedule:** recurrence, explicit IANA timezone, next execution and its own typed inputs. A saved schedule should expose inputs for inspection. Clearly distinguish paused, disabled, synchronization pending and unavailable schedule metadata. Never translate an unavailable scheduler into “No schedule.”
- **Delivery:** retain One chat / A chat per run. Adding arbitrary output destinations is a separate delivery feature; a chat input reference does not itself change delivery routing.
- **Integrations:** authenticated trigger instructions and safe payload preview. No token in the page, URL, clipboard preview or design fixture.
- **Controls:** Enable/Disable and Delete; preserve synchronization failure/retry behavior. Disabling does not cancel active runs. Deleting a definition retains historical executions.

## Visual and interaction specification

Keep Denkzeug's existing visual language, not a new automation dashboard brand.

| Role | Baseline token/value | Use |
| --- | --- | --- |
| Workspace | `--surface-app` / #f8f9fc | Main canvas |
| List surface | `--surface-rail` / #f0f2f8 | Sidebar |
| Content | `--surface-panel` / #ffffff | Inputs, contained detail |
| Text | `--text` / #20232c | Hierarchy |
| Action | `--accent` / #4b5fc0 | Focus/selection |
| Workflow identity | `--workflow-color` / #875b26 | Process marker, not every status |

Sora for interface text, existing monospace for code/IDs only. Baseline type sizes: title 1.25rem, body 0.875rem, secondary 0.8125rem; retain configurable 125% root scale. Left-align content; descriptions below 65ch. Use the existing spacing scale and 0.5rem control radius. Borders encode sections; do not turn every fact into a large identical card. Status always includes text, not color alone. Reuse themes, Quasar primitives and `data-skin-part` hooks.

Keep selection and keyboard navigation instant. Optional pointer-open dialog/popover transitions can use 160–200ms transform/opacity and origin-aware easing; no list staggers or decorative graph entrances. Respect application and system reduced-motion settings.

## Accessibility and responsive behavior

- Label, help and error associated with each control. Inline field errors plus focused error summary on submission; preserve all entered values.
- Combobox/listbox semantics, active descendant, arrow keys, Enter, Escape; announce loading/result counts without stealing focus. Accessible name includes selected label and type.
- Focus trap for modal, return focus to Run action, visible focus, no background tab stops. Run-submit cannot fire while selecting a resource with Enter.
- 44px touch actions; no hover-only operations. At 390px, single column form and vertically stacked run metadata; no body horizontal scroll.
- At 900px and below use the existing list/detail back pattern; selected workflow and run filters survive back navigation.
- Verify night/light themes, 100/125/150% interface size, long names, empty help, localization and keyboard-sized viewport. Keep actions reachable without overlaying fields.

## Validation of the direction

The proposal deliberately rejects a generic metrics dashboard and an always-open three-column inspector: neither improves the primary job of safely selecting inputs and understanding a process. The memorable element is continuity of workflow context, not decoration.

Before release, test five tasks with representative users: find a failed run; choose an enum without reading code; find an agent among duplicate names; run manually without changing the schedule; inspect a retained run after its definition is deleted. Record completion, wrong selections, navigation backtracking and confidence. Proposed targets: all tasks complete without IDs/JSON for supported fields; no unintended schedule edits; no automatic reruns; keyboard-only parity. These are release criteria, not measured results.
