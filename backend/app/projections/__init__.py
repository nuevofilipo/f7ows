"""Dimensionality reduction of chunk embeddings into 2D/3D display coordinates.

``compute`` is the pure reducer used by the worker; ``router`` serves the
pre-computed coordinates to the map UI.
"""
from app.projections.api import router
from app.projections.projections import compute

__all__ = ["compute", "router"]
