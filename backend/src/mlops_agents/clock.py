from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """A naive datetime is read as UTC; an aware one is returned as is."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def to_iso(dt: datetime) -> str:
    dt = as_utc(dt)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return to_iso(utc_now())
