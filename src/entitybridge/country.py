"""Exact ISO aliases for candidate comparison; raw source values remain intact."""
from functools import lru_cache
from types import MappingProxyType

import pycountry

from .normalization import normalize_text
from .store import digest

COUNTRY_VERSION = "iso3166-exact-pycountry-24.6.1-v1"


@lru_cache(maxsize=1)
def _aliases():
    aliases = {}
    for country in pycountry.countries:
        for field in ("alpha_2", "alpha_3", "numeric", "name", "official_name", "common_name"):
            value = normalize_text(country._fields.get(field))
            if value:
                aliases.setdefault(value, set()).add(country.alpha_2)
    # An ambiguous standard spelling is not resolved by iteration order.
    return MappingProxyType({key: next(iter(values)) for key, values in aliases.items() if len(values) == 1})


def country_key(value):
    value = normalize_text(value)
    if not value:
        return None
    code = _aliases().get(value)
    return "iso:" + code if code else "unrecognized:" + value


def country_fingerprint():
    return digest({"version": COUNTRY_VERSION, "aliases": dict(_aliases())})


def country_candidate_view(records):
    """New records for retrieval only; no mutation or loss of scoring evidence."""
    return [row | {"country": country_key(row.get("country"))} for row in records]
