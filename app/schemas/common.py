"""Shared constants and small schemas."""
from typing import Literal

import pycountry

INTERESTS = ("Music", "Travel", "Gaming", "Movies", "Fitness", "Art", "Food", "Anime", "Tech", "Books")
Gender = Literal["male", "female", "other"]
Platform = Literal["android", "ios", "web"]
COUNTRY_CODES = frozenset(c.alpha_2 for c in pycountry.countries)


def validate_country(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.upper()
    if v not in COUNTRY_CODES:
        raise ValueError("Unknown ISO 3166-1 alpha-2 country code")
    return v

