"""Typed exceptions for the tool_router package."""

from __future__ import annotations


class ToolRouterError(Exception):
    """Base class for all tool_router errors."""


class CatalogError(ToolRouterError):
    """The provided catalog is structurally unusable (not a dict, no tools list)."""


class ProviderError(ToolRouterError):
    """An external provider (embedding/LLM) failed; callers degrade gracefully."""


class ToolNotFoundError(ToolRouterError):
    """No registered tool has the requested id."""


class DuplicateToolError(ToolRouterError):
    """A tool with this id is already registered."""


class InvalidToolError(ToolRouterError):
    """A submitted tool failed the intake guardrail."""

    def __init__(self, reason: str, issues: list[str]) -> None:
        super().__init__(f"{reason}: {'; '.join(issues)}")
        self.reason = reason
        self.issues = issues
