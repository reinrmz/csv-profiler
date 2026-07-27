#!/usr/bin/env python
"""Deterministic data-quality profiler for messy CSV/Excel files.

Reads a tabular file entirely as strings, computes descriptive statistics and
machine-readable issue codes, and writes a single JSON object to stdout. The
script makes no judgement calls about what should be done with the data: it
reports only what it can measure. Interpretation is left to the caller.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

__all__ = [
    "profile_file",
    "read_table",
    "detect_encoding",
    "sniff_delimiter",
    "infer_column_type",
    "detect_date_formats",
    "main",
]

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm"}

HIGH_NULL_RATE = 0.5
TYPE_CONFIDENCE_THRESHOLD = 0.8
OUTLIER_SIGMA = 3.0
MAX_SAMPLE_VALUES = 5
MAX_TOP_VALUES = 5

NULL_TOKENS = {"", "na", "n/a", "nan", "none", "null", "-", "--", "nil", "?"}

BOOL_TRUE = {"true", "t", "yes", "y", "1"}
BOOL_FALSE = {"false", "f", "no", "n", "0"}
BOOL_TOKENS = BOOL_TRUE | BOOL_FALSE

CURRENCY_CHARS = "$€£¥₱"
_INT_RE = re.compile(r"^[+-]?\d+$")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")

# (strftime pattern, matching regex). Order matters: first match wins per value.
DATE_FORMATS: list[tuple[str, re.Pattern[str]]] = [
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")),
    ("%Y/%m/%d", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")),
    ("%d/%m/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("%d-%m-%Y", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("%d.%m.%Y", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")),
    ("%b %d %Y", re.compile(r"^[A-Za-z]{3,9}\.? \d{1,2},? \d{4}$")),
    ("%d %b %Y", re.compile(r"^\d{1,2} [A-Za-z]{3,9}\.?,? \d{4}$")),
    ("%Y-%m-%dT%H:%M:%S", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")),
    ("%Y-%m-%d %H:%M", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")),
]

CATEGORICAL_MAX_UNIQUE = 20
CATEGORICAL_MAX_RATIO = 0.5


class ProfileError(Exception):
    """Raised when the input file cannot be read or parsed at all."""


# --------------------------------------------------------------------------- #
# File-level helpers
# --------------------------------------------------------------------------- #


def detect_encoding(path: Path) -> tuple[str, bool]:
    """Return (encoding, used_fallback) for a text file.

    Tries a UTF-8 BOM check, then strict UTF-8, then falls back to latin-1,
    which never raises. The second element is True when the fallback was used.
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "latin-1", True
    return "utf-8", False


def sniff_delimiter(path: Path, encoding: str) -> str:
    """Guess the field delimiter of a delimited text file.

    Falls back to the suffix convention (tab for .tsv, else comma) when
    :class:`csv.Sniffer` cannot decide.
    """
    default = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            sample = handle.read(64 * 1024)
    except OSError:
        return default
    if not sample.strip():
        return default
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Sniffer is unreliable on single-column files; count candidates.
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: first_line.count(d) for d in ",;\t|"}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] > 0 else default


def read_table(
    path: Path, sheet: str | None = None
) -> tuple[pd.DataFrame, dict[str, Any], bool]:
    """Read a CSV/TSV/Excel file as all-string data.

    Returns the frame, a file-metadata dict, and whether an encoding fallback
    was needed. Every cell is read as ``str`` so no value is silently coerced.
    """
    suffix = path.suffix.lower()
    meta: dict[str, Any] = {
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "detected_encoding": None,
        "delimiter": None,
        "sheet_names": None,
    }
    # Filled in for delimited files: the header exactly as written on disk.
    # pandas de-duplicates repeated labels ("city" -> "city.1"), so the raw
    # header is the only place a duplicate name is still visible.
    meta["_raw_header"] = []

    if suffix in EXCEL_SUFFIXES:
        try:
            book = pd.ExcelFile(path)
        except Exception as exc:  # noqa: BLE001 - surfaced as a JSON error
            raise ProfileError(f"cannot open Excel workbook: {exc}") from exc
        meta["sheet_names"] = list(book.sheet_names)
        target = sheet if sheet is not None else book.sheet_names[0]
        if target not in book.sheet_names:
            raise ProfileError(
                f"sheet {target!r} not found; available: {book.sheet_names}"
            )
        frame = book.parse(target, dtype=str, header=0, keep_default_na=False)
        meta["sheet_names"] = list(book.sheet_names)
        return _normalise(frame), meta, False

    if suffix not in CSV_SUFFIXES:
        raise ProfileError(f"unsupported file type: {suffix or '(none)'}")

    encoding, fallback = detect_encoding(path)
    delimiter = sniff_delimiter(path, encoding)
    meta["detected_encoding"] = encoding
    meta["delimiter"] = delimiter
    try:
        frame = pd.read_csv(
            path,
            dtype=str,
            sep=delimiter,
            encoding=encoding,
            keep_default_na=False,
            skip_blank_lines=True,
            engine="python",
        )
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    except UnicodeDecodeError:
        fallback = True
        meta["detected_encoding"] = "latin-1"
        frame = pd.read_csv(
            path,
            dtype=str,
            sep=delimiter,
            encoding="latin-1",
            keep_default_na=False,
            skip_blank_lines=True,
            engine="python",
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a JSON error
        raise ProfileError(f"cannot parse file: {exc}") from exc

    meta["_raw_header"] = _read_raw_header(
        path, meta["detected_encoding"] or encoding, delimiter
    )
    return _normalise(frame), meta, fallback


def _read_raw_header(path: Path, encoding: str, delimiter: str) -> list[str]:
    """Return the first row's field names exactly as stored in the file."""
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            for row in csv.reader(handle, delimiter=delimiter):
                return [field.strip() for field in row]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return []


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Force every cell to ``str`` or ``None``, leaving column labels intact."""
    if frame.empty and not len(frame.columns):
        return frame
    out = frame.copy()
    for col in out.columns:
        out[col] = out[col].map(_as_text)
    return out


def _as_text(value: Any) -> str | None:
    """Coerce one cell to a stripped string, or ``None`` if it reads as null."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value)
    if text.strip().lower() in NULL_TOKENS:
        return None
    return text


# --------------------------------------------------------------------------- #
# Value-level classification
# --------------------------------------------------------------------------- #


def _clean_numeric(text: str) -> str:
    """Strip currency symbols, thousands separators and accounting parentheses."""
    stripped = text.strip()
    negative = stripped.startswith("(") and stripped.endswith(")")
    if negative:
        stripped = stripped[1:-1]
    for char in CURRENCY_CHARS:
        stripped = stripped.replace(char, "")
    stripped = stripped.replace(" ", "").replace(" ", "")
    if _THOUSANDS_RE.match(stripped):
        stripped = stripped.replace(",", "")
    stripped = stripped.rstrip("%")
    return f"-{stripped}" if negative and stripped else stripped


def _is_int(text: str) -> bool:
    return bool(_INT_RE.match(_clean_numeric(text)))


def _is_float(text: str) -> bool:
    return bool(_NUMERIC_RE.match(_clean_numeric(text)))


def _is_bool(text: str) -> bool:
    return text.strip().lower() in BOOL_TOKENS


def _match_date_format(text: str) -> str | None:
    """Return the strftime pattern matching ``text``, or ``None``."""
    candidate = text.strip()
    for pattern, regex in DATE_FORMATS:
        if regex.match(candidate):
            return pattern
    return None


def detect_date_formats(values: Iterable[str]) -> list[str]:
    """List the distinct date patterns present in ``values``, most common first."""
    counts: Counter[str] = Counter()
    for value in values:
        pattern = _match_date_format(value)
        if pattern:
            counts[pattern] += 1
    return [pattern for pattern, _ in counts.most_common()]


def infer_column_type(values: list[str]) -> tuple[str, float]:
    """Infer a column's semantic type and the share of values that match it.

    Returns one of ``int``, ``float``, ``date``, ``bool``, ``categorical``,
    ``text`` or ``empty``, plus a confidence in ``[0, 1]``.
    """
    if not values:
        return "empty", 0.0

    total = len(values)
    n_int = sum(1 for v in values if _is_int(v))
    n_float = sum(1 for v in values if _is_float(v))
    n_bool = sum(1 for v in values if _is_bool(v))
    n_date = sum(1 for v in values if _match_date_format(v))

    scores = {
        "bool": n_bool / total,
        "int": n_int / total,
        "float": n_float / total,
        "date": n_date / total,
    }
    # Prefer the most specific type that clears the threshold.
    for name in ("bool", "date", "int", "float"):
        if scores[name] >= TYPE_CONFIDENCE_THRESHOLD:
            return name, round(scores[name], 4)

    unique = len(set(values))
    if unique <= CATEGORICAL_MAX_UNIQUE and unique / total <= CATEGORICAL_MAX_RATIO:
        return "categorical", 1.0
    return "text", 1.0


def _matches_type(text: str, inferred: str) -> bool:
    """True when a single value conforms to the column's inferred type."""
    if inferred == "int":
        return _is_int(text)
    if inferred == "float":
        return _is_float(text)
    if inferred == "bool":
        return _is_bool(text)
    if inferred == "date":
        return _match_date_format(text) is not None
    return True


# --------------------------------------------------------------------------- #
# Column profiling
# --------------------------------------------------------------------------- #


def _numeric_series(values: list[str]) -> pd.Series:
    """Convert cleaned numeric strings to floats, dropping anything unparseable."""
    parsed = pd.to_numeric(
        pd.Series([_clean_numeric(v) for v in values], dtype="object"),
        errors="coerce",
    )
    return parsed.dropna()


def _profile_column(
    raw_name: Any, name: str, series: pd.Series, row_count: int
) -> dict[str, Any]:
    """Build the per-column statistics block for one column."""
    present = [v for v in series.tolist() if v is not None]
    non_null = len(present)
    null_count = row_count - non_null
    inferred, confidence = infer_column_type(present)

    mismatches = [v for v in present if not _matches_type(v, inferred)]
    top = Counter(present).most_common(MAX_TOP_VALUES)

    column: dict[str, Any] = {
        "name": name,
        "raw_name": str(raw_name),
        "non_null_count": non_null,
        "null_count": null_count,
        "null_rate": round(null_count / row_count, 4) if row_count else 0.0,
        "unique_count": len(set(present)),
        "inferred_type": inferred,
        "type_confidence": confidence,
        "mixed_type_sample": mismatches[:MAX_SAMPLE_VALUES],
        "detected_date_formats": [],
        "has_leading_trailing_whitespace": any(v != v.strip() for v in present),
        "numeric_stored_as_text": False,
        "min": None,
        "max": None,
        "mean": None,
        "std": None,
        "top_values": [{"value": value, "count": count} for value, count in top],
    }

    if inferred == "date":
        column["detected_date_formats"] = detect_date_formats(present)

    if inferred in {"int", "float"}:
        numeric = _numeric_series(present)
        # Text storage is the norm here (everything was read as str); flag it
        # only when the raw text needed cleaning to become a number.
        column["numeric_stored_as_text"] = any(
            _clean_numeric(v) != v.strip() for v in present
        )
        if not numeric.empty:
            column["min"] = float(numeric.min())
            column["max"] = float(numeric.max())
            column["mean"] = round(float(numeric.mean()), 6)
            column["std"] = (
                round(float(numeric.std(ddof=1)), 6) if len(numeric) > 1 else 0.0
            )

    return column


def _casing_variants(values: list[str]) -> list[str]:
    """Return values whose casefolded form appears under more than one spelling."""
    groups: dict[str, set[str]] = {}
    for value in values:
        groups.setdefault(value.strip().casefold(), set()).add(value.strip())
    return sorted(
        variant
        for spellings in groups.values()
        if len(spellings) > 1
        for variant in spellings
    )


# --------------------------------------------------------------------------- #
# Issue detection
# --------------------------------------------------------------------------- #


IDENTIFIER_TOKENS = {
    "id",
    "ids",
    "key",
    "code",
    "no",
    "num",
    "number",
    "ref",
    "reference",
    "uuid",
    "guid",
    "sku",
    "pk",
    "identifier",
    "acct",
    "account",
    "invoice",
    "order",
}


def _looks_like_identifier(name: str) -> bool:
    """True when a column name reads like a key (``order_id``, ``customer no``)."""
    tokens = [t for t in re.split(r"[^0-9A-Za-z]+", name.casefold()) if t]
    if any(token in IDENTIFIER_TOKENS for token in tokens):
        return True
    # camelCase / concatenated forms such as "orderId" or "custno".
    return bool(re.search(r"(?:^|[^a-z])(id|key|code|no|ref)$", name.casefold()))


def _issue(code: str, severity: str, column: str | None, detail: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "column": column, "detail": detail}


def _detect_totals_row(frame: pd.DataFrame, columns: list[dict[str, Any]]) -> bool:
    """True when the final row looks like an appended totals line.

    A totals row is null in most text-like columns but populated in the
    numeric ones.
    """
    if frame.empty or len(frame) < 2 or not columns:
        return False

    text_cols = [c["name"] for c in columns if c["inferred_type"] in {"text", "categorical", "date"}]
    num_cols = [c["name"] for c in columns if c["inferred_type"] in {"int", "float"}]
    if not text_cols or not num_cols:
        return False

    last = frame.iloc[-1]
    text_nulls = sum(1 for c in text_cols if last.get(c) is None)
    num_filled = sum(1 for c in num_cols if last.get(c) is not None)
    return text_nulls >= max(1, math.ceil(len(text_cols) * 0.5)) and num_filled == len(num_cols)


def _collect_issues(
    frame: pd.DataFrame,
    columns: list[dict[str, Any]],
    candidate_keys: list[str],
    encoding_fallback: bool,
    raw_header: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Derive the machine-readable issue list from the computed statistics."""
    issues: list[dict[str, Any]] = []
    row_count = len(frame)

    if encoding_fallback:
        issues.append(
            _issue(
                "ENCODING_FALLBACK",
                "warning",
                None,
                "file is not valid UTF-8; decoded as latin-1",
            )
        )

    if row_count:
        dup_rows = int(frame.duplicated(keep="first").sum())
        if dup_rows:
            issues.append(
                _issue(
                    "DUPLICATE_ROWS",
                    "blocking",
                    None,
                    f"{dup_rows} fully duplicated row(s)",
                )
            )

    # Column-name hygiene. pandas renames repeated labels ("city" -> "city.1"),
    # so duplicates are counted from the raw header when one is available.
    header_counts: Counter[str] = Counter(
        name for name in (raw_header or []) if name
    ) or Counter(c["name"] for c in columns)
    reported: set[str] = set()
    for column in columns:
        name = column["name"]
        raw = column["raw_name"]
        if not name or name.startswith("Unnamed:"):
            issues.append(
                _issue(
                    "EMPTY_COLUMN_NAME",
                    "warning",
                    name,
                    f"column header is blank or placeholder (raw={raw!r})",
                )
            )
        # Match "city.1" back to the "city" that pandas renamed.
        base = re.sub(r"\.\d+$", "", name)
        for candidate in (name, base):
            if header_counts.get(candidate, 0) > 1 and candidate not in reported:
                reported.add(candidate)
                issues.append(
                    _issue(
                        "DUPLICATE_COLUMN_NAME",
                        "blocking",
                        candidate,
                        f"column name appears {header_counts[candidate]} times",
                    )
                )
                break

    for column in columns:
        name = column["name"]
        present = [
            v for v in frame[name].tolist() if v is not None
        ] if name in frame.columns and not isinstance(frame[name], pd.DataFrame) else []

        if column["non_null_count"] == 0:
            issues.append(
                _issue("ALL_NULL_COLUMN", "warning", name, "column is entirely empty")
            )
        elif column["null_rate"] > HIGH_NULL_RATE:
            issues.append(
                _issue(
                    "HIGH_NULL_RATE",
                    "warning",
                    name,
                    f"{column['null_rate']:.0%} of values are null",
                )
            )

        if len(column["detected_date_formats"]) > 1:
            issues.append(
                _issue(
                    "MIXED_DATE_FORMATS",
                    "warning",
                    name,
                    "date formats present: " + ", ".join(column["detected_date_formats"]),
                )
            )

        if column["numeric_stored_as_text"]:
            issues.append(
                _issue(
                    "NUMERIC_AS_TEXT",
                    "warning",
                    name,
                    "numeric values carry currency symbols, separators or parentheses",
                )
            )

        if column["has_leading_trailing_whitespace"]:
            issues.append(
                _issue(
                    "WHITESPACE_PADDING",
                    "informational",
                    name,
                    "values have leading or trailing whitespace",
                )
            )

        if column["inferred_type"] in {"categorical", "text"} and present:
            variants = _casing_variants(present)
            if variants:
                issues.append(
                    _issue(
                        "INCONSISTENT_CASING",
                        "informational",
                        name,
                        "same value in multiple casings: "
                        + ", ".join(repr(v) for v in variants[:MAX_SAMPLE_VALUES]),
                    )
                )

    # Near-key duplicates: an identifier-shaped column that is almost unique.
    for column in columns:
        name = column["name"]
        if name in candidate_keys or column["non_null_count"] == 0:
            continue
        if column["inferred_type"] in {"bool", "categorical", "date", "float"}:
            continue
        if not _looks_like_identifier(name):
            continue
        uniqueness = column["unique_count"] / column["non_null_count"]
        if uniqueness >= 0.9 and column["unique_count"] < column["non_null_count"]:
            dupes = column["non_null_count"] - column["unique_count"]
            issues.append(
                _issue(
                    "CANDIDATE_KEY_DUPLICATES",
                    "blocking",
                    name,
                    f"looks like an identifier but has {dupes} duplicated value(s)",
                )
            )

    if _detect_totals_row(frame, columns):
        issues.append(
            _issue(
                "SUSPECTED_TOTALS_ROW",
                "warning",
                None,
                f"row {row_count - 1} (0-indexed) looks like an appended totals line",
            )
        )

    return issues


def _outliers(frame: pd.DataFrame, columns: list[dict[str, Any]]) -> dict[str, int]:
    """Count values beyond three standard deviations, per numeric column."""
    result: dict[str, int] = {}
    for column in columns:
        if column["inferred_type"] not in {"int", "float"}:
            continue
        name = column["name"]
        present = [v for v in frame[name].tolist() if v is not None]
        numeric = _numeric_series(present)
        if len(numeric) < 3:
            result[name] = 0
            continue
        std = float(numeric.std(ddof=1))
        if not std or math.isnan(std):
            result[name] = 0
            continue
        mean = float(numeric.mean())
        result[name] = int(((numeric - mean).abs() > OUTLIER_SIGMA * std).sum())
    return result


# --------------------------------------------------------------------------- #
# Top-level profile
# --------------------------------------------------------------------------- #


def profile_file(path: Path, sheet: str | None = None) -> dict[str, Any]:
    """Profile one CSV/TSV/Excel file and return the report as a plain dict."""
    if not path.exists():
        raise ProfileError(f"file not found: {path}")

    frame, meta, fallback = read_table(path, sheet=sheet)
    row_count = len(frame)

    columns: list[dict[str, Any]] = []
    for raw_name in frame.columns:
        name = str(raw_name).strip()
        series = frame[raw_name]
        if isinstance(series, pd.DataFrame):  # duplicate labels
            series = series.iloc[:, 0]
        columns.append(_profile_column(raw_name, name, series, row_count))

    candidate_keys = [
        c["name"]
        for c in columns
        if row_count and c["null_count"] == 0 and c["unique_count"] == row_count
    ]

    # Issue detection reads columns by their stripped name.
    renamed = frame.copy()
    renamed.columns = [str(c).strip() for c in frame.columns]

    raw_header = meta.pop("_raw_header", [])

    return {
        "file": meta,
        "shape": {"rows": row_count, "columns": len(frame.columns)},
        "columns": columns,
        "issues": _collect_issues(
            renamed, columns, candidate_keys, fallback, raw_header
        ),
        "candidate_keys": candidate_keys,
        "outliers": _outliers(renamed, columns),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Writes JSON to stdout and returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="profile.py",
        description="Profile a CSV/TSV/Excel file and emit JSON data-quality stats.",
    )
    parser.add_argument("path", help="path to a .csv, .tsv, .xlsx or .xls file")
    parser.add_argument("--sheet", default=None, help="Excel sheet name (default: first)")
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="emit compact single-line JSON instead of indented JSON",
    )
    args = parser.parse_args(argv)

    try:
        report = profile_file(Path(args.path), sheet=args.sheet)
    except ProfileError as exc:
        json.dump({"error": str(exc), "path": args.path}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to stdout
        json.dump(
            {"error": f"{type(exc).__name__}: {exc}", "path": args.path}, sys.stdout
        )
        sys.stdout.write("\n")
        return 1

    if args.json_only:
        json.dump(report, sys.stdout, separators=(",", ":"), default=str)
    else:
        json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
