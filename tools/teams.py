# ============================================================
#  tools/teams.py — Microsoft Teams notifications via incoming webhook
#
#  Setup (one-time, per channel):
#    1. In Teams, open the target channel
#    2. Click ... -> Manage channel -> Edit (Connectors section)
#       OR click ... -> Connectors (classic Teams)
#    3. Search "Incoming Webhook" -> Add -> Add
#    4. Name it (e.g. "BrightEdge AI Agent"), click Create
#    5. Copy the webhook URL -> TEAMS_WEBHOOK_URL in .env
#       URL looks like: https://brightedge.webhook.office.com/webhookb2/...
#
#  Public functions:
#    notify(ticket_id, assignee_email, customer_message, agent_note)
#    notify_error(ticket_id, assignee_email, error_text)
# ============================================================

import os
import requests

TEAMS_WEBHOOK_URL  = os.environ["TEAMS_WEBHOOK_URL"]
ZENDESK_SUBDOMAIN  = os.environ["ZENDESK_SUBDOMAIN"]

# Timeout for the webhook POST (seconds)
_TIMEOUT = 10


def _ticket_url(ticket_id: str) -> str:
    return f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{ticket_id}"


def _view_ticket_action(ticket_id: str) -> dict:
    return {
        "type":    "Action.OpenUrl",
        "title":   "View Ticket",
        "url":     _ticket_url(ticket_id),
        "style":   "positive",
    }


def _set_ticket_open(ticket_id: str):
    """Sets the Zendesk ticket to open. Never raises."""
    try:
        from tools.zendesk import set_status
        set_status(ticket_id, "open")
    except Exception as e:
        print(f"  [Teams] WARNING - could not set ticket #{ticket_id} to open: {e}")


def notify(ticket_id: str, assignee_email: str | None,
           customer_message: str, agent_note: str, failed: bool = False):
    """
    Posts an upload result Adaptive Card to the configured Teams channel.

    Uses msteams.entities to @mention the assignee by email (UPN) — this
    triggers a real Teams notification for the user, not just visual text.

    Pass failed=True to switch the header to red (attention style).

    Never raises — pipeline must not fail due to a notification error.
    """
    mention_text, entities = _build_mention(assignee_email)
    for_line = f"For: {mention_text}" if mention_text else f"For: (unassigned)"

    header_style = "attention" if failed else "accent"
    header_title = (f"Upload Failed \u2014 Ticket #{ticket_id}" if failed
                    else f"Keyword Upload \u2014 Ticket #{ticket_id}")

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.2",
                "body": [
                    {
                        "type": "Container",
                        "style": header_style,
                        "items": [{
                            "type": "TextBlock",
                            "size": "Medium",
                            "weight": "Bolder",
                            "text": header_title,
                            "color": "Light",
                        }],
                    },
                    {
                        "type": "TextBlock",
                        "text": for_line,
                        "wrap": True,
                        "spacing": "Small",
                        "isSubtle": True,
                    },
                    *_message_to_blocks(customer_message),
                    {
                        "type": "TextBlock",
                        "text": "AI Agent Summary",
                        "weight": "Bolder",
                        "separator": True,
                        "spacing": "Medium",
                    },
                    *_message_to_blocks(agent_note, subtle=True),
                ],
                "actions": [_view_ticket_action(ticket_id)],
                "msteams": {"entities": entities},
            },
        }],
    }

    _set_ticket_open(ticket_id)
    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=card, timeout=_TIMEOUT)
        resp.raise_for_status()
        tag = f"@{assignee_email}" if assignee_email else "(unassigned)"
        print(f"  [Teams] Notification sent for ticket #{ticket_id} to {tag}")
    except Exception as e:
        print(f"  [Teams] WARNING - notification failed for ticket #{ticket_id}: {e}")


def notify_missing_fields(ticket_id: str, assignee_email: str | None, missing_fields: list):
    """
    Posts a structured 'missing information' Adaptive Card to the Teams channel.

    Each item in missing_fields should be a human-readable string describing
    what is missing and how to fix it. The card always includes instructions
    to re-add the ai_agent_automation tag once fixed.

    This function is designed to be called by both the mock orchestrator and
    the future Claude orchestrator -- both populate missing_fields with the
    same descriptive string format.
    """
    mention_text, entities = _build_mention(assignee_email)
    for_line = f"For: {mention_text}" if mention_text else "For: (unassigned)"

    # Build one TextBlock bullet per missing field
    missing_items = [
        {
            "type":  "TextBlock",
            "text":  f"\u2022 {field}",
            "wrap":  True,
            "color": "Attention",
        }
        for field in missing_fields
    ]

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type":    "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.2",
                "body": [
                    {
                        "type":  "Container",
                        "style": "attention",
                        "items": [{
                            "type":   "TextBlock",
                            "size":   "Medium",
                            "weight": "Bolder",
                            "text":   f"Action Required \u2014 Ticket #{ticket_id}",
                            "color":  "Light",
                        }],
                    },
                    {
                        "type": "TextBlock",
                        "text": for_line,
                        "wrap": True,
                    },
                    {
                        "type":   "TextBlock",
                        "text":   "The following information could not be found in the ticket:",
                        "wrap":   True,
                        "weight": "Bolder",
                        "spacing": "Medium",
                    },
                    *missing_items,
                    *_message_to_blocks(
                        "To restart automation: add the missing details as an internal note, "
                        "then re-add the tag ai_agent_automation to the ticket.",
                        subtle=True,
                    ),
                ],
                "actions": [_view_ticket_action(ticket_id)],
                "msteams": {"entities": entities},
            },
        }],
    }

    _set_ticket_open(ticket_id)
    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=card, timeout=_TIMEOUT)
        resp.raise_for_status()
        print(f"  [Teams] Missing-fields card sent for ticket #{ticket_id}")
    except Exception as e:
        print(f"  [Teams] WARNING - missing-fields card failed for ticket #{ticket_id}: {e}")


def notify_error(ticket_id: str, assignee_email: str | None, error_text: str):
    """
    Posts a failure / manual-review Adaptive Card to the Teams channel.
    Uses attention (red) container style for the header.
    """
    mention_text, entities = _build_mention(assignee_email)
    for_line = f"For: {mention_text}" if mention_text else "For: (unassigned)"

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.2",
                "body": [
                    {
                        "type": "Container",
                        "style": "attention",
                        "items": [{
                            "type": "TextBlock",
                            "size": "Medium",
                            "weight": "Bolder",
                            "text": f"Action Required \u2014 Ticket #{ticket_id}",
                            "color": "Light",
                        }],
                    },
                    {
                        "type": "TextBlock",
                        "text": for_line,
                        "wrap": True,
                    },
                    *_message_to_blocks(error_text),
                ],
                "actions": [_view_ticket_action(ticket_id)],
                "msteams": {"entities": entities},
            },
        }],
    }

    _set_ticket_open(ticket_id)
    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=card, timeout=_TIMEOUT)
        resp.raise_for_status()
        print(f"  [Teams] Error card sent for ticket #{ticket_id}")
    except Exception as e:
        print(f"  [Teams] WARNING - error card failed for ticket #{ticket_id}: {e}")


# ─────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────

def _message_to_blocks(text: str, subtle: bool = False) -> list:
    """
    Converts a plain-text message into a list of Adaptive Card body elements.

    Each non-empty line becomes its own TextBlock so Teams renders them on
    separate lines (single \\n inside one TextBlock is not reliable in Teams).

    Key : Value lines (containing " : ") are grouped into a FactSet for
    cleaner aligned rendering. A blank line flushes any pending FactSet.

    Args:
        text:   Multi-line plain text (output of build_reply or build_note)
        subtle: If True, renders text in a subtle (grey) colour

    Returns:
        List of Adaptive Card body element dicts
    """
    blocks       = []
    fact_buffer  = []   # accumulates {"title": ..., "value": ...} pairs

    def _flush_facts():
        if fact_buffer:
            blocks.append({"type": "FactSet", "facts": list(fact_buffer), "spacing": "Small"})
            fact_buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            _flush_facts()
            continue

        # Key : Value lines → FactSet (e.g. "  Keywords added  : 272")
        if " : " in line and not line.startswith(("\u2022", "\u26a0", "Hi,", "All ", "Please", "For ")):
            parts = line.split(" : ", 1)
            fact_buffer.append({"title": parts[0].strip(), "value": parts[1].strip()})
            continue

        # Any other content flushes the fact buffer first
        _flush_facts()

        block = {"type": "TextBlock", "wrap": True, "spacing": "Small"}
        if subtle:
            block["isSubtle"] = True

        # Section headers (no leading bullet/indent, not a sentence)
        if not line.startswith(("\u2022", "\u26a0", " ")) and line.endswith(":"):
            block["weight"] = "Bolder"
            block["spacing"] = "Medium"
            block["text"] = line
        else:
            block["text"] = line

        blocks.append(block)

    _flush_facts()
    return blocks


def _display_name(email: str) -> str:
    """
    Derives a display name from an email address.
    ankit.singh@brightedge.com -> Ankit Singh
    """
    local = email.split("@")[0]
    parts = local.replace(".", " ").replace("_", " ").split()
    return " ".join(p.title() for p in parts)


def _build_mention(email: str | None) -> tuple[str, list]:
    """
    Returns (mention_text, entities_list) for an Adaptive Card @mention.
    mention_text is the <at>Name</at> string to embed in a TextBlock.
    Returns ("", []) if email is None.
    """
    if not email:
        return "", []
    name = _display_name(email)
    mention_text = f"<at>{name}</at>"
    entities = [{
        "type": "mention",
        "text": mention_text,
        "mentioned": {"id": email, "name": name},
    }]
    return mention_text, entities


def _plain_to_text(text: str) -> str:
    """
    Converts plain text for an Adaptive Card TextBlock.
    Normalises bullet characters; preserves line breaks via newline joining.
    (Adaptive Cards do not render HTML — no <br> tags needed.)
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("\u2022", "-", "\u26a0", "\u2713")):
            lines.append("\u2022 " + stripped.lstrip("\u2022-\u26a0\u2713 "))
        else:
            lines.append(stripped)
    return "\n".join(lines)
