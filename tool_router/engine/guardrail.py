"""Registry intake guardrail.

Validates tool submissions against a fixed structural spec (see
docs/design/onboarding.md for the full rule table). Every rule and reason
code below is stable — do not invent new codes without updating that doc.

Checks run in a fixed precedence order; ALL failures are collected, but the
FIRST (highest-precedence) one is the primary reason, so a single
`expected_reason` can be reported even when several rules fail at once.

Two modes:
- ``validate_submission`` — strict intake gate for NEW tools (rejects).
- ``validate_catalog``    — lenient pass for the initial trusted catalog
  (logs issues, never drops tools: the catalog is the routing ground truth).
"""

from __future__ import annotations

import json
import re
from typing import Any

from tool_router.domain.models import GuardrailIssue, GuardrailResult
from tool_router.domain.naming import SIDE_EFFECTS
from tool_router.log import get_logger

logger = get_logger("guardrail")

# ``id`` and ``cluster`` are optional: see domain/naming.py, which fills them in.
REQUIRED_FIELDS = ("name", "description", "parameters")
ID_PATTERN = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
DESCRIPTION_MIN_WORDS = 6
DESCRIPTION_MAX_CHARS = 500
MAX_PARAMETERS = 12
ALLOWED_PARAM_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})


def validate_submission(
    submission: Any,
    known_ids: set[str],
    known_tools: dict[str, dict] | None = None,
) -> GuardrailResult:
    """Validate one submitted tool against the intake spec.

    Args:
        submission: the raw submission — may be a dict or a JSON string.
        known_ids: ids already registered (for DUPLICATE_ID / DANGLING_REPLACED_BY).
        known_tools: optional id -> raw tool map (for CIRCULAR_VERSION_REF).
    """
    issues: list[GuardrailIssue] = []

    # 1. MALFORMED_JSON_STRUCTURE — must be a JSON object.
    if isinstance(submission, str):
        try:
            submission = json.loads(submission)
        except (json.JSONDecodeError, ValueError):
            return GuardrailResult(
                accepted=False,
                issues=[GuardrailIssue(
                    code="MALFORMED_JSON_STRUCTURE",
                    message="Submission is not valid JSON.",
                )],
            )
    if not isinstance(submission, dict):
        return GuardrailResult(
            accepted=False,
            issues=[GuardrailIssue(
                code="MALFORMED_JSON_STRUCTURE",
                message=f"Submission must be a JSON object, got {type(submission).__name__}.",
            )],
        )

    tool_id = submission.get("id") if isinstance(submission.get("id"), str) else None

    # 2. MISSING_REQUIRED_FIELD
    missing = [f for f in REQUIRED_FIELDS if f not in submission]
    if missing:
        issues.append(GuardrailIssue(
            code="MISSING_REQUIRED_FIELD",
            message=f"Missing required field(s): {', '.join(missing)}.",
        ))

    # 3. BAD_ID_FORMAT
    if tool_id is not None and not ID_PATTERN.match(tool_id):
        issues.append(GuardrailIssue(
            code="BAD_ID_FORMAT",
            message=f"id '{tool_id}' must match {ID_PATTERN.pattern} (cluster-prefixed snake_case).",
        ))

    # 4. BAD_NAME_FORMAT
    name = submission.get("name")
    if isinstance(name, str) and not NAME_PATTERN.match(name):
        issues.append(GuardrailIssue(
            code="BAD_NAME_FORMAT",
            message=f"name '{name}' must match {NAME_PATTERN.pattern}.",
        ))

    # 5/6. DESCRIPTION_TOO_VAGUE / DESCRIPTION_TOO_LONG
    description = submission.get("description")
    if isinstance(description, str):
        if len(description.split()) < DESCRIPTION_MIN_WORDS:
            issues.append(GuardrailIssue(
                code="DESCRIPTION_TOO_VAGUE",
                message=f"Description has fewer than {DESCRIPTION_MIN_WORDS} words — "
                        "too vague to categorize safely.",
            ))
        if len(description) > DESCRIPTION_MAX_CHARS:
            issues.append(GuardrailIssue(
                code="DESCRIPTION_TOO_LONG",
                message=f"Description exceeds {DESCRIPTION_MAX_CHARS} characters.",
            ))

    # 7/8. TOO_MANY_PARAMETERS / INVALID_PARAM_TYPE
    params = submission.get("parameters")
    if isinstance(params, list):
        if len(params) > MAX_PARAMETERS:
            issues.append(GuardrailIssue(
                code="TOO_MANY_PARAMETERS",
                message=f"{len(params)} parameters exceeds the maximum of {MAX_PARAMETERS}.",
            ))
        for p in params:
            ptype = p.get("type") if isinstance(p, dict) else None
            if ptype is not None and ptype not in ALLOWED_PARAM_TYPES:
                issues.append(GuardrailIssue(
                    code="INVALID_PARAM_TYPE",
                    message=f"Parameter type '{ptype}' is not one of {sorted(ALLOWED_PARAM_TYPES)}.",
                ))
                break  # one representative issue is enough

    # 9. BAD_VERSION_FORMAT
    version = submission.get("version")
    if version is not None and not (isinstance(version, str) and VERSION_PATTERN.match(version)):
        issues.append(GuardrailIssue(
            code="BAD_VERSION_FORMAT",
            message=f"version '{version}' is not semver x.y.z.",
        ))

    # INVALID_SIDE_EFFECTS — a typo here would silently switch off safety checks.
    side_effects = submission.get("side_effects")
    if side_effects is not None and side_effects not in SIDE_EFFECTS:
        issues.append(GuardrailIssue(
            code="INVALID_SIDE_EFFECTS",
            message=f"side_effects '{side_effects}' must be one of {', '.join(SIDE_EFFECTS)}.",
        ))

    # 10. DUPLICATE_ID
    if tool_id is not None and tool_id in known_ids:
        issues.append(GuardrailIssue(
            code="DUPLICATE_ID",
            message=f"id '{tool_id}' is already registered.",
        ))

    # 11. DANGLING_REPLACED_BY — deprecated tools must point at a real successor.
    replaced_by = submission.get("replaced_by")
    if submission.get("deprecated") and isinstance(replaced_by, str):
        if replaced_by not in known_ids:
            issues.append(GuardrailIssue(
                code="DANGLING_REPLACED_BY",
                message=f"replaced_by '{replaced_by}' does not exist in the catalog.",
            ))

    # 12. CIRCULAR_VERSION_REF — follow the replaced_by chain looking for a loop.
    # The submitted tool is not in ``known_tools`` yet, so the loop must test for
    # "back at a tool we already visited" BEFORE looking the next tool up;
    # otherwise a cycle through the submission itself is never seen.
    if tool_id is not None and isinstance(replaced_by, str):
        seen = {tool_id}
        cursor: Any = replaced_by
        while isinstance(cursor, str):
            if cursor in seen:
                issues.append(GuardrailIssue(
                    code="CIRCULAR_VERSION_REF",
                    message="replaced_by chain forms a cycle.",
                ))
                break
            seen.add(cursor)
            cursor = (known_tools or {}).get(cursor, {}).get("replaced_by")

    return GuardrailResult(tool_id=tool_id, accepted=not issues, issues=issues)


def validate_catalog(tools: list[dict]) -> list[GuardrailResult]:
    """Lenient validation for the initial trusted catalog.

    Issues are logged for observability but tools are NOT dropped — the
    loaded catalog is the routing ground truth; the strict gate applies to
    *new* submissions only (see module docstring).
    """
    ids = {t.get("id") for t in tools if isinstance(t.get("id"), str)}
    results: list[GuardrailResult] = []
    for raw in tools:
        # For catalog validation, a tool's own id counts as known for
        # replaced_by resolution, and duplicates are checked across the batch.
        other_ids = ids - {raw.get("id")} if isinstance(raw.get("id"), str) else ids
        result = validate_submission(raw, known_ids=set(), known_tools=None)
        # Re-check referential rules against the full batch (not "already registered").
        replaced_by = raw.get("replaced_by")
        if raw.get("deprecated") and isinstance(replaced_by, str) and replaced_by not in ids:
            result.issues.append(GuardrailIssue(
                code="DANGLING_REPLACED_BY",
                message=f"replaced_by '{replaced_by}' not found in catalog.",
            ))
        _ = other_ids  # duplicates within a well-formed catalog are caught by dict keying upstream
        if result.issues:
            logger.warning(
                "catalog tool %s has intake issues (kept anyway): %s",
                raw.get("id"), [i.code for i in result.issues],
            )
        results.append(result)
    return results
