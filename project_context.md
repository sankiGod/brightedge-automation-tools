# BrightEdge Keyword Upload — AI Agent System

## What This Is

A production AI agent that processes BrightEdge keyword upload requests from Zendesk automatically. An agent adds a single tag to a ticket; the system takes over — reads the ticket, downloads the keyword file, validates it, transforms it into BrightEdge's required TSV format, performs the upload via browser automation, and posts the result back to Microsoft Teams.

**Core principle: AI decides. Code executes. Validation protects.**

---

## File Structure (WAT Framework)

```
webhook_receiver.py         FastAPI server — receives Zendesk webhooks, runs pipeline in background thread

core/
  orchestrator.py           Reasoning layer — reads ticket, selects skill, extracts inputs (3 modes)
  validator.py              Confidence + input checks before execution
  skill_registry.py         Auto-discovers all Skill subclasses in skills/ via importlib reflection
  reporter.py               Builds agent-facing internal note from execution result (2 modes)

skills/
  base.py                   Abstract Skill base class (name, description, input_schema, validate, execute)
  keyword_upload.py         Full end-to-end keyword upload skill implementation
  kwg_se_upload.py          KWG search engine assignment upload skill

tools/
  zendesk.py                Zendesk API — fetch ticket, post reply, download attachment, set status
  brightedge.py             Playwright browser automation — login, pod detection, upload, response parsing
  brightedge_api.py         BrightEdge REST API client — keyword groups, KWG names, QA verification
  parser.py                 CSV/Excel parsing, column normalisation, fuzzy filename/sheet matching
  transformer.py            Column mapping (fuzzy → Claude fallback), row remapping, TSV generation, reply building
  column_reasoner.py        AI column mapping fallback (Cowork or Anthropic API)
  teams.py                  Microsoft Teams Adaptive Card notifications (3 card types)
  attachment.py             Binary file download utility (MCP gap filler); defines TMP_DIR and cleanup

mcp/
  zendesk_server.py         Custom local MCP server wrapping tools/zendesk.py — used by Anthropic API path

workflows/
  keyword_upload.json       Workflow definition — inputs, outputs, edge cases
  kwg_se_upload.json        Workflow definition — inputs, outputs, edge cases

tmp/                        Project-local temp directory — TSV uploads and downloaded attachments
                            Files older than 10 days are deleted on server startup
```

---

## Pipeline Flow

```
Zendesk tag (ai_agent_automation) fires
  → POST /webhook/zendesk  (FastAPI — webhook_receiver.py)
      → dedup check (5-min window, keyed on ticket_id)
      → background thread: _run_and_clear(ticket_id)
          → cleanup_old_tmp_files()              ← runs on server startup, not per-ticket
          → zd.fetch_ticket(ticket_id)           ← one API call, result passed into orchestrator
          → orchestrator.decide(ticket_id, registry, ticket=ticket_meta)
              ├─ Mock mode (default)             rule-based regex, no AI
              ├─ Cowork mode                     saves cowork_ticket.json, polls for cowork_decision.json
              └─ Anthropic API mode (commented)  Claude + Zendesk MCP sidecar
          → decision["inputs"]["ticket_id"] = ticket_id   ← injected here, before validation
          → validator.validate(decision, skill)
          → skill.execute(inputs)
              → zd.fetch_ticket (second call, within skill — skill is self-contained)
              → download + parse CSV/Excel files
              → fuzzy match files/sheets → account IDs
              → transformer.map_columns (fuzzy → Claude fallback)
              → transformer.remap_rows + transform_to_groups
              → brightedge.fetch_account_logins (one browser session)
              → brightedge.upload_to_brightedge (per account — Playwright)
          → transformer.build_reply(all_summaries, skipped_notes)   ← customer-facing
          → reporter.build_note(skill_name, inputs, result, elapsed) ← agent-facing
          → any_upload_failed = any(not s["success"] for s in all_summaries)
          → teams.notify(ticket_id, assignee_email, customer_message, agent_note,
                         failed=any_upload_failed)
              → set_status(ticket_id, "open")    ← always called inside teams.notify
          → _processing.pop(ticket_id) in finally  ← clears dedup so ticket can be re-triggered
```

**Error paths:**

- `TimeoutError` (Cowork or column reasoner did not respond within 5 min) → dedicated `except TimeoutError` → `teams.notify_error` with retry instructions
- Missing fields from orchestrator → `teams.notify_missing_fields` with actionable bullet per field
- Validation failure → `teams.notify_error`
- Any other exception → `teams.notify_error` with friendly message (JSONDecodeError, ConnectionError, and generic cases handled separately)

---

## Orchestrator — 3 Modes

Controlled by the `COWORK_ORCHESTRATOR` environment variable. All three modes produce identical output JSON:

```json
{
  "skill": "keyword_upload",
  "confidence": 0.95,
  "inputs": {
    "username": "john@company.com",
    "mappings": [
      {"identifier": null, "account_id": "191"}
    ]
  },
  "missing_fields": [],
  "notes": ""
}
```

`ticket_id` is **not** in the orchestrator output — it is injected into `inputs` by `webhook_receiver.py` before validation.

### Mode 1: Mock (default, `COWORK_ORCHESTRATOR` unset or `false`)

Rule-based regex extraction. No AI API call required. Used for POC testing and when tickets follow a known format.

- Extracts username via pattern: `brightedge username:`, `be username:`, or `username:`
- Extracts account_id: first match of `account id: <digits>`
- Builds mappings: single-file (identifier=null) if 1 attachment; multi-file uses `_extract_multi_file_mappings()` which scans for filename basename near an `Account ID:` block (within 300 chars)
- Confidence: `0.95` if all fields found, `0.0` if anything missing

### Mode 2: Cowork (`COWORK_ORCHESTRATOR=true`)

File-based handoff — Cowork acts as the AI reasoning layer.

1. Uses pre-fetched ticket (avoids redundant API call) or fetches if not provided
2. Clears any stale `cowork_decision.json` from a previous run
3. Writes `cowork_ticket.json` containing:
   - Ticket data (subject, body, keyword_files — auth tuple stripped)
   - `_instructions` block: task description, mode note, expected output format
   - Pre-filled `mappings` template with actual filenames as identifiers (Cowork only needs to fill in account IDs)
   - Mode note: `"Single-file mode — identifier must be null"` or `"Multi-file mode — N files detected..."`
4. Polls for `cowork_decision.json` every 2 seconds, 5-minute timeout
5. On timeout: raises `TimeoutError` → pipeline sends Teams card with retry instructions

**Cowork prompt:** "Read `cowork_ticket.json` and write `cowork_decision.json`"

### Mode 3: Anthropic API (commented out — pending API credits)

Claude via Zendesk MCP sidecar. Code is complete in `core/orchestrator.py`.

```python
# Restore by uncommenting in core/orchestrator.py:
client.beta.messages.create(
    model="claude-sonnet-4-6",
    mcp_servers=[{"type": "stdio", "command": "python", "args": [MCP_SERVER_PATH], "env": mcp_env}],
    betas=["mcp-client-2025-04-04"],
)
```

MCP fallback: if the sidecar fails to start, `_decide_direct()` passes the raw ticket JSON in the prompt instead (no MCP tools, just context).

---

## Column Reasoner — 2 Modes

`tools/column_reasoner.py` — called by `transformer.map_columns()` when fuzzy matching confidence falls below `FUZZY_SHEET_THRESHOLD` (default 0.6).

Both modes use the same system prompt instructing the model to return only JSON with `keyword`, `plp`, and `groups` fields using exact column names from the provided headers list.

### Mode 1: Cowork (`COWORK_ORCHESTRATOR=true`)

1. Clears any stale `cowork_column_response.json`
2. Saves `cowork_column_request.json` with `headers`, `sample_rows` (first 5), and the full `system_prompt`
3. Polls for `cowork_column_response.json` every 2 seconds, 5-minute timeout
4. Returns `None` on timeout (not `TimeoutError`) — skill logs a skip note for that file and continues

**Cowork prompt:** "Read `cowork_column_request.json` and give me the column mapping JSON"

### Mode 2: Anthropic API (default when Cowork off)

Calls `claude-sonnet-4-6` directly with headers + first 5 sample rows. Client is initialised lazily so a missing `ANTHROPIC_API_KEY` does not crash startup. Returns `None` on any API or parse error.

**Output shape (both modes):**
```json
{
  "keyword": "Search Term",
  "plp": "Landing Page URL",
  "groups": ["KW Group 1", "KW Group 2"],
  "source": "claude",
  "confidence": 1.0
}
```

---

## MCP Server (`mcp/zendesk_server.py`)

**This is a custom local MCP server — not an official Zendesk integration.**

- Runs as a stdio subprocess spawned by the orchestrator's Anthropic API path
- Wraps `tools/zendesk.fetch_ticket()` and a direct comments API call
- Strips the `auth` tuple from all responses (credentials never exposed via MCP)
- Used **only** by the Anthropic API orchestrator path (currently commented out)

**Tools exposed:**

| Tool | Returns |
|---|---|
| `get_ticket(ticket_id)` | Subject, full body (credentials note first), keyword file list — no auth tuple |
| `get_ticket_comments(ticket_id)` | Structured comment list: id, body, public flag, has_attachments, attachment_names |

**Gap:** MCP cannot return binary file bytes. Attachment downloads always go through `tools/attachment.py` or `tools/zendesk.download_file()` directly.

---

## Teams Notifications (`tools/teams.py`)

Three public functions, all using Adaptive Card v1.2. Every function:
- Calls `set_status(ticket_id, "open")` on the Zendesk ticket before posting
- Never raises — pipeline must not fail due to a notification error
- Uses `msteams.entities` to `@mention` the assignee by UPN, triggering a real Teams notification
- Includes an `Action.OpenUrl` "View Ticket" button: `https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{ticket_id}`

### `notify(ticket_id, assignee_email, customer_message, agent_note, failed=False)`

Success/result card. Header style and title depend on the `failed` flag:
- `failed=False` (default) → `accent` (blue) header, title: `"Keyword Upload — Ticket #X"`
- `failed=True` → `attention` (red) header, title: `"Upload Failed — Ticket #X"`

`failed=True` is passed when any account summary has `success=False`. Renders `customer_message` then `agent_note` via `_message_to_blocks()`.

### `notify_missing_fields(ticket_id, assignee_email, missing_fields)`

"Action Required" card. `attention` (red) header. One `TextBlock` bullet per missing field string. Ends with instructions to add the missing info and re-add the `ai_agent_automation` tag.

### `notify_error(ticket_id, assignee_email, error_text)`

Failure card. `attention` (red) header. `error_text` rendered via `_message_to_blocks()`.

### `_message_to_blocks(text, subtle=False)`

Converts plain text into Adaptive Card body elements:
- Lines containing ` : ` (not starting with bullet/greeting prefixes) → buffered into a `FactSet` for aligned key-value rendering
- A blank line flushes the FactSet buffer
- Lines ending in `:` with no leading bullet → bold section header `TextBlock`
- All other lines → regular `TextBlock`

---

## Temp File Management (`tools/attachment.py`)

`TMP_DIR = Path(__file__).parent.parent / "tmp"` — all pipeline temp files go here:
- TSV files written by `tools/brightedge.py` (`NamedTemporaryFile` with `dir=TMP_DIR`)
- Attachment downloads from `tools/attachment.py`

`cleanup_old_tmp_files(days=10)` deletes files in `TMP_DIR` older than `days` days. Called once at server startup in `webhook_receiver.py`'s `__main__` block.

---

## Ticket Scoping Rules (CRITICAL — most bug-prone area)

These two sources are **independent** — they can come from different comments:

| What | Where it comes from |
|---|---|
| **Credentials** (username, account ID) | Latest internal note containing "brightedge username", "be username", or "account id" |
| **Keyword files** | Latest comment of any type (public or internal) that has a supported attachment (.csv, .xlsx, .xls) |

`tools/zendesk.fetch_ticket()` walks comments in **reverse** (newest first) for both. The first credentialed internal note wins. The first comment with attachments wins. Single-file vs. multi-file mode is determined by the attachment count on that one comment — not across all comments.

---

## Fuzzy Filename Matching — Two-Pass (`tools/parser.py`)

`fuzzy_match_filename(name, available_files)` runs two passes, both at threshold 0.5:

1. **Pass 1 — full filename:** compares `name` directly against `available_files` (case-insensitive, difflib)
2. **Pass 2 — basename match:** strips extension from both `name` and all candidates, compares basenames

This handles tickets where the `File:` label is shorter than the actual attachment filename, e.g. `"norgren"` matching `"norgren_keywords.csv"` (basenames: `"norgren"` vs `"norgren_keywords"`).

Sheet name matching uses `fuzzy_match_sheet()` with a stricter 0.6 threshold.

---

## Column Normalisation

Before alias matching, all headers are normalised:
- Strip `(optional)`, `(required)`, `*`, `#`, `[]`
- Replace `\n` and `\t` with spaces
- Collapse whitespace, lowercase

| Field | Recognised aliases (after normalisation) |
|---|---|
| Keyword | `keyword`, `kw`, `keywords`, `search term`, `search terms` |
| PLP | `preferred landing page`, `plp`, `landing page`, `url` |
| Groups | any header starting with `keyword group` after normalisation |

---

## TSV Output Format (Keyword Upload)

```
Login\tKW\tPLP\toff\tKWG1\tKWG2\tKWG3...
```

- `Login` — account-scoped email from `fetch_account_logins()`, falls back to org username
- `KW` — keyword text
- `PLP` — preferred landing page URL (empty string if none)
- `off` — always literal `0` (tracking active). Not derived from the source file.
- `KWG1…N` — keyword group columns. Count is dynamic: max groups any single keyword belongs to across the file
- Keywords in multiple groups appear on **one row** with each group in its own KWG column — not comma-separated in one cell
- UTF-8 encoded

## TSV Output Format (KWG SE Upload)

```
Login\tKWG\tSEs
```

- `Login` — account login email
- `KWG` — keyword group name
- `SEs` — comma-separated search engine IDs (e.g. `1,2,3`)
- Header is exact: `login`, `kwg`, `ses` (confirmed from BrightEdge sample file)
- UTF-8 encoded, no BOM

---

## BrightEdge DOM Facts — Keyword Upload (CRITICAL)

The `hidden` attribute on `#massAccountUploadResponseHeader` is **never removed** by BrightEdge. Checking `element.hidden` always returns `true` even after upload completes.

**Correct completion check:**
```javascript
() => {
    const resp = document.getElementById('massAccountUploadResponseHeader');
    return resp && resp.style.display === 'block';
}
```

`wait_for_function()` is used (not a polling loop) — reacts the instant the DOM changes.

Upload timeout: 600,000ms (10 minutes max).

**Pod detection:** after login, BrightEdge redirects to `appN.brightedge.com`. All subsequent navigation uses this pod subdomain — never `www.brightedge.com`.

**Login timeout:** `TIMEOUT_LOGIN = 60,000ms` (60 seconds). Increased from 30s because the login page can be slow to respond when called back-to-back with another browser session.

---

## BrightEdge DOM Facts — KWG SE Upload (CRITICAL)

The KWG SE upload page behaves differently from keyword upload:

**After clicking Next**, BrightEdge **navigates** to a new page:
```
/admin/mass_account_kwg_se_upload_import
```

Results render immediately on the `_import` page — there is no `#massAccountUploadResponseHeader` or `body.loading` phase.

**Correct implementation** (`_upload_kwg_se_and_poll()` in `tools/brightedge.py`):
```python
page.click("#massAccountFileUploadNext", no_wait_after=True)
page.wait_for_url("**/admin/mass_account_kwg_se_upload_import**", timeout=UPLOAD_TIMEOUT_MS)
page_text = page.inner_text("body")
return _parse_kwg_se_response(page_text)
```

**Response format** (confirmed from live run):
```
Account Specific - login@email.com :
Congratulations => All Passed!
```
or on error:
```
Account Specific - login@email.com :
Invalid Se Id Detected => "47"
```

`_parse_kwg_se_response()` extracts lines after the `"Account Specific"` sentinel, counts `Updated KWGs =>` lines for `kwgs_updated`, and collects any line containing `invalid`, `error`, `failed`, `not found`, or `detected` into `error_msgs`.

**Invalid SE ID handling** in `tools/transformer.py`:
- `_invalid_se_errors(summary)` — filters `error_msgs` for entries containing `"invalid se id"`
- If all failed accounts have invalid SE errors, the Teams message is specific: explains the cause and instructs the user to correct their SE IDs and resubmit
- A failed upload (`any_upload_failed=True`) sends a red (`attention`) Teams card via `teams.notify(failed=True)`

---

## Ticket Body Formats

### Single account (CSV or single-sheet Excel)
```
BrightEdge Username: john@company.com
Account ID: 191
```

### Multiple CSVs
```
BrightEdge Username: john@company.com
File: norgren_keywords.csv
Account ID: 191
File: bergfreunde_keywords.csv
Account ID: 225
```

### Multi-sheet Excel
```
BrightEdge Username: john@company.com
Sheet: Norgren
Account ID: 191
Sheet: Bergfreunde
Account ID: 225
```

`parser.parse_brightedge_fields()` handles all three. The `IDENTIFIER_RE` regex matches `sheet:`, `tab:`, `worksheet:`, `file:`, `filename:` labels (case-insensitive). Account IDs must be all-digit.

---

## Trigger and Loop Prevention

1. **Primary:** The Zendesk trigger removes `ai_agent_automation` the moment it fires. The tag is gone before the pipeline runs — internal notes posted by the reporter cannot re-trigger it.

2. **Secondary:** `webhook_receiver.py` maintains `_processing` (dict keyed on `ticket_id`) with a 5-minute dedup window. Zendesk webhook retries within 5 minutes are silently dropped with `{"status": "skipped", "reason": "dedup"}`.

3. **Dedup cleared in `finally`:** `_run_and_clear()` wraps `process_in_background()` and pops `ticket_id` from `_processing` in a `finally` block. The dedup entry is always cleared regardless of success or failure, so re-adding the tag after any failure always triggers a fresh run.

---

## Key Pipeline Details (Behaviour-Affecting)

1. **Ticket fetched once, passed into orchestrator.** `webhook_receiver.py` calls `zd.fetch_ticket()` once at the top of the pipeline and passes the result to `orchestrator.decide(ticket=ticket_meta)`. All three orchestrator modes accept and prefer the pre-fetched ticket to avoid a redundant Zendesk API call.

2. **`ticket_id` injected before validation.** `decision["inputs"]["ticket_id"] = ticket_id` is set explicitly in `webhook_receiver.py` after the orchestrator returns, before `validator.validate()` runs. This ensures both validation and execution always have the ticket ID regardless of what the orchestrator produced.

3. **Dedicated `TimeoutError` handler.** `process_in_background()` has a specific `except TimeoutError` block before the generic `except Exception`. It posts a Teams card explaining that Cowork may not be running and gives clear retry instructions. The generic handler catches all other exceptions separately.

4. **`missing_fields` strings are human-readable and actionable.** Both the mock orchestrator and the future Claude orchestrator populate `missing_fields` with complete, agent-readable instructions (e.g. `"BrightEdge Username not found. Add a line: 'BrightEdge Username: your@email.com'"`). These strings are rendered directly as bullet points in Teams cards without any further formatting.

5. **Failed upload routing.** `webhook_receiver.py` computes `any_upload_failed = any(not s.get("success") for s in all_summaries)` and passes `failed=any_upload_failed` to `teams.notify()`. This switches the card header to red and the title to "Upload Failed".

6. **`error_msgs` passed through.** `upload_kwg_se_to_brightedge()` includes `"error_msgs": upload_result.get("error_msgs", [])` in its return dict so `transformer.build_kwg_se_reply()` can detect specific failure types (e.g. invalid SE IDs).

---

## Environment Variables

```bash
# Zendesk
ZENDESK_SUBDOMAIN=brightedge1
ZENDESK_EMAIL=ankit.singh@brightedge.com
ZENDESK_API_TOKEN=<api_token>

# BrightEdge
BRIGHTEDGE_PASSWORD=<universal_password>
BE_LOGIN_URL=https://www.brightedge.com/secure/login    # optional, defaults to this value

# Anthropic
ANTHROPIC_API_KEY=<api_key>       # required for column_reasoner API path and reporter AI path

# Microsoft Teams
TEAMS_WEBHOOK_URL=https://brightedge.webhook.office.com/webhookb2/...

# Orchestrator / column reasoner mode
COWORK_ORCHESTRATOR=false          # true = Cowork file-based handoff for both orchestrator and column reasoner

# Fuzzy matching thresholds (optional overrides)
FUZZY_SHEET_THRESHOLD=0.6          # sheet name and column header matching (stricter)
FUZZY_FILE_THRESHOLD=0.5           # CSV filename matching (looser, handles short labels)

# Production (not yet implemented)
# REDIS_URL=redis://localhost:6379/0
```

---

## Running the System

**Terminal 1 — webhook server:**
```
python webhook_receiver.py
```

**Terminal 2 — ngrok tunnel:**
```
.\ngrok.exe http 8000
```

The ngrok URL persists across restarts on the free plan — no need to update the Zendesk webhook URL on restart.

**Endpoints:**
- `POST /webhook/zendesk` — main webhook (returns 200 immediately, processes in background thread)
- `GET /webhook/zendesk` — Zendesk test button endpoint (returns reachability confirmation)
- `GET /health` — health check
- `GET /` — status page

---

## How to Add a New Skill

1. Create `skills/<skill_name>.py` implementing the `Skill` base class from `skills/base.py`
2. Create `workflows/<skill_name>.json` defining inputs, outputs, and edge cases
3. The Skill Registry auto-discovers it via `pkgutil.iter_modules` + `importlib` — no changes to core code required

**Required interface:**
```python
class Skill:
    name = "skill_name"
    description = "Used by orchestrator for skill selection"

    def input_schema(self) -> dict:
        return {}

    def validate(self, inputs: dict) -> dict:
        return {"valid": True, "errors": []}

    def execute(self, inputs: dict) -> dict:
        return {"status": "success"}
```

---

## Current Status

### Working end-to-end

- Full WAT pipeline: webhook → orchestrator → validator → skill → reporter → Teams
- Mock orchestrator (rule-based, no API): single-file and multi-file CSV, regex extraction
- Cowork orchestrator mode: file handoff with pre-filled mapping template and `_instructions` block
- Cowork column reasoner mode: file handoff with headers, sample rows, and system prompt
- Anthropic API column reasoner: calls `claude-sonnet-4-6` directly (requires `ANTHROPIC_API_KEY`)
- File parsing: CSV (UTF-8 BOM handling), Excel (all sheets, header row auto-detection)
- Column mapping: fuzzy alias matching → Claude fallback pipeline
- TSV transformation: multi-group deduplication, dynamic KWG column count, account-scoped login lookup
- BrightEdge Playwright — keyword upload: login, cookie banner, pod detection, account switch, upload, DOM response parsing, logout
- BrightEdge Playwright — KWG SE upload: login, account switch, TSV upload, `wait_for_url` navigation to `_import` page, response parsing, logout
- Invalid SE ID detection: specific Teams card message with actionable fix instructions
- Account login lookup: `fetch_account_logins()` scrapes manage_account admin table with DataTables pagination
- Teams Adaptive Cards: success (blue), upload failure (red), missing-fields, and error card types; `@mention` via `msteams.entities`; `failed=` flag routes to red/blue header automatically
- Zendesk: fetch ticket (credential/file scoping), post public reply and internal note, set status
- Loop prevention: tag removal (primary) + 5-min dedup window with `finally` clear (secondary)
- `TimeoutError` separated from generic exceptions for clean Teams messaging
- Temp file management: all TSVs and downloads go to `tmp/`; files older than 10 days cleaned on startup
- `ticket_id` injection before validation
- Ticket pre-fetched once and passed into orchestrator (no double API call)

### Pending / not yet implemented

- **Anthropic API orchestrator** — commented out in `core/orchestrator.py` pending API credits. Code is complete; uncomment the `client.beta.messages.create` block and remove the `_decide_mock()` call.
- **Anthropic API reporter** — commented out in `core/reporter.py` pending API credits. Rule-based mock active. Uncomment the `client.messages.create` block to restore AI-generated summaries.
- **Stuck upload condition** — DOM state when BrightEdge hangs mid-upload is unknown. Retry logic cannot be written until DevTools inspection during a live stuck upload identifies the signal.
- **Multi-sheet Excel end-to-end** — code path exists and is exercised, but has not been tested with a real multi-sheet Excel ticket.
- **Celery + Redis** — webhook currently runs synchronously in a background thread (one ticket at a time per process). `REDIS_URL` is reserved for the future async task queue.
- **Teams confirm/reject card for column mapping** — interactive Confirm/Reject buttons for agent review of ambiguous column mappings are not yet built. Column reasoner currently proceeds automatically and logs a skip note if it cannot resolve.
