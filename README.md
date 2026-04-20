# BrightEdge Automation Tools

AI-powered Zendesk automation agent that uses Claude to orchestrate keyword uploads to BrightEdge. Receives webhooks, extracts structured inputs, executes browser-based uploads via Playwright, and notifies via Microsoft Teams.

---

## How It Works

A Zendesk ticket tagged `ai_agent_automation` fires a webhook to this server. The pipeline then runs automatically:

```
Zendesk Webhook
  → FastAPI (webhook_receiver.py)
      → Orchestrator (Claude API)
          → Reads ticket via Zendesk MCP
          → Selects skill + extracts inputs
      → Validator
          → Confidence + input checks
      → Skill
          → Downloads attachment
          → Parses CSV/Excel
          → Uploads to BrightEdge via Playwright
          → QA verifies upload
      → Reporter (Claude API)
          → Builds agent-facing summary
      → Microsoft Teams notification
```

**Core principle: AI decides. Code executes. Validation protects.**

---

## Supported Skills

| Skill | Trigger tag | Description |
|---|---|---|
| `keyword_upload` | `ai_agent_automation` | Uploads keywords to BrightEdge Mass Account Keyword Upload |
| `kwg_se_upload` | `ai_agent_automation` | Assigns search engines to keyword groups via BrightEdge KWG SE Upload |

The orchestrator reads the ticket subject and body to determine which skill to invoke.

---

## Project Structure

```
├── core/
│   ├── orchestrator.py      # Claude-powered decision engine
│   ├── validator.py         # Input + confidence validation
│   ├── reporter.py          # Claude-powered reply builder
│   └── skill_registry.py   # Auto-discovers skills/
├── skills/
│   ├── base.py              # Skill base class
│   ├── keyword_upload.py    # Keyword upload skill
│   └── kwg_se_upload.py     # KWG SE upload skill
├── tools/
│   ├── brightedge.py        # Playwright upload automation
│   ├── brightedge_api.py    # BrightEdge REST API client
│   ├── zendesk.py           # Zendesk API helpers
│   ├── attachment.py        # Zendesk attachment downloader
│   ├── parser.py            # CSV/Excel parser + column normaliser
│   ├── transformer.py       # TSV builder + Teams reply formatter
│   ├── teams.py             # Microsoft Teams Adaptive Card notifications
│   └── column_reasoner.py  # Claude-powered column header matching
├── mcp/
│   └── zendesk_server.py   # Zendesk MCP sidecar server
├── workflows/
│   ├── keyword_upload.json  # Skill definition (inputs, outputs, edge cases)
│   └── kwg_se_upload.json
├── webhook_receiver.py      # FastAPI entry point
├── .env.example
└── CLAUDE.md                # AI coding guidelines for this repo
```

---

## Setup

### 1. Install dependencies

```bash
pip install fastapi uvicorn playwright python-dotenv requests openpyxl anthropic mcp
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in all values in .env
```

See [`.env.example`](.env.example) for all required variables.

### 3. Configure Zendesk

- Create a webhook pointing to `POST https://<your-ngrok-url>/webhook/zendesk`
- Create a trigger that fires on `ai_agent_automation` tag added and calls the webhook
- The trigger should also **remove** the `ai_agent_automation` tag to prevent loops

---

## Running

**Terminal 1 — start the server:**
```bash
python webhook_receiver.py
```

**Terminal 2 — expose via ngrok:**
```bash
.\ngrok.exe http 8000
```

The ngrok URL persists across restarts on the free plan — no need to update the Zendesk webhook URL on restart.

**Endpoints:**
| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/zendesk` | Main webhook receiver |
| `GET` | `/webhook/zendesk` | Zendesk test button (returns 200) |
| `GET` | `/health` | Health check |

---

## Triggering a Ticket

1. Open a Zendesk ticket
2. Add an internal note with credentials:
   ```
   BrightEdge Username: user@company.com
   Account ID: 12345
   ```
3. Attach the keyword CSV or Excel file as a comment
4. Add the tag `ai_agent_automation`

The pipeline runs automatically. Results are posted to Microsoft Teams and the ticket is set to open.

---

## Adding a New Skill

1. Create `skills/<skill_name>.py` implementing the `Skill` base class:

```python
from skills.base import Skill

class MySkill(Skill):
    name = "my_skill"
    description = "What it does — used by the orchestrator for skill selection"

    def input_schema(self):
        return {
            "username":   {"type": "string", "required": True},
            "account_id": {"type": "string", "required": True},
        }

    def validate(self, inputs):
        errors = []
        if not inputs.get("username"):
            errors.append("username is required")
        return {"valid": len(errors) == 0, "errors": errors}

    def execute(self, inputs):
        # Your logic here
        return {"status": "success", "all_summaries": [...]}
```

2. Create `workflows/<skill_name>.json` describing inputs, outputs, and edge cases.

The Skill Registry auto-discovers it — no changes to core code required.

---

## Environment Variables

| Variable | Description |
|---|---|
| `ZENDESK_SUBDOMAIN` | Your Zendesk subdomain (e.g. `yourcompany`) |
| `ZENDESK_EMAIL` | Email used for Zendesk API auth |
| `ZENDESK_API_TOKEN` | Zendesk API token |
| `BRIGHTEDGE_PASSWORD` | Universal BrightEdge password |
| `BE_LOGIN_URL` | BrightEdge login URL (default: `https://www.brightedge.com/secure/login`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude orchestrator + reporter) |
| `TEAMS_WEBHOOK_URL` | Microsoft Teams incoming webhook URL |
| `COWORK_ORCHESTRATOR` | Set `true` to use Cowork instead of Claude API (default: `false`) |
| `FUZZY_SHEET_THRESHOLD` | Fuzzy match threshold for sheet names (default: `0.6`) |
| `FUZZY_FILE_THRESHOLD` | Fuzzy match threshold for filenames (default: `0.5`) |

---

## Loop Prevention

- The Zendesk trigger removes the `ai_agent_automation` tag when it fires — the tag is gone before the pipeline runs
- `webhook_receiver.py` keeps a 5-minute deduplication window per ticket ID as a secondary safety net
- Internal notes posted by the agent do not re-trigger the pipeline
