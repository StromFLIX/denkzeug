# Usage

Open **Settings → Run & manage → Usage** to explore token consumption. The page uses existing database observations; opening it never calls a model or provider.

- Reported token total with uncached input, output, cache read and cache write shown beside it.
- Model response and agent execution counts.
- **Group by** applies to both the selected-range token bar and the daily stacked chart: model, owner (administrators), workload, saved agent or workflow. The largest four groups have their own colors; remaining groups are included in **Other**. Filter to see any individual group on its own.
- Select a daily bar to see its counts without changing the range totals. On mobile, filters collapse and a day picker makes narrow bars easier to inspect. Ranges over 90 days stay daily and scroll horizontally.
- Last 7/30/90 days or a custom range of up to 366 days.
- Filters for account owner (administrators), workload, model, saved agent and workflow.
- Breakdowns by model, agent, workflow, account owner (administrators) or execution.
- Workflow rows open a run-level breakdown including saved-agent task chats and delegated work. Execution and run links retain existing private-resource permissions.
- Filtered CSV export, up to 10,000 rows, using the same scope and selection as the page. Desktop, Android and iOS use the shared file-download flow.

Filters, range, chart grouping, table breakdown, sorting and pagination are in the URL. Chart grouping does not change the detailed table or its CSV export. Dates use the browser's IANA timezone. Missing token counts display as a dash; known zero values display as zero. No coverage panels or billing estimates are shown.

## HTTP API

Both endpoints require a signed-in **human session**, including native bearer sessions. An API key or delegated service cannot use a broad settings grant to obtain workspace analytics.

- `GET /api/settings/usage`: summary, chart buckets, facets, and a paginated breakdown.
- `GET /api/settings/usage/export`: the complete filtered breakdown as CSV, not only the displayed page. Exports over 10,000 rows fail with a narrowing instruction rather than silently truncate.

Parameters:

| Parameter | Values |
| --- | --- |
| `start`, `end` | Inclusive calendar dates, default last 30 days |
| `timezone` | IANA timezone, default UTC |
| `group` | Table and CSV: `models`, `agents`, `workflows`, `users`, `executions`, `runs` |
| `chart_group` | Summary endpoint only: `models`, `users`, `workload`, `agents`, `workflows`. If omitted, no grouped chart is returned. |
| `user`, `model`, `agent`, `workflow` | Stable IDs; empty means all visible values |
| `workload` | `chats` or `workflows` |
| `sort` | `tokens` (default), `newest`, `name` |
| `offset`, `limit` | Summary endpoint only; default 0 / 50, maximum page size 100 |

When `chart_group` is specified, `chart.series` lists the range totals and labels in chart order. Each `daily` row contains `series`, an array of counts in that same order. Grouped counts cover the full filtered range, independent of table pagination. Days without any reports retain `tokens: null`; zeros in `series` mean that no reported tokens contributed to that series on that day.

Members see only their own resource-owned observations, facets and exports. Only administrators can select other owners or request the owner grouping in charts or tables. Administrators receive metric metadata, not private prompts, chat titles, tool results, jobs or credentials. Links into another account's chats/runs are not returned. Both responses are `Cache-Control: no-store`.

## Accounting and implementation

`services/backend/nautionette_backend/usage.py` reads a consistent, date-bounded SQLite snapshot. Supporting indexes cover accepted message dates, turn/message joins, response events and run dates. No new provider telemetry or accounting payload collection is introduced.

- Chat usage is summed across persisted response events. Repeated terminal-result usage is not added. Last-result-only historical usage is used once when no usable response reports exist.
- Model changes within a native execution retain separate model subtotals without multiplying execution counts.
- `input`, `output`, `cacheRead`, `cacheWrite` use the existing `run_statistics.token_counts()` validation. Legacy provider-context input is normalized by subtracting cache reads/writes; output already includes reasoning.
- Legacy activity-agent attempts come from `workflow_calls`; direct MCP attempts are not model token counts.
- Saved-profile workflow task chats and nested delegation use server provenance/root-turn linkage. Workload buckets are exclusive; alternative breakdowns never add consumption again.
- Owner IDs are account IDs from owned resources, not `chat_turns.user_id` (which references an input message).
- Dates are the accepted session input date or the workflow activity run's start date. Run drilldown groups observed work by workflow execution ID.
- Unknown usage remains null rather than a fabricated zero. CSV protects labels against spreadsheet-formula injection.

Tests: `tests/backend/test_usage.py`, `services/frontend/src/usage.test.js`, and `services/frontend/tests/usage.spec.js`. CI includes the backend and browser suites explicitly.
