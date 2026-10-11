"""Shared constants for live tests. Imported by name via pytest `pythonpath` (importlib mode)."""

from datetime import UTC, datetime

ARMED_AT = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)
