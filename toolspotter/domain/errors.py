"""Typed exceptions for the toolspotter package."""

from __future__ import annotations


class ToolSpotterError(Exception):
    """Base class for all toolspotter errors."""


class CatalogError(ToolSpotterError):
    """The provided catalog is structurally unusable (not a dict, no tools list)."""


class ProviderError(ToolSpotterError):
    """An external provider (embedding/LLM) failed; callers degrade gracefully."""


class ToolNotFoundError(ToolSpotterError):
    """No registered tool has the requested id."""


class DuplicateToolError(ToolSpotterError):
    """A tool with this id is already registered."""


class InvalidToolError(ToolSpotterError):
    """A submitted tool failed the intake guardrail."""

    def __init__(self, reason: str, issues: list[str]) -> None:
        super().__init__(f"{reason}: {'; '.join(issues)}")
        self.reason = reason
        self.issues = issues


class InvalidCatalogError(CatalogError):
    """One or more tools in a catalog failed the intake guardrail.

    ``rejected`` lists EVERY failing tool, so they can all be fixed in one pass:
    ``{"index": int, "id": str | None, "reason": str, "issues": [str, ...]}``.
    """

    def __init__(self, rejected: list[dict]) -> None:
        shown = [
            f"  tools[{r['index']}] {r['id'] or '(no id)'}  [{r['reason']}] {r['issues'][0]}"
            for r in rejected[:20]
        ]
        if len(rejected) > 20:
            shown.append(f"  … and {len(rejected) - 20} more")
        super().__init__(f"{len(rejected)} invalid tool(s) in the catalog:\n" + "\n".join(shown))
        self.rejected = rejected


class ToolInUseError(ToolSpotterError):
    """Another tool points at this one with ``replaced_by``."""


class StateError(ToolSpotterError):
    """The saved registry could not be read or written."""
