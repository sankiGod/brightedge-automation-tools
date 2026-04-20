# ============================================================
#  tools/transformer.py — Data transformation
#
#  Functions:
#    map_columns(headers, sample_rows) → column mapping dict  [Claude call]
#    remap_rows(rows, column_map)      → rows with standard column names
#    transform_to_groups(rows)         → { group_name: [{name, plp}] }
#    build_tsv(username, groups)       → TSV string for BrightEdge upload
#    build_reply(summaries, skipped)   → Zendesk reply message string
# ============================================================

import json
import os
import re
from collections import defaultdict

# Standard column names that transform_to_groups() expects
KEYWORD_COL  = "keyword"
PLP_COL      = "preferred landing page"
GROUP_PREFIX = "keyword group"

FUZZY_SHEET_THRESHOLD = float(os.environ.get("FUZZY_SHEET_THRESHOLD", "0.6"))


# ─────────────────────────────────────────────
#  Column mapping — fuzzy first, Claude fallback
# ─────────────────────────────────────────────

def map_columns(headers: list, sample_rows: list) -> dict:
    """
    Maps raw customer column headers to standard BrightEdge fields.
    Called once per file/sheet before transform_to_groups().

    Primary path: rule-based fuzzy matching via parser.fuzzy_match_columns().
    Fallback path: Claude (claude-sonnet-4-20250514) via column_reasoner,
                   triggered when fuzzy confidence < FUZZY_SHEET_THRESHOLD.

    Args:
        headers:     List of column name strings from the parsed file
        sample_rows: First N rows of data (used by Claude fallback for context)

    Returns:
        {
            "keyword":    "<actual column name or None>",
            "plp":        "<actual column name or None>",
            "groups":     ["<col1>", "<col2>", ...],
            "source":     "fuzzy" | "claude",
            "confidence": float,
        }

    Raises:
        ValueError if neither path can identify the keyword column.
    """
    from tools.parser import fuzzy_match_columns
    from tools.column_reasoner import reason_columns

    # ── Primary: fuzzy alias matching ────────────────────────────
    mapping = fuzzy_match_columns(headers)
    print(f"  [Transformer] Fuzzy column mapping: {mapping}")

    if mapping["confidence"] >= FUZZY_SHEET_THRESHOLD:
        return mapping

    # ── Fallback: Claude column reasoning ────────────────────────
    print(f"  [Transformer] Fuzzy confidence {mapping['confidence']} below threshold "
          f"({FUZZY_SHEET_THRESHOLD}) — calling Claude column reasoner.")

    claude_mapping = reason_columns(headers, sample_rows)

    if claude_mapping is None or not claude_mapping.get("keyword"):
        raise ValueError(
            f"Column mapping failed: could not identify the keyword column. "
            f"Headers seen: {headers}"
        )

    return claude_mapping


def remap_rows(rows: list, column_map: dict) -> list:
    """
    Remaps row dicts from customer column names to standard names
    so that transform_to_groups() can work without any alias matching.

    Args:
        rows:       List of row dicts from parse_csv() or parse_excel()
        column_map: Output of map_columns() — {keyword, plp, groups}

    Returns:
        List of row dicts with standardized column names:
          "keyword", "preferred landing page", "keyword group 1", "keyword group 2", ...
    """
    kw_col    = column_map.get("keyword")
    plp_col   = column_map.get("plp")
    grp_cols  = column_map.get("groups", [])

    remapped = []
    for row in rows:
        new_row = {}
        if kw_col:
            new_row[KEYWORD_COL] = row.get(kw_col, "")
        if plp_col:
            new_row[PLP_COL] = row.get(plp_col, "")
        for i, gc in enumerate(grp_cols, 1):
            if gc:
                new_row[f"{GROUP_PREFIX} {i}"] = row.get(gc, "")
        remapped.append(new_row)

    return remapped


# ─────────────────────────────────────────────
#  Row → keyword groups
# ─────────────────────────────────────────────

def transform_to_groups(rows):
    """
    Groups parsed rows by keyword group name, ready for BrightEdge upload.

    Expects rows with standardized column names (output of remap_rows()):
      - "keyword"                  — the search term
      - "preferred landing page"   — optional URL
      - "keyword group 1", "keyword group 2", ... — group memberships

    Each row can belong to multiple groups.
    Duplicates within the same group are removed.
    Rows with no keyword value are skipped.

    Args:
        rows: list of dicts with standardized column names

    Returns:
        { group_name: [ {"name": keyword, "plp": url}, ... ] }
    """
    groups         = defaultdict(list)
    seen_per_group = defaultdict(set)
    skipped        = 0

    for row in rows:
        keyword = row.get(KEYWORD_COL, "").strip()
        if not keyword:
            skipped += 1
            continue

        plp = row.get(PLP_COL, "").strip()

        # Collect all keyword group values
        group_names = [
            v.strip()
            for k, v in row.items()
            if k.startswith(GROUP_PREFIX) and v and v.strip()
        ]
        if not group_names:
            group_names = ["Ungrouped"]

        for group_name in group_names:
            key = (keyword.lower(), plp.lower())
            if key not in seen_per_group[group_name]:
                seen_per_group[group_name].add(key)
                entry = {"name": keyword}
                if plp:
                    entry["plp"] = plp
                groups[group_name].append(entry)

    if skipped:
        print(f"  {skipped} row(s) skipped — no keyword value.")

    print(f"  {len(groups)} group(s), "
          f"{sum(len(v) for v in groups.values())} total keywords.")

    return dict(groups)


# ─────────────────────────────────────────────
#  Keyword groups → TSV
# ─────────────────────────────────────────────

def build_tsv(username, groups):
    """
    Converts keyword groups into the BrightEdge Mass Account Keyword
    Upload TSV format.

    Columns: Login | KW | PLP | off | KWG1 | KWG2 | ...

    - Login : BrightEdge account email (repeated every row)
    - KW    : keyword text
    - PLP   : preferred landing page URL (empty string if none)
    - off   : always 0 (active tracking)
    - KWG1+ : one column per keyword group. Number of KWG columns is
              determined dynamically by the max groups any keyword has.
              Rows with fewer groups leave trailing columns empty.

    Keywords belonging to multiple groups appear on ONE row with each
    group in its own KWG column — NOT comma-separated in one column.

    Args:
        username: BrightEdge login email (used in the Login column)
        groups:   { group_name: [{name, plp?}] } from transform_to_groups()

    Returns:
        TSV content as a UTF-8 string including the header row.
    """
    # Build keyword map: (kw_lower, plp_lower) → { kw, plp, groups[] }
    # This merges a keyword that appears in multiple groups into one row
    keyword_map = {}
    for group_name, keywords in groups.items():
        for kw in keywords:
            key = (kw["name"].lower(), kw.get("plp", "").lower())
            if key not in keyword_map:
                keyword_map[key] = {
                    "kw":     kw["name"],
                    "plp":    kw.get("plp", ""),
                    "groups": [],
                }
            if group_name not in keyword_map[key]["groups"]:
                keyword_map[key]["groups"].append(group_name)

    # Number of KWG columns = max groups any single keyword belongs to
    max_groups  = max((len(e["groups"]) for e in keyword_map.values()), default=1)
    kwg_headers = [f"KWG{i + 1}" for i in range(max_groups)]
    header      = "\t".join(["Login", "KW", "PLP", "off"] + kwg_headers)

    lines = [header]
    for entry in keyword_map.values():
        # Pad shorter group lists with empty strings so column count is uniform
        padded = entry["groups"] + [""] * (max_groups - len(entry["groups"]))
        lines.append("\t".join([
            username,
            entry["kw"],
            entry["plp"],
            "0",
        ] + padded))

    kw_count   = len(keyword_map)
    pair_count = sum(len(v) for v in groups.values())
    print(f"  TSV: {kw_count} keyword rows, "
          f"{pair_count} keyword-group pairs, "
          f"max {max_groups} group(s) per keyword")

    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Upload summaries → Zendesk reply message
# ─────────────────────────────────────────────

def build_reply(all_summaries, skipped_notes):
    """
    Builds a single Zendesk reply message covering all accounts processed
    and any sheets or files that were skipped.

    Args:
        all_summaries: list of upload result dicts from brightedge.upload_to_brightedge()
        skipped_notes: list of human-readable strings explaining what was skipped

    Returns:
        Formatted message string ready to post on the ticket.
    """
    total_kws  = sum(s["keywords_uploaded"] for s in all_summaries)
    any_failed = any(not s["success"] for s in all_summaries)

    lines = []

    if not any_failed:
        lines += [
            "Hi, your keyword upload has been completed successfully!\n",
            f"  \u2022 Accounts processed  : {len(all_summaries)}",
            f"  \u2022 Total keywords added : {total_kws}\n",
        ]
    else:
        lines += [
            "Hi, your keyword upload completed with some issues.\n",
            f"  \u2022 Accounts processed  : {len(all_summaries)}",
            f"  \u2022 Total keywords added : {total_kws}\n",
        ]

    # Per-account breakdown
    for summary in all_summaries:
        lines.append(f"Account {summary['account_id']} ({summary['label']}):")
        lines.append(f"  Keywords added  : {summary['keywords_uploaded']}")

        qa = summary.get("qa")
        if qa is not None:
            if qa.get("ok") is True:
                lines.append(f"  QA verified     : "
                             f"{qa['found']}/{qa['total_expected']} keywords confirmed "
                             f"in groups")
            elif qa.get("ok") is False:
                lines.append(f"  QA warning      : "
                             f"{qa['found']}/{qa['total_expected']} keywords verified "
                             f"in groups")
                # Group missing entries by group name for readability
                missing_by_group: dict = {}
                for m in qa["missing"]:
                    missing_by_group.setdefault(m["group"], []).append(m["keyword"])
                shown = 0
                for group_name, kws in missing_by_group.items():
                    if shown >= 3:
                        lines.append(f"  ... and more")
                        break
                    preview = kws[:5]
                    suffix  = f" (+{len(kws)-5} more)" if len(kws) > 5 else ""
                    lines.append(f"  Missing in '{group_name}': "
                                 f"{', '.join(preview)}{suffix}")
                    shown += 1
            elif qa.get("ok") is None:
                lines.append(f"  QA              : could not verify — "
                             f"{qa.get('error', 'API error')}")
            if summary.get("qa_retry"):
                lines.append(f"  (Missing keywords were re-uploaded automatically)")

        new_groups = summary.get("new_groups", [])
        if new_groups:
            lines.append(f"  New groups      : {len(new_groups)}")

        warning_msgs = summary.get("warning_msgs", [])
        if warning_msgs:
            for w in warning_msgs:
                lines.append(f"  \u26a0 {w}")

        if summary.get("invalid_urls"):
            lines.append(f"  Invalid URLs    : {len(summary['invalid_urls'])} "
                         f"(keywords added but URLs not accepted)")
        if not summary["success"]:
            msg = summary.get("message", "Unknown error")
            if "timed out" in msg:
                keywords_added = summary.get("keywords_uploaded", 0)
                account_id     = summary["account_id"]
                if keywords_added > 0:
                    lines.append(
                        f"  \u26a0 Upload timed out — BrightEdge did not return a confirmation "
                        f"within 10 minutes. {keywords_added} keyword(s) may have been added.\n"
                        f"  Please log into BrightEdge and check account {account_id} manually.\n"
                        f"  \u2022 If some keywords are missing, attach a new file with only the "
                        f"remaining keywords and resubmit this ticket.\n"
                        f"  \u2022 If none were added, resubmit with the full original file."
                    )
                else:
                    lines.append(
                        f"  \u26a0 Upload timed out — BrightEdge did not return a confirmation "
                        f"within 10 minutes. It is unclear if any keywords were added.\n"
                        f"  Please log into BrightEdge and check account {account_id} manually.\n"
                        f"  \u2022 If no keywords were added, resubmit this ticket with the full original file.\n"
                        f"  \u2022 If some were added, attach a new file with only the remaining "
                        f"keywords and resubmit."
                    )
            else:
                lines.append(f"  Error           : {msg}")
        lines.append("")

    # Skipped items
    if skipped_notes:
        lines.append("Items skipped (not processed):")
        for note in skipped_notes:
            lines.append(f"  \u26a0 {note}")
        lines.append(
            "\nFor any skipped item, please check that the Sheet or File name "
            "in the ticket description matches what is in the attached file."
        )

    if not any_failed:
        lines.append(
            "\nAll keywords are now live in BrightEdge. "
            "Please allow up to 24 hours for ranking data to appear in reports."
        )
    else:
        lines.append(
            "\nPlease review any failed accounts and contact the team if you need help."
        )

    return "\n".join(lines)


# ─────────────────────────────────────────────
#  KWG SE Upload — data building and TSV generation
# ─────────────────────────────────────────────

def build_kwg_se_data(rows: list, column_map: dict) -> dict:
    """
    Groups SE IDs by KWG name from parsed file rows.

    Handles two customer file formats:
      1. SE IDs already semicolon-separated in one cell: "34;36"
      2. One SE ID per row for the same KWG (aggregated automatically)

    Args:
        rows:       List of row dicts from parse_csv() or parse_excel_kwg_se()
        column_map: Output of parser.map_kwg_se_columns() — {kwg_name, se_ids}

    Returns:
        { kwg_name: "34;36" } — SE IDs sorted numerically, ready for TSV.
    """
    kwg_name_col = column_map["kwg_name"]
    se_ids_col   = column_map["se_ids"]

    kwg_se: dict = {}   # kwg_name → set of SE ID strings
    skipped = 0

    for row in rows:
        kwg = row.get(kwg_name_col, "").strip() if kwg_name_col else ""
        se  = row.get(se_ids_col,   "").strip() if se_ids_col   else ""

        if not kwg:
            skipped += 1
            continue

        if kwg not in kwg_se:
            kwg_se[kwg] = set()

        # Split on semicolons or commas to handle both "34;36" and "34, 36"
        for part in re.split(r"[;,]", se):
            part = part.strip()
            if part:
                kwg_se[kwg].add(part)

    if skipped:
        print(f"  {skipped} row(s) skipped — no KWG name value.")

    # Sort SE IDs: numerically if all digits, lexically otherwise
    def _sort_key(x):
        return (0, int(x)) if x.isdigit() else (1, x)

    result = {
        kwg: ";".join(sorted(ids, key=_sort_key))
        for kwg, ids in kwg_se.items()
    }

    print(f"  KWG SE: {len(result)} KWG(s) with SE assignments.")
    return result


def build_kwg_se_tsv(login: str, kwg_se_data: dict) -> str:
    """
    Builds the BrightEdge KWG SE upload TSV.

    BrightEdge expected format (columns in this exact order):
        Login | KWG name | Search engine ID list

    - Header row is included (BrightEdge ignores it on upload).
    - Encoded as UTF-8 WITHOUT BOM (not utf-8-sig).

    Args:
        login:       BrightEdge account login email (Login column)
        kwg_se_data: { kwg_name: "34;36" } from build_kwg_se_data()

    Returns:
        TSV content as a plain UTF-8 string.
    """
    lines = ["Login\tKWG\tSEs"]
    for kwg_name, se_ids in kwg_se_data.items():
        lines.append(f"{login}\t{kwg_name}\t{se_ids}")

    print(f"  KWG SE TSV: {len(kwg_se_data)} data row(s).")
    return "\n".join(lines)


def build_kwg_se_reply(all_summaries: list, skipped_notes: list) -> str:
    """
    Builds the Zendesk reply message for a KWG SE upload ticket.

    Args:
        all_summaries: list of upload result dicts from brightedge.upload_kwg_se_to_brightedge()
        skipped_notes: list of human-readable strings explaining what was skipped

    Returns:
        Formatted message string ready to post on the ticket.
    """
    total_kwgs = sum(s.get("kwgs_updated", 0) for s in all_summaries)
    any_failed = any(not s["success"] for s in all_summaries)

    # Detect summaries that failed specifically due to invalid SE IDs
    def _invalid_se_errors(summary):
        return [e for e in summary.get("error_msgs", [])
                if "invalid se id" in e.lower()]

    all_invalid_se = any_failed and all(
        _invalid_se_errors(s) for s in all_summaries if not s["success"]
    )

    lines = []

    if not any_failed:
        lines += [
            "Hi, your keyword group search engine upload has been completed successfully!\n",
            f"  \u2022 Accounts processed  : {len(all_summaries)}",
            f"  \u2022 Total KWGs updated  : {total_kwgs}\n",
        ]
    elif all_invalid_se:
        lines += [
            "Hi, your keyword group search engine upload could not be completed "
            "because one or more invalid Search Engine IDs were detected in your file.\n",
        ]
    else:
        lines += [
            "Hi, your keyword group search engine upload completed with some issues.\n",
            f"  \u2022 Accounts processed  : {len(all_summaries)}",
            f"  \u2022 Total KWGs updated  : {total_kwgs}\n",
        ]

    for summary in all_summaries:
        lines.append(f"Account {summary['account_id']} ({summary['label']}):")

        invalid_se = _invalid_se_errors(summary)
        if invalid_se:
            for err in invalid_se:
                # Extract the bad ID from "Invalid Se Id Detected => \"3\""
                import re as _re
                m = _re.search(r'["\u201c\u201d]?([^">\s]+)["\u201c\u201d]?\s*$', err)
                bad_id = m.group(1).strip('"') if m else err
                lines.append(f"  \u26a0 Invalid Search Engine ID: {bad_id}")
        elif not summary["success"]:
            msg = summary.get("message", "Unknown error")
            if "timed out" in msg:
                lines.append(
                    f"  \u26a0 Upload timed out — BrightEdge did not return a confirmation "
                    f"within 10 minutes.\n"
                    f"  Please log into BrightEdge and check account "
                    f"{summary['account_id']} manually."
                )
            else:
                lines.append(f"  Error           : {msg}")
        else:
            lines.append(f"  KWGs updated    : {summary.get('kwgs_updated', 0)}")
            response_lines = summary.get("response_lines", [])
            if response_lines:
                lines.append(f"  BrightEdge says : {' | '.join(response_lines)}")
        lines.append("")

    if skipped_notes:
        lines.append("Items skipped (not processed):")
        for note in skipped_notes:
            lines.append(f"  \u26a0 {note}")
        lines.append(
            "\nFor any skipped item, please check that the Sheet or File name "
            "in the ticket description matches what is in the attached file."
        )

    if not any_failed:
        lines.append("\nAll search engine assignments are now live in BrightEdge.")
    elif all_invalid_se:
        lines.append(
            "\nTo fix: correct the Search Engine IDs in your upload file, "
            "re-attach it to this ticket, and re-add the ai_agent_automation tag to retry."
        )
    else:
        lines.append(
            "\nPlease review any failed accounts and contact the team if you need help."
        )

    return "\n".join(lines)
