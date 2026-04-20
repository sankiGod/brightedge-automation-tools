"""
Orchestrator Agent — reasoning layer only.
Interprets ticket context, selects skill, extracts inputs.
NEVER executes actions or calls APIs directly.

Modes (set via .env):
  COWORK_ORCHESTRATOR=true  — file-based handoff to Cowork instance
  (default)                 — Anthropic API with direct ticket fetch
"""

import json
import os
import re
from core.skill_registry import SkillRegistry

import anthropic
client = anthropic.Anthropic()

SYSTEM_PROMPT = """
You are a BrightEdge support automation orchestrator. Return ONLY this JSON, no prose:
{{
  "skill": "<skill_name>",
  "confidence": <0.0-1.0>,
  "inputs": {{<fields — omit ticket_id>}},
  "missing_fields": [<actionable strings shown to the agent>],
  "notes": "<observations>"
}}

Skills: {skills}

Rules:
- confidence >= 0.8 to proceed; lower = human review
- Omit ticket_id from inputs (pipeline injects it)
- Use the EXACT field names from each skill's input_schema — do not rename or alias them
- missing_fields: actionable, e.g. "BrightEdge Username not found. Add: 'BrightEdge Username: email@company.com'"
- Credentials (username, account_id): LATEST internal note containing them
- Files: LATEST comment with a supported attachment
- mappings for keyword_upload (always a list):
  Single file: identifier MUST be null — [{{"identifier": null, "account_id": "191"}}]
  Multi-file:  one entry per file, identifier = exact filename — [{{"identifier": "file.csv", "account_id": "191"}}, ...]
  Single = exactly 1 attachment; multi = 2 or more attachments
"""


def decide(ticket_id: str, registry: SkillRegistry, ticket: dict | None = None) -> dict:
    """
    Returns a structured orchestrator decision for the given ticket.

    Args:
        ticket: Pre-fetched ticket dict from tools.zendesk.fetch_ticket().
                If provided, avoids a redundant Zendesk API call.

    Modes (set via .env):
      COWORK_ORCHESTRATOR=true  -- saves ticket to file, waits for Cowork to
                                   write cowork_decision.json, then continues.
      (default)                 -- Anthropic API with direct ticket fetch.
    """
    if os.environ.get("COWORK_ORCHESTRATOR", "").lower() == "true":
        return _decide_via_cowork(ticket_id, ticket=ticket)

    skills_desc = json.dumps(registry.descriptions(), indent=2)
    prompt = SYSTEM_PROMPT.format(skills=skills_desc)
    raw = _decide_direct(ticket_id, prompt, ticket=ticket)
    return json.loads(raw)


def _decide_via_cowork(ticket_id: str, ticket: dict | None = None) -> dict:
    """
    Cowork orchestration mode.

    1. Uses pre-fetched ticket (or fetches from Zendesk if not provided)
    2. Saves it to cowork_ticket.json for Cowork to read
    3. Polls for cowork_decision.json (written by Cowork after reasoning)
    4. Returns the decision so the pipeline can continue
    """
    import time
    from tools.zendesk import fetch_ticket

    TICKET_FILE   = "cowork_ticket.json"
    DECISION_FILE = "cowork_decision.json"
    POLL_INTERVAL = 2    # seconds between checks
    TIMEOUT       = 300  # 5 minutes max wait

    if ticket is None:
        print(f"  [Orchestrator] Cowork mode — fetching ticket #{ticket_id}...")
        ticket = fetch_ticket(ticket_id)
    else:
        print(f"  [Orchestrator] Cowork mode — using pre-fetched ticket #{ticket_id}.")

    if not ticket:
        return {
            "skill": None, "confidence": 0.0,
            "inputs": {"ticket_id": ticket_id},
            "missing_fields": ["ticket_data"],
            "notes": "fetch_ticket returned None",
        }

    safe_ticket = {k: v for k, v in ticket.items() if k != "auth"}
    files       = safe_ticket.get("keyword_files", [])
    filenames   = [f["filename"] for f in files]

    # Build mode-specific mappings template so Cowork knows exactly what to produce
    if len(filenames) == 1:
        mappings_template = [{"identifier": None, "account_id": "<extract from ticket body>"}]
        mode_note = "Single-file mode — identifier must be null."
    elif len(filenames) > 1:
        mappings_template = [
            {"identifier": name, "account_id": "<extract from ticket body>"}
            for name in filenames
        ]
        mode_note = (
            f"Multi-file mode — {len(filenames)} files detected. "
            "Each identifier must exactly match the filename shown. "
            "Find each file's account_id from the ticket body (look for 'File: <name>' or 'Account ID:' blocks)."
        )
    else:
        mappings_template = []
        mode_note = "No files found — set skill to null and confidence to 0.0."

    instructions = {
        "_instructions": {
            "task": (
                "Read the ticket data below and write cowork_decision.json. "
                "Extract the BrightEdge username and account ID(s) from the ticket body. "
                "Do NOT include ticket_id in inputs — the pipeline adds it automatically."
            ),
            "mode": mode_note,
            "output_file": DECISION_FILE,
            "output_format": {
                "skill": "keyword_upload",
                "confidence": "<0.0–1.0, use 0.95 if all fields found>",
                "inputs": {
                    "username": "<BrightEdge login email from ticket body>",
                    "mappings": mappings_template,
                },
                "missing_fields": "<list any fields you could not find, else []>",
                "notes": "<brief explanation>",
            },
        }
    }

    # Clear any stale decision from a previous run
    if os.path.exists(DECISION_FILE):
        os.remove(DECISION_FILE)

    with open(TICKET_FILE, "w", encoding="utf-8") as f:
        json.dump({**instructions, **safe_ticket}, f, indent=2)

    file_summary = (
        f"{len(filenames)} file(s): {', '.join(filenames)}" if filenames else "no files"
    )
    print(f"  [Orchestrator] Ticket saved → {TICKET_FILE} ({file_summary})")
    print(f"  [Orchestrator] Waiting for Cowork to write {DECISION_FILE} ...")
    print(f"  [Orchestrator] (Switch to Cowork and say: 'read {TICKET_FILE} and write {DECISION_FILE}')")

    start = time.time()
    while time.time() - start < TIMEOUT:
        if os.path.exists(DECISION_FILE):
            with open(DECISION_FILE, "r", encoding="utf-8") as f:
                decision = json.load(f)
            print(f"  [Orchestrator] Cowork decision received! skill={decision.get('skill')} confidence={decision.get('confidence')}")
            return decision
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Cowork did not write {DECISION_FILE} within {TIMEOUT}s. "
        "Make sure Cowork is running and has the folder open."
    )


def _decide_direct(ticket_id: str, prompt: str, ticket: dict | None = None) -> str:
    """MCP fallback — passes raw ticket data when MCP server is unavailable."""
    from tools.zendesk import fetch_ticket
    if ticket is None:
        ticket = fetch_ticket(ticket_id)
    if not ticket:
        return json.dumps({
            "skill": None, "confidence": 0.0,
            "inputs": {},
            "missing_fields": ["Could not fetch ticket data from Zendesk."],
            "notes": "fetch_ticket returned None",
        })
    # Strip auth + file URLs (CDN auth tokens — irrelevant for reasoning)
    safe_ticket = {k: v for k, v in ticket.items() if k != "auth"}
    if "keyword_files" in safe_ticket:
        safe_ticket["keyword_files"] = [
            {"filename": f["filename"], "extension": f["extension"]}
            for f in safe_ticket["keyword_files"]
        ]
    print(f"  [Orchestrator] API call → claude-haiku-4-5-20251001 (direct mode, ticket #{ticket_id})")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=prompt,
        messages=[{"role": "user", "content": f"Ticket:\n{json.dumps(safe_ticket, indent=2)}\n\nReturn decision JSON."}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences — the model sometimes wraps its JSON in ```json ... ```
    raw = re.sub(r'^```(?:json)?\s*\n?', '', raw)
    raw = re.sub(r'\s*```\s*$', '', raw).strip()
    print(f"  [Orchestrator] Response ({response.usage.input_tokens} in / {response.usage.output_tokens} out tokens):")
    print(f"  {raw}")
    return raw


def _decide_mock(ticket_id: str, ticket: dict | None = None) -> dict:
    """
    Rule-based orchestrator mock -- no AI API required.

    Uses pre-fetched ticket (or fetches from Zendesk if not provided) and
    extracts inputs by pattern-matching the ticket body. Returns the same
    decision JSON structure the real orchestrator produces.

    Extraction rules:
      username   -- first match of: "BrightEdge Username:", "BE Username:", "Username:"
      account_id -- first match of: "Account ID:" followed by digits
      mappings   -- single-account if 1 file; multi-file uses filename as identifier
    """
    from tools.zendesk import fetch_ticket

    if ticket is None:
        print(f"  [Orchestrator] Mock mode -- fetching ticket #{ticket_id}.")
        ticket = fetch_ticket(ticket_id)
    else:
        print(f"  [Orchestrator] Mock mode -- using pre-fetched ticket #{ticket_id}.")

    if not ticket:
        return {
            "skill":          None,
            "confidence":     0.0,
            "inputs":         {"ticket_id": ticket_id},
            "missing_fields": ["ticket_data"],
            "notes":          "fetch_ticket returned None -- subject mismatch or invalid ticket",
        }

    body  = ticket.get("body", "")
    files = ticket.get("keyword_files", [])

    # ── Extract username ──────────────────────────────────────────
    username = None
    username_pattern = re.compile(
        r"(?:brightedge\s+username|be\s+username|username)\s*[:\-]\s*(\S+)",
        re.IGNORECASE,
    )
    m = username_pattern.search(body)
    if m:
        username = m.group(1).strip()

    # ── Extract account_id (first / default) ─────────────────────
    account_id = None
    account_pattern = re.compile(
        r"account\s+id\s*[:\-]\s*(\d+)",
        re.IGNORECASE,
    )
    m = account_pattern.search(body)
    if m:
        account_id = m.group(1).strip()

    # ── Build mappings ────────────────────────────────────────────
    # missing_fields contains human-readable, actionable descriptions
    # so they can be surfaced directly in Teams cards without further formatting.
    # This structure is intentionally generic -- the Claude orchestrator can
    # populate the same list with descriptive strings when activated.
    missing = []
    if not username:
        missing.append(
            "BrightEdge Username not found in ticket body. "
            "Add a line: 'BrightEdge Username: your@email.com'"
        )
    if not account_id:
        missing.append(
            "Account ID not found in ticket body. "
            "Add a line: 'Account ID: 12345'"
        )

    if len(files) == 1:
        # Single-file mode -- identifier is null
        mappings = [{"identifier": None, "account_id": account_id}] if account_id else []
    elif len(files) > 1:
        # Multi-file mode -- try to match filename to account ID blocks in body
        mappings = _extract_multi_file_mappings(body, files, account_id)
    else:
        mappings = []
        missing.append(
            "No keyword file attached. "
            "Attach a CSV or Excel file to the ticket."
        )

    if not mappings and account_id:
        # account_id found but mappings still empty (multi-file edge case)
        missing.append(
            "Could not map files to Account IDs. "
            "For multiple files add a 'File: filename.csv / Account ID: 12345' block per file."
        )

    confidence = 0.95 if not missing else 0.0
    notes = "Mock orchestrator -- rule-based extraction." if not missing else (
        f"Extraction failed: {len(missing)} field(s) missing. Ticket needs manual review."
    )

    print(f"  [Orchestrator] username={username!r} account_id={account_id!r} "
          f"files={len(files)} confidence={confidence}")

    return {
        "skill":          "keyword_upload" if confidence >= 0.8 else None,
        "confidence":     confidence,
        "inputs": {
            "ticket_id": ticket_id,
            "username":  username,
            "mappings":  mappings,
        },
        "missing_fields": missing,
        "notes":          notes,
    }


def _extract_multi_file_mappings(body: str, files: list, default_account_id: str | None) -> list:
    """
    For multi-file tickets, attempts to match each filename to an account ID
    by scanning for the filename (or its basename) within ~300 chars of an Account ID.

    Falls back to default_account_id for any unmatched file.
    """
    mappings = []
    for f in files:
        filename = f["filename"]
        basename = filename.rsplit(".", 1)[0]   # strip extension for matching

        pattern = re.compile(
            re.escape(basename) + r".{0,300}?account\s+id\s*[:\-]\s*(\d+)",
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.search(body)
        acct = m.group(1).strip() if m else default_account_id

        mappings.append({"identifier": filename, "account_id": acct})

    return mappings
