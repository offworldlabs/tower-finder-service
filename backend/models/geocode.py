"""Pydantic models for the address-lookup endpoint.

The request is one free-text field, so the constraints on it are the whole of
the input validation: length is bounded before the string is ever put on a
query string to somebody else's service, and unknown keys are refused rather
than ignored, so a client sending `adress` learns it at once.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Stripped first, then measured: " " is empty and must be rejected, while an
# address padded out to 201 characters by trailing spaces is not too long.
# 200 is comfortably past the longest real address and short enough that the
# upstream URL stays sane.
AddressQuery = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class GeocodeRequest(BaseModel):
    """POST body for /api/geocode."""

    model_config = ConfigDict(extra="forbid")

    query: AddressQuery = Field(..., description="Free-text address, place or ZIP to resolve")


class GeocodeResponse(BaseModel):
    """A resolved point, and how well it is pinned down.

    ``precision`` is what the UI needs to choose a map zoom: a street match is
    a rooftop, a postcode or a locality is a neighbourhood or a city.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="The stripped query this answers")
    latitude: float
    longitude: float
    matched_address: str = Field(..., description="The address as the provider spells it")
    provider: Literal["census", "nominatim"]
    precision: Literal["street", "postcode", "locality"]
