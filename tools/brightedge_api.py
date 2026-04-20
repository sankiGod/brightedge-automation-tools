# ============================================================
#  tools/brightedge_api.py — BrightEdge API 3.0 client
#
#  Used for post-upload QA verification: confirms that uploaded
#  keywords and keyword groups actually appear in the account.
#
#  Auth: HTTP Basic Auth (same username + BRIGHTEDGE_PASSWORD
#        as the Playwright login).
#
#  Public functions:
#    get_keywords(account_id, username)                         → set of lowercase str
#    get_keyword_groups(account_id, username)                   → { name_lower: id }
#    get_keywords_in_group(account_id, kwg_id, username)        → set of lowercase str
#    verify_keywords(account_id, username, expected, delay=15)  → dict
#    verify_kwg_names(account_id, username, expected, delay=15) → dict
#    verify_keyword_groups(account_id, username, groups, delay=15) → dict
# ============================================================

import os
import time
import requests

BRIGHTEDGE_PASSWORD = os.environ["BRIGHTEDGE_PASSWORD"]
BE_API_BASE         = "https://api.brightedge.com/3.0"


def _auth(username: str):
    return (username, BRIGHTEDGE_PASSWORD)


# ─────────────────────────────────────────────
#  Raw API fetchers
# ─────────────────────────────────────────────

def get_keywords(account_id: str, username: str) -> set:
    """
    Fetches all tracked keywords for an account using pagination.

    BrightEdge returns max 5000 keywords per call. Loops with offset
    increments until offset >= total.

    Returns:
        set of lowercase keyword strings.
    """
    keywords = set()
    offset   = 0
    count    = 5000

    while True:
        url  = (f"{BE_API_BASE}/objects/keywords/{account_id}"
                f"?offset={offset}&count={count}")
        resp = requests.get(url, auth=_auth(username), timeout=30)
        resp.raise_for_status()
        data     = resp.json()
        items    = data.get("keywords", [])
        total    = int(data.get("total", 0))
        returned = len(items)

        for item in items:
            kw = item.get("keyword", "").strip()
            if kw:
                keywords.add(kw.lower())

        offset += returned
        if offset >= total or not items:
            break

    print(f"  [API] Account {account_id}: {len(keywords)} keywords fetched "
          f"(total in account: {total if 'total' in dir() else '?'})")
    return keywords


def get_keyword_groups(account_id: str, username: str) -> dict:
    """
    Fetches all keyword groups for an account.

    Returns:
        { name_lower: id } dict — no pagination (all returned at once).
    """
    url  = f"{BE_API_BASE}/objects/keywordgroups/{account_id}"
    resp = requests.get(url, auth=_auth(username), timeout=30)
    resp.raise_for_status()
    data   = resp.json()
    groups = {}

    for item in data.get("keywordgroups", []):
        name = item.get("keywordgroup", "").strip()
        gid  = item.get("id", "")
        if name:
            groups[name.lower()] = gid

    print(f"  [API] Account {account_id}: {len(groups)} KWGs fetched")
    return groups


def get_keywords_in_group(account_id: str, kwg_id: str, username: str) -> set:
    """
    Fetches all keywords belonging to a specific keyword group.

    Endpoint: GET /objects/keywordgroups/<account_id>/<kwg_id>
    Assumed response: {"keywords": [{"keyword": "...", "id": "..."}, ...]}

    Returns:
        set of lowercase keyword strings.
    """
    url  = f"{BE_API_BASE}/objects/keywordgroups/{account_id}/{kwg_id}"
    resp = requests.get(url, auth=_auth(username), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    kws  = set()

    for item in data.get("keywords", []):
        kw = item.get("keyword", "").strip()
        if kw:
            kws.add(kw.lower())

    print(f"  [API] Group {kwg_id}: {len(kws)} keywords fetched")
    return kws


# ─────────────────────────────────────────────
#  QA verification functions
# ─────────────────────────────────────────────

def verify_keywords(account_id: str, username: str,
                    expected: set, delay: int = 15) -> dict:
    """
    Waits `delay` seconds (propagation time) then checks that all expected
    keywords are present in the account via the BrightEdge API.

    Args:
        account_id: BrightEdge account ID string
        username:   BrightEdge login email (used for Basic Auth)
        expected:   set of keyword strings to confirm (case-insensitive)
        delay:      seconds to wait before querying (default 15)

    Returns:
        {
            "ok":             bool   — True if all expected keywords found
            "missing":        [str]  — lowercase keywords not found
            "found":          int    — number of expected keywords confirmed
            "total_expected": int    — total keywords we were looking for
        }
    """
    print(f"  [QA] Waiting {delay}s before keyword verification...")
    time.sleep(delay)

    try:
        existing = get_keywords(account_id, username)
    except Exception as e:
        print(f"  [QA] API call failed: {e}")
        return {
            "ok":             None,   # None = indeterminate (API error)
            "missing":        [],
            "found":          0,
            "total_expected": len(expected),
            "error":          str(e),
        }

    expected_lower = {k.lower() for k in expected}
    missing        = expected_lower - existing

    return {
        "ok":             len(missing) == 0,
        "missing":        sorted(missing),
        "found":          len(expected_lower) - len(missing),
        "total_expected": len(expected_lower),
    }


def verify_kwg_names(account_id: str, username: str,
                     expected: set, delay: int = 15) -> dict:
    """
    Waits `delay` seconds then checks that all expected KWG names are
    present in the account via the BrightEdge API.

    Note: There is no API endpoint to verify KWG→SE assignments.
    This function only confirms that KWG names exist in the account.

    Args:
        account_id: BrightEdge account ID string
        username:   BrightEdge login email (used for Basic Auth)
        expected:   set of KWG name strings to confirm (case-insensitive)
        delay:      seconds to wait before querying (default 15)

    Returns:
        {
            "ok":             bool   — True if all expected KWG names found
            "missing":        [str]  — lowercase KWG names not found
            "found":          int    — number of expected KWGs confirmed
            "total_expected": int    — total KWGs we were looking for
        }
    """
    print(f"  [QA] Waiting {delay}s before KWG name verification...")
    time.sleep(delay)

    try:
        existing = get_keyword_groups(account_id, username)
    except Exception as e:
        print(f"  [QA] API call failed: {e}")
        return {
            "ok":             None,
            "missing":        [],
            "found":          0,
            "total_expected": len(expected),
            "error":          str(e),
        }

    expected_lower = {n.lower() for n in expected}
    missing        = expected_lower - set(existing.keys())

    return {
        "ok":             len(missing) == 0,
        "missing":        sorted(missing),
        "found":          len(expected_lower) - len(missing),
        "total_expected": len(expected_lower),
    }


def verify_keyword_groups(account_id: str, username: str,
                          groups: dict, delay: int = 15) -> dict:
    """
    Waits `delay` seconds then verifies each keyword is present in its expected group.

    More precise than verify_keywords(): checks not just that the keyword exists
    somewhere in the account, but that it is actually in the correct group.

    Args:
        account_id: BrightEdge account ID string
        username:   BrightEdge login email (used for Basic Auth)
        groups:     {group_name: [{name, plp?}, ...]} — the uploaded groups dict
        delay:      seconds to wait before querying (default 15)

    Returns:
        {
            "ok":             bool | None — True if all found, False if some missing,
                                           None if API error
            "missing":        [{"keyword": str, "group": str}] — pairs not confirmed
            "found":          int    — number of (keyword, group) pairs confirmed
            "total_expected": int    — total pairs we were checking
            "error":          str    — only present when ok=None
        }
    """
    print(f"  [QA] Waiting {delay}s before keyword group verification...")
    time.sleep(delay)

    total = sum(len(kws) for kws in groups.values())

    try:
        existing_groups = get_keyword_groups(account_id, username)  # {name_lower: id}
    except Exception as e:
        print(f"  [QA] API call failed: {e}")
        return {
            "ok":             None,
            "missing":        [],
            "found":          0,
            "total_expected": total,
            "error":          str(e),
        }

    missing = []
    found   = 0

    for group_name, kws in groups.items():
        group_id = existing_groups.get(group_name.lower())

        if not group_id:
            print(f"  [QA] Group '{group_name}' not found in account")
            for kw in kws:
                missing.append({"keyword": kw["name"], "group": group_name})
            continue

        try:
            group_kw_set = get_keywords_in_group(account_id, group_id, username)
        except Exception as e:
            print(f"  [QA] Could not fetch keywords for group '{group_name}': {e}")
            for kw in kws:
                missing.append({"keyword": kw["name"], "group": group_name})
            continue

        for kw in kws:
            if kw["name"].lower() in group_kw_set:
                found += 1
            else:
                missing.append({"keyword": kw["name"], "group": group_name})

    return {
        "ok":             len(missing) == 0,
        "missing":        missing,
        "found":          found,
        "total_expected": total,
    }
