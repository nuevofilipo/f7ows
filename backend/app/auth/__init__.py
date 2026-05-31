"""Authentication: registration, login, logout, and the current-user dependency."""
from app.auth.auth import get_current_user, router

__all__ = ["get_current_user", "router"]
