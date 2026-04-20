# ============================================================
#  tools/zendesk.py — All Zendesk API interactions
#
#  Functions:
#    fetch_ticket(ticket_id)       → ticket info dict or None
#    post_reply(ticket_id, ...)    → posts a comment on the ticket
#    download_file(file_info)      → raw bytes of an attachment
# ============================================================

import os
import requests

ZENDESK_SUBDOMAIN    = os.environ["ZENDESK_SUBDOMAIN"]
ZENDESK_EMAIL        = os.environ["ZENDESK_EMAIL"]
ZENDESK_API_TOKEN    = os.environ["ZENDESK_API_TOKEN"]
SUPPORTED_EXTENSIONS = (".csv", ".xlsx", ".xls", ".tsv")
SUPPORTED_MIME_TYPES = (
    "text/csv",
    "application/csv",
    "text/plain",
    "text/tab-separated-values",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
)


def _base():
    return f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"


def _auth():
    return (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)


# ─────────────────────────────────────────────
#  Public functions
# ─────────────────────────────────────────────

def fetch_ticket(ticket_id):
    """
    Fetches a Zendesk ticket and finds all CSV/Excel file attachments.

    Scans ALL comments for BrightEdge credentials — not just the first.
    Internal notes are checked first (agent adds credentials there),
    then falls back to the original description.

    Returns:
        {
            ticket_id:      str,
            subject:        str,
            body:           str,   <- all comment text, internal notes first
            keyword_files:  [{ filename, url, extension }, ...],
            assignee_email: str | None,
            auth:           tuple,
        }
        or None if the ticket should be skipped.
    """
    print(f"\n[Zendesk] Fetching ticket #{ticket_id}...")

    ticket_resp = requests.get(
        f"{_base()}/tickets/{ticket_id}.json?include=users", auth=_auth()
    )
    ticket_resp.raise_for_status()
    data    = ticket_resp.json()
    ticket  = data["ticket"]
    subject = ticket["subject"]

    # Extract assignee email from sideloaded users
    users          = {u["id"]: u for u in data.get("users", [])}
    assignee_id    = ticket.get("assignee_id")
    assignee_email = users[assignee_id]["email"] if assignee_id and assignee_id in users else None
    print(f"  Assignee: {assignee_email or '(unassigned)'}")
    print(f"  Subject : {subject}")

    comments_resp = requests.get(
        f"{_base()}/tickets/{ticket_id}/comments.json", auth=_auth()
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()["comments"]

    # ── Credentials: always from the latest internal note ───────────────
    # Walk comments newest-first and use the first internal note that
    # contains BrightEdge credentials. Public comments / older notes
    # are only used as fallback body text.
    credential_keywords = ["brightedge username", "be username", "account id"]

    def _has_credentials(text):
        return any(k in text.lower() for k in credential_keywords)

    latest_cred_text = None
    internal_texts   = []
    public_texts     = []

    for comment in reversed(comments):
        text   = comment.get("body", "").strip()
        public = comment.get("public", True)
        if not text:
            continue
        if not public:
            if latest_cred_text is None and _has_credentials(text):
                latest_cred_text = text
            internal_texts.append(text)
        else:
            public_texts.append(text)

    # Use latest credentialed note as primary body; append rest for context
    if latest_cred_text:
        other_texts = [t for t in internal_texts if t != latest_cred_text] + public_texts
        body = latest_cred_text + ("\n\n" + "\n\n".join(other_texts) if other_texts else "")
    else:
        body = "\n\n".join(internal_texts + public_texts)

    print(f"  Comments : {len(internal_texts)} internal, {len(public_texts)} public")
    if latest_cred_text:
        print(f"  Credentials found in latest internal note.")

    # ── Files: latest comment that has any supported attachment ──────────
    # Walk newest-first and stop at the first comment with attachments.
    # If that comment has ONE file  → single-account mode (no File: label needed).
    # If that comment has MULTIPLE  → multi-file mode (File: + Account ID required).
    keyword_files = []

    for comment in reversed(comments):
        for att in comment.get("attachments", []):
            _, ext = os.path.splitext(att["file_name"].lower())
            if ext in SUPPORTED_EXTENSIONS or att["content_type"] in SUPPORTED_MIME_TYPES:
                keyword_files.append({
                    "filename":  att["file_name"],
                    "url":       att["content_url"],
                    "extension": ext,
                })
        if keyword_files:
            cid = comment.get("id", "?")
            print(f"  Using {len(keyword_files)} file(s) from comment {cid} "
                  f"({'multi-file mode' if len(keyword_files) > 1 else 'single-file mode'})")
            break

    for f in keyword_files:
        print(f"  File     : {f['filename']} ({f['extension']})")

    if not keyword_files:
        print("  No supported file attachments found.")
        return None

    return {
        "ticket_id":      ticket_id,
        "subject":        subject,
        "body":           body,
        "keyword_files":  keyword_files,
        "assignee_email": assignee_email,
        "auth":           _auth(),
    }


def download_file(file_info):
    """Downloads a Zendesk attachment and returns its raw bytes."""
    print(f"  Downloading: {file_info['filename']}...")
    resp = requests.get(file_info["url"], auth=_auth())
    resp.raise_for_status()
    print(f"  Downloaded {len(resp.content):,} bytes.")
    return resp.content


def set_status(ticket_id, status: str = "open"):
    """
    Updates the Zendesk ticket status without posting a comment.

    Args:
        ticket_id: Zendesk ticket ID
        status:    Target status — "open", "pending", "solved", etc.
    """
    payload = {"ticket": {"status": status}}
    resp = requests.put(
        f"{_base()}/tickets/{ticket_id}.json",
        auth=_auth(),
        json=payload,
    )
    resp.raise_for_status()
    print(f"  [Zendesk] Ticket #{ticket_id} status set to '{status}'.")


def post_reply(ticket_id, message, public=True):
    """
    Posts a comment on a Zendesk ticket and sets status to open.

    Args:
        ticket_id: Zendesk ticket ID
        message:   Comment body text
        public:    True = visible to requester, False = internal note only
    """
    payload = {
        "ticket": {
            "status":  "open",
            "comment": {"body": message, "public": public},
        }
    }
    resp = requests.put(
        f"{_base()}/tickets/{ticket_id}.json",
        auth=_auth(),
        json=payload,
    )
    resp.raise_for_status()
    label = "public reply" if public else "internal note"
    print(f"  Posted {label} on ticket #{ticket_id} (status → open).")
