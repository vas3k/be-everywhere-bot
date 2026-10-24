# Agent instructions

## Workflow and permissions

- Read the relevant code and `git status` before editing. Preserve unrelated and staged work; do not reset, reformat, or refactor it as part of another task.
- Do not commit, push, deploy, or apply database migrations without explicit user consent. Permission to edit code does not authorize these operations.
- Add or run tests only when explicitly requested. Builds, type checks, linters, and smoke checks also require an explicit request; commands below are reference, not instructions to execute them automatically.
- Review the diff and affected callers, templates, and configuration. Report what changed, which checks actually ran, and any remaining limitations.
- Do not add every change you made to README.md or AGENTS.md. Only change them if facts were changed or if directly requested.

## Code conventions

- Use ordinary Django views, forms, models, queries, and template tags. Keep logic in the relevant domain app; do not introduce service/repository layers or generic frameworks for a local feature.
- Prefer small duplication to premature extraction. Do not create a helper used in fewer than three places **or** containing fewer than five meaningful lines. These thresholds are necessary, not sufficient: extraction must improve reading. Do not pad functions or invent callers to meet them.
- For Python/Django helpers, keep a helper used only within one file in that file, with a descriptive name and no leading underscore. When multiple files use it, put it in `helpers.py` in the app that owns its meaning and import it from there.
- Reusable frontend helpers belong in `frontend/src/lib/`. Keep shared browser helpers such as `setCookie` and `csrfToken` there and import them instead of duplicating their implementation; these may be short.
- Do not introduce `_underscore`-prefixed helpers or disguise ordinary logic as protocol methods. Prefer conventional, descriptive names; protocol methods have no blanket exemption and should be used only for their actual language/framework purpose. Required views, callbacks, React components, and framework hooks are not utility helpers.
- Keep `__init__.py` files empty or minimal: necessary imports, singleton instances, and genuine package initialization only. Keep helpers and business logic in named modules. Prefer conventionally named functions for explicit setup operations where possible, subject to the helper-extraction rule above.
- Separate semantic blocks with an empty line: input, validation, queries, mutation, response. Keep related statements together; do not add a blank line after every statement. Prefer explicit control flow over dense one-liners. Break code at meaningful boundaries when it helps reading, not just to enforce a fixed line width. Match surrounding indentation and avoid unrelated formatting changes.
- Do not hard-wrap Markdown prose to a fixed column width. Keep each paragraph or list item on one source line and separate semantic blocks with an empty line (two newline characters). Preserve the line structure of tables and code examples.
- Make code self-documenting through clear names, explicit steps, and logical line breaks, even when this takes more lines. Comments are for human readers, not an agent's notes to itself. Add them only for genuinely tricky behavior, non-obvious constraints, or reasoning the code cannot express. Do not narrate each line, document every function, or add boilerplate docstrings and LLM filler.
- Reuse existing dependencies and patterns. Existing exceptions do not justify repeating them or launching a repository-wide cleanup.

## Project map

A single-user, project-scoped social sync bot built with Python 3.13+, asyncio, httpx, SQLAlchemy Core, SQLite, and Textual. Dependencies use uv. There is no web server, Django app, frontend, ORM, or task queue; the framework-specific conventions above apply only if those technologies become relevant.

Read the relevant implementation and behavior tests before editing. Use this map to find code; do not duplicate provider setup instructions from README or append release history here.

| Area | Implementation | Tests |
|------|----------------|-------|
| CLI, scheduled watch, backfill | `main.py`, `config.py` | `test_main.py`, `test_projects.py` |
| Terminal UI and forms | `tui/app.py`, `tui/forms.py`, `tui/app.tcss` | `test_tui_workflows.py` |
| Sync orchestration and retries | `sync/engine.py`, `db/deliveries.py`, `db/posts.py`, `db/sync_state.py` | `test_sync_behavior.py` |
| Thread eligibility and formatting | `utils/filters.py`, `utils/thread_processor.py`, `apis/types.py` | `test_thread_behavior.py`, `test_utils_*.py` |
| Provider auth, fetch, publish | `apis/<network>.py`, `apis/registry.py` | `test_auth_behavior.py`, `test_<network>.py` |
| Projects, accounts, roles | `db/projects.py`, `db/accounts.py`, `db/helpers.py` | `test_projects.py`, `test_account_roles.py`, `test_account_removal.py` |
| Schema and upgrades | `db/schema.py`, `db/connection.py`, `db/migrations/` | `test_migrations.py` |
| Media and token renewal | `utils/media.py`, `utils/meta_tokens.py` | `test_media.py`, `test_meta_tokens.py` |
| Dependencies, container, CI | `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/tests.yml` | — |

Test paths in the table are relative to `tests/`.

## Architecture and invariants

- `main.py` opens the TUI by default. `--watch` runs scheduled sync; `--since` backfills. Compose explicitly starts the watcher. Times and cron schedules use UTC.
- Projects contain account records and persisted timing settings. Routing never crosses project boundaries. A remote account shared across projects must be authorized separately and be destination-only in every project.
- Account identity is its local ID, not its network name. Labels are unique within a project and network. Roles are `source_and_destination`, `source_only`, and `destination_only`; Instagram/RSS must be source-only. Validate roles on writes and during sync.
- Adapters expose `authenticate`, `fetch_posts`, `download_media`, and `publish_outbound`; shared types live in `apis/types.py`. API quirks belong in the adapter, formatting in `utils/`, and orchestration in `sync/engine.py`. Auth accepts named credentials for forms and prompts when omitted.
- Sync processes projects and sources sequentially. Fetching persists normalized posts before advancing the fetch watermark. Delivery failures must remain retryable without relying on a future fetch.
- Pending batches freeze outbound content and checkpoint each published part. Retrying resumes unfinished parts. Completed mappings track source-to-destination delivery; mirror IDs prevent bot-created content from returning to the mesh. Keep checkpoint and mirror writes atomic.
- Mirror detection follows the remote identity across account records, including removed connections. Mastodon identity includes the instance; Telegram resolves channel aliases to numeric IDs. Removing a connection must erase credentials and pending work without erasing loop protection.
- Preserve source IDs through thread merging/splitting and keep media paired with its bytes. A publish result's destination ID and reply reference are distinct; multi-message publishes must record every mirror ID.
- The age gate applies to the conversation root. Initial watch cycles and explicit force/backfill actions bypass it. Replies with missing ancestors can be ineligible. URL expansion must not mutate the stored source or another destination's input.
- TUI actions are awaited in-process, one at a time, with logs and explicit deletion confirmation. Do not introduce background workers, subprocesses, queues, or application-level database locks. Logs must not expose credentials.
- Keep migrations compatible with existing account IDs, credentials, mappings, and pending deliveries. Do not rewrite applied migrations. Serialized post/outbound changes also require compatibility handling.

## Provider boundaries

- Telegram consumes a shared bot update queue, not channel history. Save source-enabled peers' updates before acknowledging offsets; destination-only peers must not accumulate source content. Preserve album member IDs.
- Threads publishes public media URLs; Telegram file references cannot be forwarded directly. Instagram/RSS only fetch. LinkedIn has no video publishing and depends on granted scopes for reading.
- Bluesky reply references include URI and CID. Threads/Instagram token renewal belongs in `utils/meta_tokens.py`; failed renewal retains the previous token until expiry.
- Network limits are defined in `config.py`. Preserve existing provider filtering and media behavior unless the task explicitly changes it; README summarizes user-facing rules.

## Operations and verification

- `data/` contains credentials and sync history. Do not inspect secrets or use real accounts for testing. Startup calls `get_engine()`, which creates the schema and applies pending migrations even for listing commands; authentication and sync also have external effects.
- Use temporary databases and mocked network calls for checks. Production migrations and publishing need separate authorization. Existing upgrade tests cover fresh and older schemas, repeatable upgrades, and data preservation.
- Concurrent sync runs are unsupported. Remote publication and local checkpointing cannot be atomic, so a crash between them can duplicate a post. Saved media URLs can expire while delivery is pending.
- When checks are authorized: `uv sync --locked --dev`, `uv run --no-sync ruff check .`, and `uv run --no-sync pytest -q`. Ruff checks syntax and likely runtime errors without imposing formatting. GitHub Actions runs these on Python 3.13 without building or deploying containers.
- Review the final diff and affected callers. Report checks actually run and any remaining limitations. Never claim a local test run verifies live provider credentials or production behavior.
