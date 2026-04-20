"""
Skill: Mass Account Keyword Upload
Uploads a customer keyword CSV/Excel file to BrightEdge via Playwright.
"""

from skills.base import Skill


class KeywordUploadSkill(Skill):
    name = "keyword_upload"
    description = (
        "Uploads keyword CSV or Excel files to BrightEdge Mass Account Keyword Upload. "
        "Use when the ticket subject contains 'keyword upload' and has a CSV or Excel attachment."
    )

    def input_schema(self) -> dict:
        return {
            "ticket_id": "Zendesk ticket ID",
            "username":  "BrightEdge login email",
            "mappings":  (
                "List of {identifier, account_id} dicts. "
                "Single account: [{'identifier': null, 'account_id': '191'}]. "
                "Multi-file: [{'identifier': 'norgren.csv', 'account_id': '191'}, ...]. "
                "Multi-sheet: [{'identifier': 'Norgren', 'account_id': '191'}, ...]."
            ),
        }

    def validate(self, inputs: dict) -> dict:
        errors = []
        if not inputs.get("ticket_id"):
            errors.append("Internal error: ticket_id not injected before validation.")
        if not inputs.get("username"):
            errors.append(
                "BrightEdge username not found. "
                "Add a line 'BrightEdge Username: your@email.com' as an internal note."
            )
        if not inputs.get("mappings"):
            errors.append(
                "No account ID mapping could be built. "
                "Ensure the ticket has a supported file attachment and an 'Account ID: 12345' line."
            )
        return {"valid": len(errors) == 0, "errors": errors}

    def execute(self, inputs: dict) -> dict:
        """
        Full pipeline for one Zendesk keyword upload ticket.

        Steps:
          1. Fetch ticket — get keyword_files and body
          2. Download, parse, and match each file/sheet to an account ID
          3. For each match: map columns (Claude), remap rows, transform to groups
          4. Upload to BrightEdge via Playwright
          5. Return structured result for reporter and build_reply
        """
        from tools import zendesk as zd
        from tools import parser
        from tools import transformer
        from tools import brightedge
        from tools import brightedge_api

        ticket_id = inputs["ticket_id"]
        username  = inputs["username"]
        mappings  = inputs["mappings"]   # [{identifier, account_id}]

        print("=" * 55)
        print(f"  [Skill] keyword_upload — ticket #{ticket_id}")
        print("=" * 55)

        # ── Step 1: Fetch ticket ─────────────────────────────────
        ticket = zd.fetch_ticket(ticket_id)
        if not ticket:
            return {
                "status": "failure",
                "reason": "ticket_not_found_or_invalid_subject",
                "all_summaries": [],
                "skipped_notes": [],
            }

        skipped_notes = []
        work_items    = []   # [{account_id, rows, label}]

        excel_files = [f for f in ticket["keyword_files"]
                       if f["extension"] in (".xlsx", ".xls")]
        csv_files   = [f for f in ticket["keyword_files"]
                       if f["extension"] == ".csv"]

        # ── Step 2a: Excel files — match by sheet name ───────────
        for file_info in excel_files:
            print(f"\n[Excel] {file_info['filename']}")
            file_bytes = zd.download_file(file_info)
            parsed     = parser.parse_excel(file_bytes)   # { sheet_name: [rows] }

            if not parsed:
                skipped_notes.append(
                    f"'{file_info['filename']}' — could not parse any keyword data"
                )
                continue

            available_sheets = list(parsed.keys())

            for mapping in mappings:
                identifier = mapping["identifier"]
                account_id = mapping["account_id"]

                # Skip mappings that look like filenames — those are for CSVs
                if identifier and "." in identifier:
                    continue

                if identifier is None:
                    if len(available_sheets) == 1:
                        work_items.append({
                            "account_id": account_id,
                            "rows":       parsed[available_sheets[0]],
                            "label":      f"{file_info['filename']} / {available_sheets[0]}",
                        })
                    else:
                        skipped_notes.append(
                            f"Account {account_id} — no Sheet label provided but "
                            f"'{file_info['filename']}' has multiple sheets "
                            f"({', '.join(available_sheets)}). "
                            f"Please add a Sheet: label for each account."
                        )
                    continue

                matched = parser.fuzzy_match_sheet(identifier, available_sheets)
                if matched:
                    work_items.append({
                        "account_id": account_id,
                        "rows":       parsed[matched],
                        "label":      f"{file_info['filename']} / {matched}",
                    })
                else:
                    skipped_notes.append(
                        f"Sheet '{identifier}' (Account {account_id}) — not found "
                        f"in '{file_info['filename']}'. "
                        f"Available: {', '.join(available_sheets)}"
                    )

            # Flag sheets with no mapping
            matched_labels = {
                wi["label"].split(" / ")[-1]
                for wi in work_items
                if file_info["filename"] in wi.get("label", "")
            }
            for sheet_name in available_sheets:
                if sheet_name not in matched_labels:
                    skipped_notes.append(
                        f"Sheet '{sheet_name}' in '{file_info['filename']}' — "
                        f"no Account ID mapping in ticket description"
                    )

        # ── Step 2b: CSV files — match by filename ───────────────
        if csv_files:
            csv_parsed = {}
            for file_info in csv_files:
                print(f"\n[CSV] {file_info['filename']}")
                file_bytes = zd.download_file(file_info)
                rows       = parser.parse_csv(file_bytes)
                if not rows:
                    skipped_notes.append(
                        f"'{file_info['filename']}' — file appears to be empty"
                    )
                else:
                    csv_parsed[file_info["filename"]] = rows

            available_filenames = list(csv_parsed.keys())
            lone_mapping = next((m for m in mappings if m["identifier"] is None), None)

            if len(csv_files) == 1:
                # Single attachment — identifier should be null, but also accept when
                # the orchestrator provided the filename itself instead of null.
                if not lone_mapping and available_filenames:
                    lone_mapping = next(
                        (m for m in mappings
                         if m.get("identifier") and
                         parser.fuzzy_match_filename(m["identifier"], available_filenames)),
                        None,
                    )
                if lone_mapping and available_filenames:
                    fname = available_filenames[0]
                    work_items.append({
                        "account_id": lone_mapping["account_id"],
                        "rows":       csv_parsed[fname],
                        "label":      fname,
                    })
                    print(f"  [CSV] Single file -> account {lone_mapping['account_id']}")
                elif available_filenames:
                    skipped_notes.append(
                        f"'{available_filenames[0]}' — no Account ID found in credentials note."
                    )
            else:
                # Multiple attachments — require File: + Account ID labels
                csv_mappings = [
                    m for m in mappings
                    if m["identifier"] and (
                        "." in m["identifier"]
                        or parser.fuzzy_match_filename(m["identifier"], available_filenames)
                    )
                ]

                matched_filenames = set()
                for mapping in csv_mappings:
                    identifier = mapping["identifier"]
                    account_id = mapping["account_id"]

                    matched = parser.fuzzy_match_filename(identifier, available_filenames)
                    if matched:
                        matched_filenames.add(matched)
                        work_items.append({
                            "account_id": account_id,
                            "rows":       csv_parsed[matched],
                            "label":      matched,
                        })
                    else:
                        skipped_notes.append(
                            f"File '{identifier}' (Account {account_id}) — "
                            f"not found as an attachment. "
                            f"Attached CSVs: {', '.join(available_filenames) or 'none'}"
                        )

                for fname in available_filenames:
                    if fname not in matched_filenames:
                        skipped_notes.append(
                            f"'{fname}' — attached but no Account ID mapping found. "
                            f"Add 'File: {fname}' and 'Account ID: <id>' to the ticket."
                        )

        # ── Nothing to process ───────────────────────────────────
        if not work_items:
            return {
                "status":        "failure",
                "reason":        "no_work_items",
                "all_summaries": [],
                "skipped_notes": skipped_notes,
            }

        # ── Step 3: Lookup account-scoped login emails (one session) ──
        account_ids   = list({item["account_id"] for item in work_items})
        account_logins = brightedge.fetch_account_logins(username, account_ids)

        # ── Step 4: Map columns (Claude), transform, upload ──
        print(f"\n  Processing {len(work_items)} file(s)/sheet(s)...")
        all_summaries = []

        for item in work_items:
            print(f"\n--- '{item['label']}' → Account {item['account_id']} ---")
            rows = item["rows"]

            if not rows:
                skipped_notes.append(f"'{item['label']}' — no rows to process")
                continue

            # Map column headers to standard field names.
            # Primary: fuzzy alias matching. Fallback: Claude column reasoner.
            headers     = list(rows[0].keys())
            sample_rows = rows[:3]
            try:
                column_map = transformer.map_columns(headers, sample_rows)
            except ValueError as e:
                skipped_notes.append(
                    f"'{item['label']}' — could not identify keyword column "
                    f"(fuzzy and Claude both failed). Headers found: {headers}"
                )
                print(f"  [Skill] Column mapping failed for '{item['label']}': {e}")
                continue

            if not column_map.get("keyword"):
                skipped_notes.append(
                    f"'{item['label']}' — could not identify keyword column. "
                    f"Headers found: {headers}"
                )
                continue

            # Remap rows to standard column names, then group
            remapped_rows = transformer.remap_rows(rows, column_map)
            groups        = transformer.transform_to_groups(remapped_rows)

            if not groups:
                skipped_notes.append(
                    f"'{item['label']}' — no keyword groups found after transformation"
                )
                continue

            summary = brightedge.upload_to_brightedge(
                username    = username,
                account_id  = item["account_id"],
                groups      = groups,
                login_email = account_logins.get(item["account_id"]),
            )
            summary["label"] = item["label"]

            # ── Step 5: API QA verification ──────────────────────────────
            if summary["success"]:
                qa = brightedge_api.verify_keyword_groups(
                    item["account_id"], username, groups
                )
                summary["qa"] = qa
                print(f"  [QA] {qa['found']}/{qa['total_expected']} keywords verified "
                      f"in groups. Missing: {len(qa['missing'])}")

                if not qa["ok"] and qa["ok"] is not None:
                    # Build partial groups from (keyword, group) pairs that failed
                    missing_pairs = {
                        (m["keyword"].lower(), m["group"].lower())
                        for m in qa["missing"]
                    }
                    partial_groups = {}
                    for group_name, kws in groups.items():
                        filtered = [
                            kw for kw in kws
                            if (kw["name"].lower(), group_name.lower()) in missing_pairs
                        ]
                        if filtered:
                            partial_groups[group_name] = filtered

                    print(f"  [QA] Retrying upload for "
                          f"{len(qa['missing'])} missing keyword(s) "
                          f"across {len(partial_groups)} group(s)...")
                    retry = brightedge.upload_to_brightedge(
                        username    = username,
                        account_id  = item["account_id"],
                        groups      = partial_groups,
                        login_email = account_logins.get(item["account_id"]),
                    )
                    retry["label"]    = item["label"]
                    retry["qa_retry"] = True
                    if retry["success"]:
                        qa2 = brightedge_api.verify_keyword_groups(
                            item["account_id"], username, groups
                        )
                        retry["qa"] = qa2
                        print(f"  [QA] Post-retry: "
                              f"{qa2['found']}/{qa2['total_expected']} verified.")
                    summary = retry

            all_summaries.append(summary)

        return {
            "status":        "success" if all_summaries else "failure",
            "all_summaries": all_summaries,
            "skipped_notes": skipped_notes,
            "ticket_id":     ticket_id,
        }
