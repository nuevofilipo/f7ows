"""User preferences: the unified, validated layer for all tunable knobs."""
from app.preferences.preferences import Preferences, describe, resolve, validate

__all__ = ["Preferences", "describe", "resolve", "validate"]
