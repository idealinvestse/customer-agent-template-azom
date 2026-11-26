"""Stateless WordPress operations dashboard routes."""

from __future__ import annotations

from flask import Flask

from .files_routes import register_files_routes
from .preview_routes import register_preview_routes
from .review_routes import register_review_routes
from .sites_routes import register_sites_routes
from .ssh_routes import register_ssh_routes


def register_wp_routes(app: Flask) -> None:
    """Register all WordPress operations routes on ``app``."""
    register_sites_routes(app)
    register_ssh_routes(app)
    register_files_routes(app)
    register_review_routes(app)
    register_preview_routes(app)


__all__ = ["register_wp_routes"]
