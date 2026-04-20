"""
Attachment Download Tool — fills the MCP gap.
Zendesk MCP server cannot return binary file bytes, so this custom tool
handles downloading CSV/Excel attachments from Zendesk ticket comments.
"""

import os
import time
from pathlib import Path
import requests


ZENDESK_SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
ZENDESK_EMAIL = os.environ["ZENDESK_EMAIL"]
ZENDESK_API_TOKEN = os.environ["ZENDESK_API_TOKEN"]
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}

# Project-local temp directory — all pipeline temp files go here.
TMP_DIR = Path(__file__).parent.parent / "tmp"


def cleanup_old_tmp_files(days: int = 10) -> None:
    """Deletes files in TMP_DIR older than `days` days. Never raises."""
    if not TMP_DIR.exists():
        return
    cutoff = time.time() - days * 86_400
    deleted = 0
    for f in TMP_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
    if deleted:
        print(f"[Cleanup] Removed {deleted} tmp file(s) older than {days} days.")


def download_attachment(url: str, filename: str, dest_dir: str = str(TMP_DIR)) -> str:
    """
    Downloads a Zendesk attachment to dest_dir.
    Returns the local file path.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)

    response = requests.get(
        url,
        auth=(f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN),
        stream=True,
        timeout=30,
    )
    response.raise_for_status()

    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    return dest_path


def get_latest_attachment(ticket_id: int) -> list[dict]:
    """
    Returns list of supported attachments from the latest comment that has attachments.
    Each item: {"filename": str, "url": str, "content_type": str}

    This mirrors the scoping rule: latest comment (any type) with a supported attachment.
    Used when the Zendesk MCP server cannot return file bytes.
    """
    base_url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
    auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)

    resp = requests.get(
        f"{base_url}/tickets/{ticket_id}/comments.json",
        auth=auth,
        timeout=15,
    )
    resp.raise_for_status()
    comments = resp.json().get("comments", [])

    # Walk comments in reverse — latest first
    for comment in reversed(comments):
        attachments = [
            {
                "filename": a["file_name"],
                "url": a["content_url"],
                "content_type": a["content_type"],
            }
            for a in comment.get("attachments", [])
            if any(a["file_name"].lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)
        ]
        if attachments:
            return attachments

    return []
