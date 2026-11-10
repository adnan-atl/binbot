from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class RawCollection:
    label: str  # bin label as the council names it
    date: date


@dataclass(frozen=True)
class LookupInput:
    postcode: str
    uprn: str | None = None
    house_number: str | None = None
    address_line: str | None = None  # "10 Abingdon Road, London": street-based councils need it


class AdapterError(Exception):
    """Fetch failed. `retryable` says whether a later attempt might work."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class NoCollectionsForAddress(AdapterError):
    """The council answered, but lists nothing for this address (flat with communal bins,
    commercial unit, wrong UPRN). Says nothing about whether the scraper works, so it must
    not count against the council's health."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class MissingInput(AdapterError):
    def __init__(self, field: str):
        super().__init__(f"adapter needs {field}", retryable=False)
        self.field = field


class CalendarAdapter(Protocol):
    name: str

    def fetch(self, lookup: LookupInput) -> list[RawCollection]: ...


_UNREACHABLE = ("timed out", "timeout", "read operation", "connecterror", "connection refused",
                "connection reset", "remote end closed", "name or service not known",
                "temporary failure in name resolution", "403", "forbidden", "502", "503", "504",
                "website is down")


def unreachable(error: str | None) -> bool:
    """The council's website didn't answer us (timed out, refused, blocked, down). Trying again
    with other inputs or for the neighbours just hits the same site, so callers stop."""
    e = (error or "").lower()
    return any(k in e for k in _UNREACHABLE)
