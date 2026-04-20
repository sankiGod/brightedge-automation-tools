# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Architecture Overview

This project follows the **WAT framework (Workflows, Agents, Tools)** with a skill-based execution model.

**Core principle: AI decides. Code executes. Validation protects.**

```
Zendesk Webhook
  → FastAPI (webhook_receiver.py)
      → Celery (concurrent ticket processing)
          → Orchestrator Agent (Claude API + tool use)
              → Zendesk MCP server  (sidecar — read/write tickets)
              → tools/attachment.py  (binary file download — MCP gap)
              → Skill Registry  (auto-discovers skills/)
                  → skills/keyword_upload.py
                  → skills/<future_skill>.py
              → Validation Layer
              → Reporter Agent (Claude API)
          → Post result to Zendesk, set ticket open
```

### Layer responsibilities

| Layer | Location | Responsibility |
|---|---|---|
| Workflows | `workflows/` | Define objective, inputs, outputs, edge cases per task type |
| Orchestrator | `core/orchestrator.py` | Reasons, selects skill, extracts inputs — NEVER executes |
| Skill Registry | `core/skill_registry.py` | Auto-discovers all skills in `skills/` |
| Skills | `skills/` | Self-contained execution modules (Playwright, APIs) |
| Validator | `core/validator.py` | Checks inputs, permissions, confidence before execution |
| Tools | `tools/` | Shared utilities (file handling, API calls) |
| Reporter | `core/reporter.py` | Builds Zendesk reply from execution result |

---

## Running the POC

**Terminal 1:**
```
python webhook_receiver.py
```
**Terminal 2:**
```
.\ngrok.exe http 8000
```
ngrok URL persists across restarts on free plan — no need to update the Zendesk webhook URL.

---

## Zendesk MCP Server

Runs as a sidecar process. Handles ticket reads, comment reads, posting internal notes, setting ticket status. Shared across all skills — no skill reimplements Zendesk API calls directly.

**Gap:** MCP cannot return binary file bytes. Attachment downloads are handled by `tools/attachment.py` as a custom tool.

---

## Adding a New Skill

1. Create `skills/<skill_name>.py` implementing the `Skill` base class from `skills/base.py`
2. Create `workflows/<skill_name>.json` defining its inputs, outputs, and edge cases
3. The Skill Registry auto-discovers it — no changes to core code required

### Required Skill interface

```python
class Skill:
    name = "skill_name"
    description = "What it does — used by orchestrator for skill selection"

    def input_schema(self) -> dict:
        """Fields the orchestrator must extract from the ticket."""
        return {}

    def validate(self, inputs: dict) -> dict:
        """Return {"valid": True/False, "errors": [...]}"""
        return {"valid": True, "errors": []}

    def execute(self, inputs: dict) -> dict:
        """Return {"status": "success"/"failure", ...}"""
        return {"status": "success"}
```

---

## Orchestrator Behavior

The orchestrator (Claude API) **only reasons and plans** — it never calls APIs or executes actions directly.

**Output format:**
```json
{
  "skill": "keyword_upload",
  "confidence": 0.92,
  "inputs": {
    "username": "john@company.com",
    "account_id": "191",
    "filename": "keywords.csv"
  },
  "missing_fields": [],
  "notes": ""
}
```

**Confidence rules:**
- `>= 0.8` → proceed to validation and execution
- `< 0.8` → route to human review, do not execute

---

## File & Credentials Scoping (Zendesk)

This is the most bug-prone area when modifying Zendesk logic:

- **Credentials** — always from the **latest internal note** containing `BrightEdge Username` or `Account ID`
- **Files** — always from the **latest comment of any type** that has a supported attachment
- These two sources are **independent** — they can come from different comments
- Single-file vs. multi-file mode is determined by attachment count on that one comment, not across all comments
- In multi-file mode, `File:` and `Account ID` labels must match filenames

---

## BrightEdge Upload — Critical DOM Behaviour

The `hidden` attribute on `#massAccountUploadResponseHeader` is **never removed** by BrightEdge. Checking `element.hidden` always returns `true`. Only `style="display: block"` is added on completion.

**Correct completion check:**
```javascript
() => {
    const resp = document.getElementById('massAccountUploadResponseHeader');
    return resp && resp.style.display === 'block';
}
```

Upload timeout: 10 minutes max.

Pod detection: after login, BrightEdge redirects to `appN.brightedge.com`. All subsequent URLs must use this subdomain — not `www.brightedge.com`.

---

## TSV Output Format

```
Login\tKW\tPLP\toff\tKWG1\tKWG2\tKWG3...
```
- `off` is a fixed literal column — not derived from the file
- KWG column count is dynamic (max groups any keyword has across the file)
- UTF-8 encoded

---

## Column Normalisation

Before matching column headers, normalise: strip `(optional)`, `(required)`, `*`, `#`, `[]`; replace `\n`/`\t` with spaces; collapse whitespace; lowercase.

| Field | Recognised aliases |
|---|---|
| Keyword | `keyword`, `kw`, `keywords`, `search term`, `search terms` |
| PLP | `preferred landing page`, `plp`, `landing page`, `url` |
| Groups | anything starting with `keyword group` after normalisation |

---

## Loop Prevention

- Zendesk trigger removes `ai_agent_automation` tag when it fires — tag is gone before pipeline runs
- `webhook_receiver.py` has a 5-minute deduplication window keyed on ticket ID as a secondary safety net
- Internal notes posted by the reporter do not re-trigger the pipeline

---

## Environment Variables

See `.env.example` for all required variables. Never commit `.env`.

---

## Work Remaining

- **Stuck upload condition** — DOM state when BrightEdge hangs is unknown. Requires DevTools inspection during a live stuck upload before adding retry logic
- **Multi-sheet Excel** — code path exists but untested end-to-end
- **Celery + Redis** setup — not yet implemented; webhook currently runs synchronously
