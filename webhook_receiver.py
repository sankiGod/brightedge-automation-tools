"""
webhook_receiver.py — FastAPI webhook server.

Receives Zendesk webhooks and runs the full WAT pipeline in a background thread:
  Orchestrator (Claude + MCP) → Validator → Skill → Reporter → Zendesk reply

Run:
    python webhook_receiver.py
"""

import time
import threading
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

# ── Deduplication window ───────────────────────────────────────
# Prevents re-processing the same ticket if Zendesk retries the webhook.
# Primary loop prevention is the Zendesk trigger removing the
# ai_agent_automation tag; this is the secondary safety net.
_processing: dict[str, float] = {}
_DEDUP_WINDOW_SECONDS = 300   # 5 minutes


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.get("/webhook/zendesk")
async def zendesk_webhook_get(request: Request):
    """Zendesk test button sends a GET — confirm the endpoint is reachable."""
    return {"status": "ok", "message": "Webhook endpoint is reachable"}


@app.post("/webhook/zendesk")
async def zendesk_webhook(request: Request):
    """
    Main webhook endpoint. Returns 200 immediately (Zendesk requires fast response)
    and processes the ticket in a background thread.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Extract ticket_id — handle multiple payload shapes
    ticket_id = str(
        payload.get("ticket_id")
        or payload.get("id")
        or (payload.get("ticket") or {}).get("id")
        or "unknown"
    )

    print(f"\n[Webhook] Received — ticket_id={ticket_id}")

    # Deduplication check
    now  = time.time()
    last = _processing.get(ticket_id, 0)
    if now - last < _DEDUP_WINDOW_SECONDS:
        print(f"  [Dedup] Ticket #{ticket_id} already processing — skipping.")
        return {"status": "skipped", "ticket_id": ticket_id, "reason": "dedup"}

    _processing[ticket_id] = now

    thread = threading.Thread(
        target=_run_and_clear,
        args=(ticket_id,),
        daemon=True,
    )
    thread.start()

    return {"status": "accepted", "ticket_id": ticket_id}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "status":   "running",
        "endpoint": "POST /webhook/zendesk",
        "health":   "GET /health",
    }


# ─────────────────────────────────────────────
#  WAT Pipeline
# ─────────────────────────────────────────────

def _run_and_clear(ticket_id: str):
    """Runs the pipeline then clears the dedup entry so the ticket can be re-triggered."""
    try:
        process_in_background(ticket_id)
    finally:
        _processing.pop(ticket_id, None)


def process_in_background(ticket_id: str):
    """
    Full WAT pipeline for one ticket:
      1. Orchestrator (Claude + Zendesk MCP) → structured decision
      2. Validator → confidence + input checks
      3. Skill.execute() → download, parse, transform, upload
      4. Reporter (Claude) → brief agent-facing summary note
      5. transformer.build_reply() → structured customer-facing note
      6. Post both notes as Zendesk internal comments
    """
    from dotenv import load_dotenv
    load_dotenv()

    from core.skill_registry import SkillRegistry
    from core import orchestrator, validator, reporter
    from tools import zendesk as zd, teams
    from tools.transformer import build_reply, build_kwg_se_reply

    print(f"\n{'=' * 55}")
    print(f"  [Pipeline] Starting ticket #{ticket_id}")
    print(f"{'=' * 55}")

    assignee_email = None   # set early so the except block can always reference it

    try:
        # ── Step 1: Fetch ticket (used for assignee email + orchestrator) ──
        ticket_meta    = zd.fetch_ticket(ticket_id)
        assignee_email = (ticket_meta or {}).get("assignee_email")

        # ── Step 2: Orchestrator decides ─────────────────────────
        registry = SkillRegistry()
        decision = orchestrator.decide(ticket_id, registry, ticket=ticket_meta)
        print(f"  Decision: skill={decision.get('skill')} "
              f"confidence={decision.get('confidence')}")

        # ── Step 3: Validate ─────────────────────────────────────
        # Inject ticket_id now so validation and execution both see it.
        decision.setdefault("inputs", {})["ticket_id"] = ticket_id

        skill_name = decision.get("skill")
        skill      = registry.get(skill_name) if skill_name else None

        if skill is None:
            missing_fields = decision.get("missing_fields", [])
            if missing_fields:
                # Orchestrator found the ticket but couldn't extract required fields
                print(f"  [Error] Missing fields: {missing_fields}")
                teams.notify_missing_fields(ticket_id, assignee_email, missing_fields)
            else:
                # Orchestrator couldn't identify which skill to use
                print(f"  [Error] Unknown or missing skill: '{skill_name}'")
                teams.notify_error(
                    ticket_id, assignee_email,
                    "Automation could not identify the correct action for this ticket.\n\n"
                    "Please review manually.",
                )
            return

        validation = validator.validate(decision, skill)
        if not validation["valid"]:
            errors_text = "\n".join(f"  \u2022 {e}" for e in validation["errors"])
            print(f"  [Validation failed]\n{errors_text}")
            teams.notify_error(
                ticket_id, assignee_email,
                f"Automation could not process this ticket automatically.\n\n"
                f"Reason:\n{errors_text}\n\nPlease review manually.",
            )
            return

        # ── Step 4: Execute skill ─────────────────────────────────
        inputs = decision.get("inputs", {})

        _execute_start = time.time()
        result = skill.execute(inputs)
        elapsed_seconds = int(time.time() - _execute_start)
        _summaries = result.get("all_summaries", [])
        _all_ok    = all(s.get("success") for s in _summaries) if _summaries else False
        _upload_status = "ok" if _all_ok else ("partial_failure" if _summaries else "none")
        print(f"  [Execute] status={result.get('status')} "
              f"uploads={_upload_status} elapsed={elapsed_seconds}s")

        # ── Step 5: Build customer-facing reply ───────────────────
        all_summaries = result.get("all_summaries", [])
        skipped_notes = result.get("skipped_notes", [])

        if all_summaries:
            if skill_name == "kwg_se_upload":
                customer_message = build_kwg_se_reply(all_summaries, skipped_notes)
            else:
                customer_message = build_reply(all_summaries, skipped_notes)
        else:
            notes = "\n".join(f"  \u26a0 {n}" for n in skipped_notes)
            customer_message = (
                "Hi, I could not process your request.\n\n"
                + (notes if notes else "No files could be matched to account mappings.")
                + "\n\nPlease check your ticket description and resubmit."
            )

        # ── Step 6: Build agent-facing AI summary + send to Teams ───
        agent_note = reporter.build_note(skill_name, inputs, result, elapsed_seconds)
        any_upload_failed = any(
            not s.get("success") for s in result.get("all_summaries", [])
        )
        teams.notify(ticket_id, assignee_email, customer_message, agent_note,
                     failed=any_upload_failed)

        print(f"\n{'=' * 55}")
        print(f"  [Pipeline] Done -- ticket #{ticket_id}")
        print(f"{'=' * 55}")

    except TimeoutError as e:
        print(f"[Pipeline] TIMEOUT on ticket #{ticket_id}: {e}")
        try:
            teams.notify_error(
                ticket_id, assignee_email,
                "Automation timed out waiting for a response.\n\n"
                "Please ensure Cowork is running and has the project folder open, "
                "then re-add the ai_agent_automation tag to retry.",
            )
        except Exception:
            pass

    except Exception as e:
        import traceback, json as _json
        print(f"[Pipeline] FATAL ERROR on ticket #{ticket_id}: {e}")
        traceback.print_exc()
        try:
            if isinstance(e, _json.JSONDecodeError):
                friendly = (
                    "The AI model returned an unexpected response and automation could not continue.\n\n"
                    "This is usually transient. Re-add the ai_agent_automation tag to retry.\n\n"
                    "If it fails again, please review the ticket manually."
                )
            elif isinstance(e, (ConnectionError, OSError)):
                friendly = (
                    "Automation could not reach an external service (Zendesk or BrightEdge).\n\n"
                    "Check your network/VPN, then re-add the ai_agent_automation tag to retry."
                )
            else:
                friendly = (
                    "Automation stopped due to an internal error.\n\n"
                    "Please review this ticket manually. "
                    "Check the server logs for technical details."
                )
            teams.notify_error(ticket_id, assignee_email, friendly)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    from tools.attachment import cleanup_old_tmp_files
    cleanup_old_tmp_files()

    print("=" * 55)
    print("  BrightEdge Keyword Upload AI Agent")
    print("=" * 55)
    print("  Webhook : POST http://localhost:8000/webhook/zendesk")
    print("  Health  : GET  http://localhost:8000/health")
    print()
    print("  Run ngrok in another terminal:")
    print("    .\\ngrok.exe http 8000")
    print("=" * 55)

    uvicorn.run(app, host="0.0.0.0", port=8000)
