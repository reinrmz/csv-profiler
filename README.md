# csv-profiler

<!-- TODO: demo GIF -->

A Claude Code skill that profiles messy CSV/Excel files and produces a data-quality report plus suggested cleanup steps.

## Design principle

Deterministic statistics are Python. Interpretation and cleanup recommendations are Claude.
The script never guesses; the skill never recomputes numbers. `scripts/profile.py` measures
what is measurable — null rates, uniqueness, type confidence, date-format counts, duplicate
rows, outlier counts — and emits it as JSON with machine-readable issue codes. Claude reads
that JSON, decides what matters, and writes the cleanup plan. Neither half does the other's
job: the script has no opinions and Claude does no arithmetic on the raw file.

## Install

```bash
pip install -r requirements.txt
```

As a Claude Code skill, copy the repo into a skills directory:

```bash
# project-local (available in this repo only)
mkdir -p .claude/skills
cp -r /path/to/csv-profiler .claude/skills/csv-profiler

# user-global (available everywhere)
cp -r /path/to/csv-profiler ~/.claude/skills/csv-profiler
```

Claude discovers it by the `name` and `description` in `SKILL.md`. Verify with `/skills`.

Plugin marketplace layout — put the skill under a plugin root and list the plugin in a
marketplace manifest:

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json
└── skills/
    └── csv-profiler/
        ├── SKILL.md
        ├── scripts/profile.py
        └── examples/messy_sample.csv
```

```jsonc
// .claude-plugin/marketplace.json
{
  "name": "my-marketplace",
  "owner": { "name": "your-name" },
  "plugins": [{ "name": "my-plugin", "source": "./my-plugin" }]
}
```

Then `/plugin marketplace add <repo-or-path>` and `/plugin install my-plugin@my-marketplace`.

## Usage

Standalone CLI:

```bash
python scripts/profile.py examples/messy_sample.csv
python scripts/profile.py book.xlsx --sheet "Q1 Sales"
python scripts/profile.py data.tsv --json-only > profile.json
```

JSON is the only thing written to stdout. An unreadable file exits non-zero with
`{"error": "..."}`.

Through Claude Code — just ask:

> profile examples/messy_sample.csv
> what's wrong with this spreadsheet before I load it?
> check sales_export.xlsx for duplicates and null columns

Claude runs the profiler, groups findings by severity, and proposes pandas cleanup steps in
priority order. It never modifies the input file.

## Sample output

Truncated from `python scripts/profile.py examples/messy_sample.csv` (two of eight columns
shown; `issues`, `candidate_keys` and `outliers` are complete):

```json
{
  "file": {
    "name": "messy_sample.csv",
    "size_bytes": 1853,
    "detected_encoding": "utf-8",
    "delimiter": ",",
    "sheet_names": null
  },
  "shape": { "rows": 31, "columns": 8 },
  "columns": [
    {
      "name": "order_date",
      "raw_name": "order_date",
      "non_null_count": 30,
      "null_count": 1,
      "null_rate": 0.0323,
      "unique_count": 28,
      "inferred_type": "date",
      "type_confidence": 1.0,
      "mixed_type_sample": [],
      "detected_date_formats": ["%Y-%m-%d", "%d/%m/%Y", "%b %d %Y"],
      "has_leading_trailing_whitespace": false,
      "numeric_stored_as_text": false,
      "min": null, "max": null, "mean": null, "std": null,
      "top_values": [
        { "value": "2024-01-22", "count": 2 },
        { "value": "24/01/2024", "count": 2 },
        { "value": "2024-01-15", "count": 1 }
      ]
    },
    {
      "name": "amount",
      "raw_name": "amount",
      "non_null_count": 31,
      "null_count": 0,
      "null_rate": 0.0,
      "unique_count": 29,
      "inferred_type": "float",
      "type_confidence": 1.0,
      "mixed_type_sample": [],
      "detected_date_formats": [],
      "has_leading_trailing_whitespace": false,
      "numeric_stored_as_text": true,
      "min": 620.0,
      "max": 151207.24,
      "mean": 9869.702581,
      "std": 31485.205805,
      "top_values": [
        { "value": "$2,265.80", "count": 2 },
        { "value": "3,340.00", "count": 2 },
        { "value": "1,240.50", "count": 1 }
      ]
    }
  ],
  "issues": [
    { "code": "DUPLICATE_ROWS", "severity": "blocking", "column": null,
      "detail": "2 fully duplicated row(s)" },
    { "code": "EMPTY_COLUMN_NAME", "severity": "warning", "column": "Unnamed: 7",
      "detail": "column header is blank or placeholder (raw='Unnamed: 7')" },
    { "code": "MIXED_DATE_FORMATS", "severity": "warning", "column": "order_date",
      "detail": "date formats present: %Y-%m-%d, %d/%m/%Y, %b %d %Y" },
    { "code": "WHITESPACE_PADDING", "severity": "informational", "column": "customer_name",
      "detail": "values have leading or trailing whitespace" },
    { "code": "INCONSISTENT_CASING", "severity": "informational", "column": "city",
      "detail": "same value in multiple casings: 'CEBU', 'Cebu', 'Davao', 'MANILA', 'Manila'" },
    { "code": "NUMERIC_AS_TEXT", "severity": "warning", "column": "amount",
      "detail": "numeric values carry currency symbols, separators or parentheses" },
    { "code": "HIGH_NULL_RATE", "severity": "warning", "column": "discount_code",
      "detail": "58% of values are null" },
    { "code": "ALL_NULL_COLUMN", "severity": "warning", "column": "legacy_flag",
      "detail": "column is entirely empty" },
    { "code": "CANDIDATE_KEY_DUPLICATES", "severity": "blocking", "column": "order_id",
      "detail": "looks like an identifier but has 2 duplicated value(s)" },
    { "code": "SUSPECTED_TOTALS_ROW", "severity": "warning", "column": null,
      "detail": "row 30 (0-indexed) looks like an appended totals line" }
  ],
  "candidate_keys": [],
  "outliers": { "amount": 1 }
}
```

## Example file: which row/column shows which issue

`examples/messy_sample.csv` is synthetic — invented company names, cities and amounts, no
real data. Row numbers are 1-based counting the header as row 1.

| Issue | Where |
|---|---|
| `DUPLICATE_ROWS` | rows 29 and 30 repeat rows 11 (`SO-1010`) and 15 (`SO-1014`) verbatim |
| `CANDIDATE_KEY_DUPLICATES` | column `order_id` — `SO-1010` and `SO-1014` each appear twice |
| `MIXED_DATE_FORMATS` | column `order_date` — `2024-01-15` (row 2), `15/01/2024` (row 3), `Jan 16 2024` (row 4) |
| `NUMERIC_AS_TEXT` | column `amount` — thousands commas (`1,240.50`, row 2) and currency symbols (`$980.00`, row 3) |
| `WHITESPACE_PADDING` | column `customer_name` — `"  Bravo Supplies  "` (row 3), `"  Foxtrot Ltd"` (row 7), `"Hotel Wholesale "` (row 9) |
| `INCONSISTENT_CASING` | column `city` — `Manila` (row 2), `manila` (row 3), `MANILA` (row 4); same for Cebu and Davao |
| `HIGH_NULL_RATE` | column `discount_code` — 18 of 31 rows empty (58%) |
| `ALL_NULL_COLUMN` | column `legacy_flag` — empty in every row |
| `EMPTY_COLUMN_NAME` | column 8 — header is blank, pandas labels it `Unnamed: 7` |
| `SUSPECTED_TOTALS_ROW` | row 32 — `order_id` is `TOTAL`, all text columns blank, `amount` filled |
| outlier (3σ) | row 31 — `98,750.00`, plus the totals row inflating mean and std |

`ENCODING_FALLBACK` and `DUPLICATE_COLUMN_NAME` are not in this file; they need a non-UTF-8
file and a repeated header respectively.

## Tests

```bash
python -m pytest tests -q
```

## What this is not

- **Not a cleaning tool.** It reports and recommends. Transformation is your call.
- **It does not modify files.** Input is opened read-only; nothing is written except JSON to stdout.
- **Not a validation framework.** No expectation suites, no schema contracts, no CI gate, no
  pass/fail. If you need declarative rules enforced on every run, use Great Expectations,
  Pandera or dbt tests. This is the step before that — figuring out what the rules should be.
