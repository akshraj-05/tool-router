"""Typed exceptions for the tool_router package."""

from __future__ import annotations


class ToolRouterError(Exception):
    """Base class for all tool_router errors."""


class CatalogError(ToolRouterError):
    """The provided catalog is structurally unusable (not a dict, no tools list)."""


class ProviderError(ToolRouterError):
    """An external provider (embedding/LLM) failed; callers degrade gracefully."""
