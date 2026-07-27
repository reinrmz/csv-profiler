"""Tests for scripts/profile.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import profile as profiler  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "profile.py"


def write_csv(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def column(report: dict, name: str) -> dict:
    return next(c for c in report["columns"] if c["name"] == name)


def test_clean_csv_has_no_issues(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "clean.csv",
        "id,city,amount\n1,Manila,10.5\n2,Cebu,20.25\n3,Davao,30.75\n",
    )
    report = profiler.profile_file(path)

    assert report["shape"] == {"rows": 3, "columns": 3}
    assert report["issues"] == []
    # Every column happens to be unique and non-null in a 3-row sample.
    assert "id" in report["candidate_keys"]
    assert column(report, "amount")["inferred_type"] == "float"
    assert column(report, "amount")["numeric_stored_as_text"] is False


def test_duplicate_rows_flagged(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "dupes.csv",
        "id,city\n1,Manila\n2,Cebu\n2,Cebu\n3,Davao\n",
    )
    report = profiler.profile_file(path)

    assert "DUPLICATE_ROWS" in codes(report)
    detail = next(i for i in report["issues"] if i["code"] == "DUPLICATE_ROWS")["detail"]
    assert detail.startswith("1 ")
    assert "id" not in report["candidate_keys"]


def test_numeric_with_separators_is_numeric_as_text(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "money.csv",
        'id,amount\n1,"1,234.50"\n2,"$2,000.00"\n3,"987.10"\n4,"12,345.67"\n',
    )
    report = profiler.profile_file(path)
    amount = column(report, "amount")

    assert "NUMERIC_AS_TEXT" in codes(report)
    assert amount["inferred_type"] == "float"
    assert amount["numeric_stored_as_text"] is True
    assert amount["max"] == pytest.approx(12345.67)


def test_three_date_formats_detected(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "dates.csv",
        "id,order_date\n"
        "1,2024-01-15\n"
        "2,15/01/2024\n"
        "3,Jan 15 2024\n"
        "4,2024-02-20\n"
        "5,20/02/2024\n"
        "6,Feb 20 2024\n",
    )
    report = profiler.profile_file(path)
    order_date = column(report, "order_date")

    assert "MIXED_DATE_FORMATS" in codes(report)
    assert order_date["inferred_type"] == "date"
    assert set(order_date["detected_date_formats"]) == {"%Y-%m-%d", "%d/%m/%Y", "%b %d %Y"}


def test_empty_and_single_column_files_do_not_crash(tmp_path: Path) -> None:
    header_only = write_csv(tmp_path, "headers.csv", "id,city\n")
    report = profiler.profile_file(header_only)
    assert report["shape"] == {"rows": 0, "columns": 2}
    assert report["candidate_keys"] == []

    single = write_csv(tmp_path, "single.csv", "only\na\nb\n")
    single_report = profiler.profile_file(single)
    assert single_report["shape"] == {"rows": 2, "columns": 1}

    blank = write_csv(tmp_path, "blank.csv", "")
    blank_report = profiler.profile_file(blank)
    assert blank_report["shape"] == {"rows": 0, "columns": 0}


def test_candidate_key_duplicates_only_on_identifier_columns(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "keys.csv",
        "order_id,customer_name\n"
        "SO-1,Alpha\nSO-2,Bravo\nSO-3,Charlie\nSO-4,Delta\n"
        "SO-5,Echo\nSO-6,Foxtrot\nSO-7,Golf\nSO-8,Golf\nSO-9,India\nSO-1,Hotel\n",
    )
    report = profiler.profile_file(path)
    key_issues = [i for i in report["issues"] if i["code"] == "CANDIDATE_KEY_DUPLICATES"]

    assert [i["column"] for i in key_issues] == ["order_id"]
    assert report["candidate_keys"] == []


def test_duplicate_header_survives_pandas_renaming(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "dupcol.csv", "id,city,city\n1,Manila,Cebu\n2,Davao,Cebu\n")
    report = profiler.profile_file(path)
    dupes = [i for i in report["issues"] if i["code"] == "DUPLICATE_COLUMN_NAME"]

    assert len(dupes) == 1
    assert dupes[0]["column"] == "city"
    assert "_raw_header" not in report["file"]


def test_latin1_fallback_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "latin.csv"
    path.write_bytes(b"id,city\n1,Caf\xe9\n2,Se\xf1or\n")
    report = profiler.profile_file(path)

    assert report["file"]["detected_encoding"] == "latin-1"
    assert "ENCODING_FALLBACK" in codes(report)


def test_stdout_is_pure_json(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "sample.csv", "id,amount\n1,10\n2,20\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["shape"]["rows"] == 2

    compact = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--json-only"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(compact.stdout.strip().splitlines()) == 1
    assert json.loads(compact.stdout)["shape"]["columns"] == 2


def test_unreadable_file_exits_nonzero_with_json_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.csv"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "error" in json.loads(result.stdout)
