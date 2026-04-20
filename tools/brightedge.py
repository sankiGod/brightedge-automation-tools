# ============================================================
#  tools/brightedge.py — BrightEdge bulk keyword upload via Playwright
#
#  Uses the BrightEdge admin Mass Account Keyword Upload UI.
#  All logic validated against the live BrightEdge admin.
#
#  Login flow:
#    1. https://www.brightedge.com/secure/login  (always same)
#    2. Accept cookie banner if present
#    3. Submit username + universal password
#    4. Read redirect URL to discover pod (app4, app5, app9...)
#
#  Upload flow per account:
#    1. Navigate to /admin/edit_account_details/{account_id}
#       — sets UI context to the correct account
#    2. Navigate to /admin/mass_account_kwupload
#    3. Set TSV file on the file input, wait for client-side validation
#    4. Click Next (retries if BrightEdge returns to the file-selection form)
#    5. Wait for "File Upload Response:" section to appear
#    6. Parse and verify the response
#    7. Logout, close browser
#
#  Public functions:
#    upload_to_brightedge(username, account_id, groups) → summary dict
# ============================================================

import os
import re
import time
import tempfile
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tools.transformer import build_tsv
from tools.attachment import TMP_DIR

BRIGHTEDGE_PASSWORD = os.environ["BRIGHTEDGE_PASSWORD"]
BE_LOGIN_URL        = os.environ.get("BE_LOGIN_URL", "https://www.brightedge.com/secure/login")

# ── Timeouts (milliseconds) ───────────────────────────────────
TIMEOUT_LOGIN = 60_000   # login page + redirect
TIMEOUT_NAV   = 15_000   # page navigation
TIMEOUT_POLL  = 15_000   # domcontentloaded wait (used in navigate)

# ── KWG SE Upload ─────────────────────────────────────────────
KWG_SE_UPLOAD_PATH = "/admin/mass_account_kwg_se_upload"


# ─────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────

def _login(page, username):
    """
    Navigates to the login page, accepts cookie banner if present,
    submits credentials, waits for redirect, and returns the pod hostname.
    """
    print(f"  [Login] Navigating to {BE_LOGIN_URL}...")
    page.goto(BE_LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT_LOGIN)

    # Accept cookie banner if it appears
    try:
        btn = page.locator("text=OK, I agree")
        btn.wait_for(state="visible", timeout=5_000)
        btn.click()
        print("  [Login] Cookie banner dismissed.")
    except Exception:
        pass

    # Fill credentials
    page.fill("input[type='email']",    username)
    page.fill("input[type='password']", BRIGHTEDGE_PASSWORD)

    # Submit and detect login failure before waiting for redirect
    page.click("button:has-text('LOGIN'), input[value='LOGIN']")
    page.wait_for_timeout(3_000)

    failure_text = page.inner_text("body").lower()
    if "login attempt has failed" in failure_text or \
       "username or password" in failure_text:
        raise Exception(
            "BrightEdge login failed — check BRIGHTEDGE_PASSWORD in .env "
            "and that the username is correct."
        )

    # Wait for redirect to pod URL
    page.wait_for_url(
        lambda url: "brightedge.com" in url and "secure/login" not in url,
        timeout=TIMEOUT_LOGIN,
    )

    match = re.search(r"(app\d+\.brightedge\.com)", page.url)
    if not match:
        raise Exception(
            f"Logged in but could not detect pod in URL: {page.url}"
        )

    pod = match.group(1)
    print(f"  [Login] Success. Pod: {pod}")
    return pod


def _switch_account(page, pod, account_id):
    """
    Navigates to edit_account_details/{account_id} to set the correct
    account context before going to the upload page.
    """
    url = f"https://{pod}/admin/edit_account_details/{account_id}"
    print(f"  [Nav] Switching to account {account_id}...")
    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)


def _navigate_to_upload(page, pod):
    """
    Navigates directly to the Mass Account Keyword Upload page.
    Must be called after _switch_account() so the context is correct.
    """
    url = f"https://{pod}/admin/mass_account_kwupload"
    print(f"  [Nav] Navigating to upload page...")
    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)

    # Confirm the file input is ready
    page.locator("input[type='file']").wait_for(state="visible", timeout=TIMEOUT_NAV)
    print(f"  [Nav] Upload page ready.")


def _upload_and_poll(page, tsv_content):
    """
    Writes TSV to a temp file, uploads it, clicks Next, then waits
    for the response div to become visible using exact DOM selectors
    discovered via DevTools inspection.

    DOM facts (confirmed via DevTools):
      - Response container : div#massAccountUploadResponseHeader
          hidden  → processing in progress
          display:block → upload complete, response text inside
      - The `hidden` attribute is NEVER removed — only style="display: block"
        is added on completion. Checking element.hidden always returns true.
        Must check resp.style.display === 'block'.

    No polling loop — uses wait_for_function which reacts the instant
    the DOM changes, so response is captured as soon as BrightEdge
    finishes processing.

    Returns:
        {
            "success":        bool,
            "message":        str,
            "added_count":    int,
            "new_groups":     [str],
            "invalid_urls":   [str],
            "response_lines": [str],
        }
    """
    TMP_DIR.mkdir(exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8", dir=str(TMP_DIR)
    )
    tmp.write(tsv_content)
    tmp.close()

    print(f"  [Upload] Temp file: {tmp.name}")

    # Confirmed via DevTools — exact IDs from BrightEdge DOM
    FILE_INPUT_ID      = "massAccountImportCSVFile"
    UPLOAD_TIMEOUT_MS  = 600_000   # 10 minutes max

    try:
        # Select file and click Next
        page.locator(f"#{FILE_INPUT_ID}").set_input_files(tmp.name)
        print(f"  [Upload] File selected. Clicking Next...")
        page.click("#massAccountFileUploadNext", timeout=TIMEOUT_NAV, no_wait_after=True)
        print(f"  [Upload] Waiting for BrightEdge to process "
              f"(max {UPLOAD_TIMEOUT_MS // 1000}s)...")

        # Wait for upload to complete or get stuck.
        #
        # DOM signals (confirmed via DevTools):
        #   Loading active : <body class="loading">
        #   Loading gone   : <body class="">  (empty — loading class removed)
        #   Upload done    : massAccountUploadResponseHeader style="display: block"
        #
        # Stuck state: body.loading disappears WITHOUT the response div appearing.
        # This happens when BrightEdge servers are slow (e.g. weekend collections) —
        # clicking Next again prompts the server to return the result.

        def _click_next_and_wait_for_loading():
            """Click Next and wait for body.loading to appear before watching for completion."""
            page.click("#massAccountFileUploadNext", timeout=TIMEOUT_NAV)
            # Wait for loading class to be added — guards against a race where
            # wait_for_function runs before body.loading appears and returns 'stuck' early.
            try:
                page.wait_for_function(
                    "() => document.body.classList.contains('loading')",
                    timeout=5_000,
                )
            except PlaywrightTimeoutError:
                pass  # loading may have started and finished extremely fast

        _click_next_and_wait_for_loading()

        # The JS function returns:
        #   'done'  — response appeared, parse it
        #   'stuck' — loading stopped without response, click Next to prompt server
        #   false   — still loading, keep waiting
        while True:
            try:
                handle = page.wait_for_function(
                    """() => {
                        const resp = document.getElementById('massAccountUploadResponseHeader');
                        if (resp && resp.style.display === 'block') return 'done';
                        if (!document.body.classList.contains('loading')) return 'stuck';
                        return false;
                    }""",
                    timeout=UPLOAD_TIMEOUT_MS,
                )
                state = handle.json_value()
            except PlaywrightTimeoutError:
                return {
                    "success": False,
                    "message": f"Upload timed out after "
                               f"{UPLOAD_TIMEOUT_MS // 1000}s — "
                               f"BrightEdge did not respond.",
                    "added_count": 0, "new_groups": [], "invalid_urls": [],
                    "response_lines": [],
                }

            if state == "done":
                break

            # Stuck — loading stopped without a response
            print(f"  [Upload] Loading stopped without a response "
                  f"— clicking Next to prompt server...")
            _click_next_and_wait_for_loading()

        print(f"  [Upload] Response appeared — upload complete.")
        page_text = page.inner_text("body")
        return _parse_response(page_text)

    finally:
        try:
            time.sleep(1)      # Windows file lock buffer
            os.unlink(tmp.name)
        except Exception:
            pass


def _parse_response(page_text):
    """
    Extracts and classifies the File Upload Response section.

    BrightEdge response format (confirmed from live testing):
        File Upload Response:
        login@be.com: New Groups => [Group A][Group B]
        login@be.com: Added Keywords => 207
        login@be.com: Invalid URL => [url1][url2]
    """
    # Extract response lines
    lines          = page_text.splitlines()
    in_response    = False
    response_lines = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if "file upload response" in s.lower():
            in_response = True
            continue
        if in_response:
            if s in ("Next", "Cancel", "Choose File", "Manage Account"):
                break
            response_lines.append(s)

    # Parse each response line
    added_count  = 0
    new_groups   = []
    invalid_urls = []
    error_msgs   = []
    warning_msgs = []   # e.g. keyword limit reached

    for line in response_lines:
        ll = line.lower()
        if "added keywords =>" in ll:
            try:
                added_count = int(line.split("=>")[-1].strip())
            except ValueError:
                pass
        elif "new groups =>" in ll:
            raw    = line.split("=>")[-1].strip()
            parsed = re.findall(r'\[([^\]]+)\]', raw)
            new_groups.extend(parsed)
        elif "invalid url =>" in ll:
            invalid_urls.append(line.split("=>")[-1].strip())
        elif any(w in ll for w in ["limit", "maximum", "exceed", "quota", "max"]):
            # Keyword usage limit or similar quota messages
            warning_msgs.append(line)
        elif "error" in ll or "failed" in ll:
            error_msgs.append(line)

    is_success    = added_count > 0
    summary_parts = []
    if added_count:
        summary_parts.append(f"{added_count} keywords added")
    if new_groups:
        summary_parts.append(f"{len(new_groups)} new groups created")
    if invalid_urls:
        summary_parts.append(f"{len(invalid_urls)} invalid URL(s)")
    if warning_msgs:
        summary_parts.append(f"warnings: {'; '.join(warning_msgs)}")
    if error_msgs:
        summary_parts.append(f"errors: {'; '.join(error_msgs)}")

    return {
        "success":        is_success,
        "message":        " | ".join(summary_parts) if summary_parts else "(no message)",
        "added_count":    added_count,
        "new_groups":     new_groups,
        "invalid_urls":   invalid_urls,
        "warning_msgs":   warning_msgs,
        "error_msgs":     error_msgs,
        "response_lines": response_lines,   # raw lines for Zendesk reply
    }


def _logout(page):
    """Clicks Logout and waits for redirect to the public site."""
    print("  [Logout] Logging out...")
    try:
        page.click("text=Logout", timeout=10_000)
        page.wait_for_url(
            lambda url: "/admin" not in url and "/ui/" not in url,
            timeout=15_000,
        )
        print(f"  [Logout] Done. ({page.url})")
    except Exception as e:
        print(f"  [Logout] Failed (non-critical): {e}")


def _verify_groups(input_groups, result):
    """
    Compares input groups against the BrightEdge response.

    BrightEdge only reports NEWLY created groups — groups that already
    existed are not mentioned. So:
      new_groups   = groups BE created fresh this upload
      pre_existing = input groups not in new_groups (already existed)

    Prints a reconciliation table and returns a summary dict.
    """
    input_names   = set(input_groups.keys())
    new_names     = set(result.get("new_groups", []))
    pre_existing  = input_names - new_names
    unexpected    = new_names - input_names
    added_count   = result.get("added_count", 0)

    # Unique keywords = distinct (keyword, plp) pairs across all groups.
    # A keyword that belongs to multiple groups is ONE unique keyword in the TSV
    # and counted ONCE by BrightEdge's "Added Keywords" response.
    # Do NOT use sum(len(v) ...) — that counts keyword-group pairs, not unique keywords.
    unique_keywords = {
        (kw["name"].lower(), kw.get("plp", "").lower())
        for kws in input_groups.values()
        for kw in kws
    }
    total_unique = len(unique_keywords)

    print(f"\n  {'─' * 45}")
    print(f"  GROUP RECONCILIATION")
    print(f"  {'─' * 45}")
    print(f"  Input groups       : {len(input_names)}")
    print(f"  Unique keywords    : {total_unique}")
    print(f"  Keywords confirmed : {added_count}")
    print()

    if new_names:
        print(f"  Newly created ({len(new_names)}):")
        for g in sorted(new_names):
            print(f"    + {g} ({len(input_groups.get(g, []))} keywords)")

    if pre_existing:
        print(f"\n  Pre-existing ({len(pre_existing)}) — keywords merged in:")
        for g in sorted(pre_existing):
            print(f"    ~ {g} ({len(input_groups.get(g, []))} keywords)")

    if unexpected:
        print(f"\n  WARNING — reported by BrightEdge but not in input:")
        for g in sorted(unexpected):
            print(f"    ? {g}")

    print()
    if added_count == total_unique:
        print(f"  Keyword count : {added_count}/{total_unique} — MATCH ✓")
    elif added_count < total_unique:
        diff = total_unique - added_count
        be_lines = result.get("response_lines", [])
        be_msg   = " | ".join(be_lines) if be_lines else "see BrightEdge response"
        print(f"  Keyword count : {added_count}/{total_unique} — "
              f"{diff} fewer than sent")
        print(f"  BrightEdge says: {be_msg}")
    else:
        print(f"  Keyword count : {added_count}/{total_unique} — unexpected")

    return {
        "input_groups":    sorted(input_names),
        "new_groups":      sorted(new_names),
        "existing_groups": sorted(pre_existing),
        "unexpected":      sorted(unexpected),
        "keywords_sent":   total_unique,
        "keywords_added":  added_count,
        "counts_match":    added_count == total_unique,
    }


# ─────────────────────────────────────────────
#  Public functions
# ─────────────────────────────────────────────

def fetch_account_logins(username: str, account_ids: list) -> dict:
    """
    Logs into BrightEdge once, scrapes the manage_account admin page,
    and returns {account_id: login_email} for each requested account ID.

    Called once per ticket before the upload loop so every TSV row
    uses the correct account-scoped login email (not the org username).

    Args:
        username:    BrightEdge org-level login email
        account_ids: list of account ID strings to look up

    Returns:
        dict mapping str(account_id) → login_email string.
        Accounts not found in the table are omitted — callers should
        fall back to the org username if a key is missing.
    """
    target_ids = {str(aid) for aid in account_ids}
    logins: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()
        page.set_default_timeout(TIMEOUT_NAV)

        try:
            pod = _login(page, username)
            url = f"https://{pod}/admin/manage_account/"
            print(f"  [AccountLookup] {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)

            # Expand DataTables to show up to 100 rows at once
            try:
                page.select_option("select[name$='_length']", "100")
                page.wait_for_timeout(1_000)
            except Exception:
                pass

            # Scrape table pages until all target IDs are found or no Next page
            while True:
                rows = page.query_selector_all("table tbody tr")
                for row in rows:
                    cells = row.query_selector_all("td")
                    if len(cells) < 2:
                        continue
                    acct_id = cells[0].inner_text().strip()
                    login   = cells[1].inner_text().strip()
                    if acct_id in target_ids:
                        logins[acct_id] = login
                        print(f"  [AccountLookup] {acct_id} → {login}")

                # Stop if we found everything or there's no enabled Next button
                if logins.keys() >= target_ids:
                    break
                try:
                    next_btn = page.locator("a.paginate_button.next:not(.disabled)")
                    if next_btn.count() == 0:
                        break
                    next_btn.click()
                    page.wait_for_timeout(800)
                except Exception:
                    break

        except Exception as e:
            print(f"  [AccountLookup] Error: {e}")

        finally:
            try:
                _logout(page)
            except Exception:
                pass
            context.close()
            browser.close()
            print("  [AccountLookup] Browser closed.")

    missing = target_ids - logins.keys()
    if missing:
        print(f"  [AccountLookup] WARNING — login not found for account(s): {missing}")

    return logins


def upload_to_brightedge(username, account_id, groups, login_email=None):
    """
    Full Playwright upload flow for one BrightEdge account.

    Args:
        username:    BrightEdge org-level login email (used to log in)
        account_id:  BrightEdge account ID (from ticket body)
        groups:      { group_name: [{name, plp?}] } from transformer
        login_email: Account-scoped login for the TSV Login column.
                     Fetched via fetch_account_logins(). Falls back to
                     username if not provided.

    Returns:
        {
            "account_id":        str,
            "label":             str,     ← set by skill
            "success":           bool,
            "keywords_uploaded": int,
            "groups_ok":         int,
            "groups_failed":     int,
            "new_groups":        [str],
            "invalid_urls":      [str],
            "reconciliation":    dict,
            "message":           str,
            "results":           [dict],  ← per-group breakdown for build_reply
        }
    """
    tsv_login   = login_email or username
    print(f"\n[BrightEdge] Account {account_id} — login for TSV: {tsv_login}")

    tsv_content = build_tsv(tsv_login, groups)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=300)
        context = browser.new_context()
        page    = context.new_page()
        page.set_default_timeout(TIMEOUT_NAV)

        upload_result = None

        try:
            pod = _login(page, username)
            _switch_account(page, pod, account_id)
            _navigate_to_upload(page, pod)
            upload_result = _upload_and_poll(page, tsv_content)

        except PlaywrightTimeoutError as e:
            upload_result = {
                "success": False, "message": f"Timeout: {e}",
                "added_count": 0, "new_groups": [], "invalid_urls": [],
                "response_lines": [],
            }
            print(f"  TIMEOUT: {e}")

        except Exception as e:
            upload_result = {
                "success": False, "message": str(e),
                "added_count": 0, "new_groups": [], "invalid_urls": [],
                "response_lines": [],
            }
            print(f"  ERROR: {e}")

        finally:
            try:
                _logout(page)
            except Exception:
                pass
            context.close()
            browser.close()
            print("  [Browser] Closed.")

    # Reconcile groups if upload succeeded
    reconciliation = {}
    if upload_result["success"]:
        reconciliation = _verify_groups(groups, upload_result)

    # Build the summary dict
    added   = upload_result["added_count"]
    success = upload_result["success"]

    return {
        "account_id":        account_id,
        "success":           success,
        "keywords_uploaded": added if success else 0,
        "groups_ok":         len(groups) if success else 0,
        "groups_failed":     0 if success else len(groups),
        "new_groups":        upload_result.get("new_groups", []),
        "invalid_urls":      upload_result.get("invalid_urls", []),
        "warning_msgs":      upload_result.get("warning_msgs", []),
        "response_lines":    upload_result.get("response_lines", []),
        "reconciliation":    reconciliation,
        "message":           upload_result["message"],
        "results": [
            {
                "group":    name,
                "group_id": "bulk-upload",
                "count":    len(kws),
                "status":   "success" if success else "failed",
                "error":    upload_result["message"] if not success else None,
            }
            for name, kws in groups.items()
        ],
    }


# ─────────────────────────────────────────────
#  KWG SE Upload — Playwright functions
# ─────────────────────────────────────────────

def _navigate_to_kwg_se_upload(page, pod):
    """
    Navigates to the Mass Account KWG SE Upload page.
    Must be called after _switch_account() so account context is set.

    NOTE: DOM IDs below are assumed to match the keyword upload page.
    Verify via DevTools on first run if navigation or file input fails.
    """
    url = f"https://{pod}{KWG_SE_UPLOAD_PATH}"
    print(f"  [Nav] Navigating to KWG SE upload page ({KWG_SE_UPLOAD_PATH})...")
    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)
    page.locator("input[type='file']").wait_for(state="visible", timeout=TIMEOUT_NAV)
    print(f"  [Nav] KWG SE upload page ready.")


def _upload_kwg_se_and_poll(page, tsv_content):
    """
    Writes the KWG SE TSV to a temp file, uploads it, clicks Next,
    then polls for the response div using the same stuck-state loop
    as _upload_and_poll().

    TSV is written as UTF-8 without BOM — BrightEdge KWG SE upload
    requires plain UTF-8 (not UTF-8 BOM).

    NOTE: File input ID, Next button ID, and response div ID are assumed
    to match the keyword upload page. Verify via DevTools on first run.

    Returns:
        {
            "success":        bool,
            "message":        str,
            "kwgs_updated":   int,
            "error_msgs":     [str],
            "response_lines": [str],
        }
    """
    # Write as plain UTF-8 (no BOM) — important for KWG SE upload
    TMP_DIR.mkdir(exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8", dir=str(TMP_DIR)
    )
    tmp.write(tsv_content)
    tmp.close()

    print(f"  [Upload] Temp file: {tmp.name}")

    # NOTE: Verify these IDs via DevTools on first run against the KWG SE page.
    FILE_INPUT_ID     = "massAccountImportCSVFile"   # assumed same as KW upload
    UPLOAD_TIMEOUT_MS = 600_000                       # 10 minutes

    try:
        page.locator(f"#{FILE_INPUT_ID}").set_input_files(tmp.name)
        print(f"  [Upload] File selected. Clicking Next...")

        def _click_next_and_wait_for_loading():
            page.click("#massAccountFileUploadNext", timeout=TIMEOUT_NAV, no_wait_after=True)
            try:
                page.wait_for_function(
                    "() => document.body.classList.contains('loading')",
                    timeout=5_000,
                )
            except PlaywrightTimeoutError:
                pass  # loading may start and finish extremely fast

        _click_next_and_wait_for_loading()
        print(f"  [Upload] Waiting for KWG SE import page "
              f"(max {UPLOAD_TIMEOUT_MS // 1000}s)...")

        # After clicking Next, BrightEdge navigates to the _import result page.
        # Results are rendered immediately on that page — no polling needed.
        try:
            page.wait_for_url(
                "**/admin/mass_account_kwg_se_upload_import**",
                timeout=UPLOAD_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            return {
                "success": False,
                "message": f"Upload timed out after "
                           f"{UPLOAD_TIMEOUT_MS // 1000}s — "
                           f"BrightEdge did not navigate to the result page.",
                "kwgs_updated": 0,
                "error_msgs":   [],
                "response_lines": [],
            }

        print(f"  [Upload] Import result page loaded — reading response...")
        page_text = page.inner_text("body")
        return _parse_kwg_se_response(page_text)

    finally:
        try:
            time.sleep(1)
            os.unlink(tmp.name)
        except Exception:
            pass


def _parse_kwg_se_response(page_text):
    """
    Parses the _import result page returned by BrightEdge after a KWG SE upload.

    The import page shows results under a header like:
        Account Specific - login@email.com :
            Updated KWGs => 5
            Invalid Se Id Detected => "bad_id"

    Collects all lines after that header and classifies them as success or error.
    """
    lines          = page_text.splitlines()
    in_response    = False
    response_lines = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        # Sentinel: "Account Specific - ... :" header on the _import page
        if "account specific" in s.lower():
            in_response = True
            continue
        if in_response:
            # Stop at nav/footer items
            if s in ("Next", "Cancel", "Choose File", "Manage Account",
                     "Accounts", "Users", "Advanced", "Logout"):
                break
            response_lines.append(s)

    kwgs_updated = 0
    error_msgs   = []

    for line in response_lines:
        ll = line.lower()
        if "updated kwgs =>" in ll or "modified =>" in ll:
            m = re.search(r"=>\s*(\d+)", line)
            if m:
                kwgs_updated += int(m.group(1))
        elif ("invalid" in ll or "error" in ll or "failed" in ll
              or "not found" in ll or "detected" in ll):
            error_msgs.append(line)

    # Treat any non-empty response without errors as success
    is_success    = bool(response_lines) and not error_msgs
    summary_parts = []
    if kwgs_updated:
        summary_parts.append(f"{kwgs_updated} KWGs updated")
    elif response_lines and not error_msgs:
        summary_parts.append("; ".join(response_lines[:3]))
    if error_msgs:
        summary_parts.append(f"errors: {'; '.join(error_msgs[:5])}")

    return {
        "success":        is_success,
        "message":        " | ".join(summary_parts) if summary_parts else "(no message)",
        "kwgs_updated":   kwgs_updated,
        "error_msgs":     error_msgs,
        "response_lines": response_lines,
    }


def upload_kwg_se_to_brightedge(username, account_id, kwg_se_data, login_email=None):
    """
    Full Playwright upload flow for KWG SE assignments to one BrightEdge account.

    Args:
        username:    BrightEdge org-level login email (used to log in)
        account_id:  BrightEdge account ID (from ticket body)
        kwg_se_data: { kwg_name: "34;36" } from transformer.build_kwg_se_data()
        login_email: Account-scoped login for the TSV Login column.
                     Fetched via fetch_account_logins(). Falls back to username.

    Returns:
        {
            "account_id":     str,
            "label":          str,   ← set by skill
            "success":        bool,
            "kwgs_updated":   int,
            "response_lines": [str],
            "message":        str,
        }
    """
    from tools.transformer import build_kwg_se_tsv

    tsv_login   = login_email or username
    print(f"\n[BrightEdge KWG SE] Account {account_id} — login for TSV: {tsv_login}")

    tsv_content = build_kwg_se_tsv(tsv_login, kwg_se_data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=300)
        context = browser.new_context()
        page    = context.new_page()
        page.set_default_timeout(TIMEOUT_NAV)

        upload_result = None

        try:
            pod = _login(page, username)
            _switch_account(page, pod, account_id)
            _navigate_to_kwg_se_upload(page, pod)
            upload_result = _upload_kwg_se_and_poll(page, tsv_content)

        except PlaywrightTimeoutError as e:
            upload_result = {
                "success": False, "message": f"Timeout: {e}",
                "kwgs_updated": 0, "error_msgs": [], "response_lines": [],
            }
            print(f"  TIMEOUT: {e}")

        except Exception as e:
            upload_result = {
                "success": False, "message": str(e),
                "kwgs_updated": 0, "error_msgs": [], "response_lines": [],
            }
            print(f"  ERROR: {e}")

        finally:
            try:
                _logout(page)
            except Exception:
                pass
            context.close()
            browser.close()
            print("  [Browser] Closed.")

    kwgs_updated = upload_result.get("kwgs_updated", 0)
    success      = upload_result["success"]

    return {
        "account_id":     account_id,
        "success":        success,
        "kwgs_updated":   kwgs_updated if success else 0,
        "response_lines": upload_result.get("response_lines", []),
        "error_msgs":     upload_result.get("error_msgs", []),
        "message":        upload_result["message"],
    }
