"""Hermes plugin entry point for repository-root installations."""

try:
    from .plugin.adapter import register
except ImportError:  # pytest/importers that load this as a top-level module
    from plugin.adapter import register

__all__ = ["register"]
