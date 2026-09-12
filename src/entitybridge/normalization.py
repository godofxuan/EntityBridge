"""Loss-conscious normalization and a strict identifier-missing feature boundary."""
from __future__ import annotations

import re
import unicodedata

NORMALIZATION_VERSION = "nfkc-upper-punctuation-v1"
FEATURE_VIEW_VERSION = "identifier-missing-text-redaction-v2"
MATCHER_COLUMNS = ("record_id", "record_version_id", "source", "name", "address", "city", "postcode", "country")


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value).upper()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)).strip() or None


def normalize_postcode(value: str | None) -> str | None:
    return re.sub(r"\s+", "", normalize_text(value) or "") or None


def matcher_view(record: dict, record_id: str, record_version_id: str) -> dict:
    """Project explicit features; provenance, identifiers and extra keys never pass through."""
    result = {
        "record_id": record_id, "record_version_id": record_version_id,
        "source": record["source"],
        **{key: normalize_text(record.get(key)) for key in ("name", "address", "city", "country")},
        "postcode": normalize_postcode(record.get("postcode")),
    }
    identifiers = [re.sub(r'[^A-Z0-9]', '', str(record.get(key) or '').upper()) for key in ('registration_number', 'lei')]
    identifiers = [value for value in identifiers if value]
    for field in ('name', 'address', 'city', 'postcode', 'country'):
        compact = re.sub(r'[^A-Z0-9]', '', result[field] or '')
        if any(identifier in compact for identifier in identifiers):
            result[field] = None
    return result
