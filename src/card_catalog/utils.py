"""Small shared helpers."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Naive UTC datetime, replacing the deprecated ``datetime.utcnow()``.

    SQLAlchemy's :class:`DateTime` columns are timezone-naive by default; we
    stash UTC as naive to keep the schema stable and avoid mixed-aware
    comparisons elsewhere in the codebase.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
