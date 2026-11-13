"""
Schema loader: reads workflow JSON schemas and dispositions CSV.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from solution.normalise import norm_header

# Path to schema directory (relative to repo root; works from any cwd)
_SCHEMA_DIR = Path(__file__).parent.parent / "assignment-a-data" / "schemas"


def load_workflow_schema(workflow_id: str) -> dict:
    """Load and return the parsed JSON schema for a workflow."""
    path = _SCHEMA_DIR / f"{workflow_id}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_header_map(schema: dict, csv_headers: list[str]) -> dict[str, dict]:
    """
    Map each CSV column → schema field definition.
    Returns {csv_col_original: field_def} for matched columns only.
    Unknown columns are silently ignored per spec.
    """
    fields = schema["fields"]
    # Build lookup: normalised name/alias → field def
    lookup: dict[str, dict] = {}
    for field in fields:
        lookup[norm_header(field["name"])] = field
        for alias in field.get("aliases", []):
            lookup[norm_header(alias)] = field

    mapping: dict[str, dict] = {}
    for col in csv_headers:
        key = norm_header(col)
        if key in lookup:
            mapping[col] = lookup[key]
    return mapping


def get_reference_field(schema: dict) -> Optional[str]:
    """Return the name of the field marked 'reference: true'."""
    for f in schema["fields"]:
        if f.get("reference"):
            return f["name"]
    return None


_dispositions: Optional[dict[str, dict[str, dict]]] = None


def load_dispositions() -> dict[str, dict[str, dict]]:
    """
    Returns {workflow_id: {code: {is_terminal, is_success}}}.
    Cached after first load.
    """
    global _dispositions
    if _dispositions is not None:
        return _dispositions
    path = _SCHEMA_DIR / "dispositions.csv"
    result: dict[str, dict[str, dict]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wf = row["workflow_id"]
            code = row["code"]
            result.setdefault(wf, {})[code] = {
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    _dispositions = result
    return result

