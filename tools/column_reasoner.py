# ============================================================
#  tools/column_reasoner.py — AI-powered column mapping fallback
#
#  Called by tools/transformer.map_columns() when fuzzy matching
#  cannot confidently identify the keyword column.
#
#  Modes (set via .env):
#    COWORK_ORCHESTRATOR=true  — file-based handoff to Cowork
#    (default)                 — Anthropic API (requires API key)
#
#  Public functions:
#    reason_columns(headers, sample_rows) -> dict | None
# ============================================================

import json
import os

REQUEST_FILE  = "cowork_column_request.json"
RESPONSE_FILE = "cowork_column_response.json"

_SYSTEM_PROMPT = """
Map CSV columns to BrightEdge fields. Return ONLY JSON, no prose:
{"keyword": "<col or null>", "plp": "<col or null>", "groups": ["<col>", ...]}
Rules: exact column names; keyword=search term/kw (required); plp=URL/landing page (optional); groups=keyword group columns in order; exclude analytics (impressions, clicks, volume, CTR, CPC).
"""


def reason_columns(headers: list, sample_rows: list) -> dict | None:
    """
    AI-powered column mapping fallback.

    Called when fuzzy matching confidence is below FUZZY_SHEET_THRESHOLD.

    Modes:
      COWORK_ORCHESTRATOR=true — saves headers + sample rows to
        cowork_column_request.json, polls for cowork_column_response.json.
      (default) — calls Anthropic API directly.

    Args:
        headers:     List of column name strings
        sample_rows: First N rows of data (up to 5 used)

    Returns:
        {
            "keyword":    "<col name or None>",
            "plp":        "<col name or None>",
            "groups":     ["<col1>", ...],
            "source":     "claude",
            "confidence": 1.0,
        }
        or None if reasoning fails or returns an unparseable response.
    """
    if os.environ.get("COWORK_ORCHESTRATOR", "").lower() == "true":
        return _reason_via_cowork(headers, sample_rows)
    return _reason_via_api(headers, sample_rows)


def _reason_via_cowork(headers: list, sample_rows: list) -> dict | None:
    """
    Cowork column reasoning mode.

    Saves headers + sample rows to cowork_column_request.json and polls
    for cowork_column_response.json (written by Cowork after reasoning).
    """
    import time

    POLL_INTERVAL = 2    # seconds between checks
    TIMEOUT       = 300  # 5 minutes max wait

    sample = sample_rows[:2]

    # Clear stale response from a previous run
    if os.path.exists(RESPONSE_FILE):
        os.remove(RESPONSE_FILE)

    with open(REQUEST_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "headers":     headers,
            "sample_rows": sample,
            "system_prompt": _SYSTEM_PROMPT.strip(),
        }, f, indent=2)

    print(f"  [ColumnReasoner] Cowork mode — saved {REQUEST_FILE}")
    print(f"  [ColumnReasoner] Waiting for Cowork to write {RESPONSE_FILE} ...")
    print(f"  [ColumnReasoner] (Switch to Cowork and say: 'read {REQUEST_FILE} and give me the column mapping JSON')")

    start = time.time()
    while time.time() - start < TIMEOUT:
        if os.path.exists(RESPONSE_FILE):
            with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            return _parse_mapping(raw)
        time.sleep(POLL_INTERVAL)

    print(f"  [ColumnReasoner] WARNING - Cowork did not write {RESPONSE_FILE} within {TIMEOUT}s.")
    return None


def _reason_via_api(headers: list, sample_rows: list) -> dict | None:
    """Anthropic API column reasoning — active when COWORK_ORCHESTRATOR is not set."""
    # Anthropic client — initialised lazily so import doesn't fail when
    # ANTHROPIC_API_KEY is absent.
    # TODO: Restore when Anthropic API credits are available.
    _client = None

    def _get_client():
        nonlocal _client
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return _client

    sample = sample_rows[:2]
    user_content = (
        f"Headers: {json.dumps(headers)}\n"
        f"Rows: {json.dumps(sample)}"
    )

    try:
        client   = _get_client()
        print(f"  [ColumnReasoner] API call → claude-haiku-4-5-20251001 | headers: {headers}")
        response = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 150,
            system     = _SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        print(f"  [ColumnReasoner] Response ({response.usage.input_tokens} in / {response.usage.output_tokens} out tokens): {raw}")
        return _parse_mapping(raw)
    except json.JSONDecodeError as e:
        print(f"  [ColumnReasoner] WARNING - could not parse Claude response as JSON: {e}")
        return None
    except Exception as e:
        print(f"  [ColumnReasoner] WARNING - API call failed: {e}")
        return None


def _parse_mapping(raw: str) -> dict | None:
    """Validates and normalises a JSON column mapping string."""
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [ColumnReasoner] WARNING - could not parse response as JSON: {e}")
        return None

    if not isinstance(mapping, dict) or "keyword" not in mapping:
        print("  [ColumnReasoner] Response missing required keys — treating as failed.")
        return None

    if not isinstance(mapping.get("groups"), list):
        mapping["groups"] = []

    mapping["source"]     = "claude"
    mapping["confidence"] = 1.0

    print(f"  [ColumnReasoner] Mapping resolved: {mapping}")
    return mapping
