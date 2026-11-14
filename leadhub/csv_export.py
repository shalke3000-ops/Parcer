"""CSV export with spreadsheet formula-injection protection (OWASP CSV injection)."""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

# Cells starting with these are interpreted as formulas by Excel/Sheets/LibreOffice.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "|", "%")
UTF8_BOM = "﻿"  # lets Excel open Cyrillic text correctly

LEAD_COLUMNS = (
    "id", "source", "source_name", "score", "budget_eur", "status", "posted_at", "found_at", "author", "link", "text",
    "note",
)
BUSINESS_COLUMNS = (
    "id", "name", "category", "city", "address", "distance_m", "phone", "website", "messenger", "social",
    "opportunity", "status", "lat", "lon", "osm_type", "osm_id", "note",
)


def escape_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    if isinstance(value, (int, float)):
        return text  # genuine numbers are safe and should stay numeric
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def to_csv(rows: Iterable[dict[str, Any]], columns: tuple[str, ...]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([escape_cell(row.get(col)) for col in columns])
    return UTF8_BOM + buffer.getvalue()
