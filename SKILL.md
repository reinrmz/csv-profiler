---
name: csv-profiler
description: Profile, inspect, validate or plan the cleanup of a messy CSV, TSV, or Excel (xlsx/xls) file. Runs a deterministic Python profiler that reports data quality — null rates, duplicate rows and duplicate keys, dtype inference and type confidence, mixed date formats, numbers stored as text, whitespace padding, inconsistent casing, encoding problems, outliers, totals rows — then interprets the findings and proposes pandas cleanup steps. Use when the user asks to profile / inspect / audit / sanity-check / validate / assess / "what's wrong with" a data file, asks about nulls, duplicates, column types, or wants to know how to clean a spreadsheet or CSV before loading it.
---

# CSV / Excel profiler

Statistics come from the script. Interpretation and cleanup advice come from you.
Never recompute a number the script already reports; never invent one it does not.

## Procedure

1. Run the profiler. Never open the raw file to compute stats yourself.

   ```bash
   python scripts/profile.py <path>                    # indented JSON
   python scripts/profile.py <path> --sheet "Sheet2"   # pick an Excel sheet
   python scripts/profile.py <path> --json-only        # compact, single line
   ```

   Non-zero exit means the file is unreadable; stdout holds `{"error": ...}`. Report the
   error and stop — do not fall back to reading the file another way.

2. Read the JSON. Use `columns[]`, `issues[]`, `candidate_keys`, `outliers`, `shape`, `file`.
   Every claim you make must trace to a field in that payload.

3. Report findings grouped by severity, using the `severity` field on each issue:

   - **Blocking** — cannot load or join reliably: unreadable/unparseable file,
     `DUPLICATE_ROWS`, `CANDIDATE_KEY_DUPLICATES`, `DUPLICATE_COLUMN_NAME`.
   - **Warning** — silently wrong results if ignored: `HIGH_NULL_RATE`, `ALL_NULL_COLUMN`,
     `MIXED_DATE_FORMATS`, `NUMERIC_AS_TEXT`, `SUSPECTED_TOTALS_ROW`, `EMPTY_COLUMN_NAME`,
     `ENCODING_FALLBACK`.
   - **Informational** — cosmetic or grouping risk: `WHITESPACE_PADDING`, `INCONSISTENT_CASING`.

   For each issue give the column, the measured number, and the consequence. One line each.
   Mention low `type_confidence` and `mixed_type_sample` values when they explain an issue.

4. Propose cleanup steps in priority order — blocking first, then warnings, then
   informational — with the pandas code for each. Order matters: drop the totals row and
   fix headers before parsing types; strip whitespace and normalise casing before
   deduplicating or grouping. State what each step changes and what it discards.

5. Never modify, overwrite, or write alongside the input file. Recommend only. Only write
   a cleanup script or apply changes if the user explicitly asks for it, and then to a new
   output path.

## Reading the payload

- `inferred_type` + `type_confidence` — confidence below ~0.9 means mixed content; check
  `mixed_type_sample` for the offenders.
- `numeric_stored_as_text: true` — values needed cleaning (currency symbol, thousands
  comma, accounting parentheses) to parse as numbers.
- `detected_date_formats` — more than one entry means `pd.to_datetime` without an explicit
  format will parse rows inconsistently (day/month ambiguity).
- `candidate_keys` — unique and non-null; empty means no safe join key exists as-is.
- `outliers` — count beyond 3σ only. Values are deliberately withheld; do not guess them,
  and do not call an outlier an error without domain context.
- `raw_name` vs `name` — differ when the header itself carries whitespace.
