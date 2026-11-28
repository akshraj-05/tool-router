"""Registry intake guardrail.

Validates tool submissions against a fixed structural spec (see
docs/design/onboarding.md for the full rule table). Every rule and reason
code below is stable — do not invent new codes without updating that doc.

Checks run in a fixed precedence order; ALL failures are collected, but the
FIRST (highest-precedence) one is the primary reason, so a single
`expected_reason` can be reported even when several rules fail at once.

Every path is strict: a catalog file, ``POST /registry``, the create / update /
mutate endpoints and MCP scans all apply the same rules.

- ``validate_submission`` — one tool against the tools already registered.
- ``validate_catalog``    — a whole batch, where tools may refer to each other.
"""

from __future__ import annotations

import re
from typing import Any

from toolspotter.domain.models import GuardrailIssue, GuardrailResult
from toolspotter.domain.naming import SIDE_EFFECTS

# ``id`` and ``cluster`` are optional: see domain/naming.py, which fills them in.
REQUIRED_FIELDS = ("name", "description", "parameters")
ID_PATTERN = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
DESCRIPTION_MIN_WORDS = 6
DESCRIPTION_MAX_CHARS = 500
MAX_PARAMETERS = 12
ALLOWED_PARAM_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})
MAX_NAME_CHARS = 128

TOOL_FIELDS = frozenset(
    "id name cluster description side_effects parameters tags version deprecated replaced_by source".split()
)
PARAM_FIELDS = frozenset("name type required description".split())
# Fields that earlier versions defined. Old catalogs may still carry them: ignored.
LEGACY_FIELDS = frozenset("returns aliases replaces".split())
_STRING_FIELDS = ("id", "name", "cluster", "description", "side_effects")
_OPTIONAL_STRING_FIELDS = ("version", "replaced_by")


def validate_submission(
    submission: Any,
    known_ids: set[str],
    known_tools: dict[str, dict] | None = None,
    reference_ids: set[str] | None = None,
) -> GuardrailResult:
    """Validate one submitted tool against the intake spec.

    Args:
        submission: the tool definition (a dict).
        known_ids: ids already registered (for DUPLICATE_ID).
        known_tools: optional id -> raw tool map (for CIRCULAR_VERSION_REF).
        reference_ids: ids ``replaced_by`` may point to; defaults to ``known_ids``.
    """
    issues: list[GuardrailIssue] = []

    def fail(code: str, message: str) -> None:
        issues.append(GuardrailIssue(code=code, message=message))

    # MALFORMED_JSON_STRUCTURE — must be a JSON object (text is never re-parsed: a
    # string that happens to contain JSON would otherwise be indexed as a string).
    if not isinstance(submission, dict):
        return GuardrailResult(accepted=False, issues=[GuardrailIssue(
            code="MALFORMED_JSON_STRUCTURE",
            message=f"A tool must be a JSON object, got {type(submission).__name__}.")])

    tool_id = submission.get("id") if isinstance(submission.get("id"), str) else None

    # MISSING_REQUIRED_FIELD
    missing = [f for f in REQUIRED_FIELDS if f not in submission]
    if missing:
        fail("MISSING_REQUIRED_FIELD", f"Missing required field(s): {', '.join(missing)}.")

    # UNKNOWN_FIELD — a typo such as "side_effect" must not be silently ignored.
    unknown = sorted(str(k) for k in set(submission) - TOOL_FIELDS - LEGACY_FIELDS)
    if unknown:
        fail("UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}.")

    # INVALID_FIELD_TYPE
    wrong = [f"{f} must be a string" for f in _STRING_FIELDS
             if f in submission and not isinstance(submission[f], str)]
    wrong += [f"{f} must be a string or null" for f in _OPTIONAL_STRING_FIELDS
              if submission.get(f) is not None and not isinstance(submission[f], str)]
    if "deprecated" in submission and not isinstance(submission["deprecated"], bool):
        wrong.append("deprecated must be true or false")
    tags = submission.get("tags", [])
    if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        wrong.append("tags must be a list of strings")
    if "parameters" in submission and not isinstance(submission["parameters"], list):
        wrong.append("parameters must be a list")
    if "source" in submission and not isinstance(submission["source"], dict):
        wrong.append("source must be an object")
    if wrong:
        fail("INVALID_FIELD_TYPE", "; ".join(wrong) + ".")

    # BAD_ID_FORMAT
    if tool_id is not None and (len(tool_id) > MAX_NAME_CHARS or not ID_PATTERN.match(tool_id)):
        fail("BAD_ID_FORMAT",
             f"id '{tool_id[:60]}' must match {ID_PATTERN.pattern} (cluster-prefixed snake_case), "
             f"at most {MAX_NAME_CHARS} characters.")

    # BAD_NAME_FORMAT
    name = submission.get("name")
    if isinstance(name, str) and (len(name) > MAX_NAME_CHARS or not NAME_PATTERN.match(name)):
        fail("BAD_NAME_FORMAT",
             f"name '{name[:60]}' must match {NAME_PATTERN.pattern}, at most {MAX_NAME_CHARS} characters.")

    # DESCRIPTION_TOO_VAGUE / DESCRIPTION_TOO_LONG
    description = submission.get("description")
    if isinstance(description, str):
        if len(description.split()) < DESCRIPTION_MIN_WORDS:
            fail("DESCRIPTION_TOO_VAGUE",
                 f"Description has fewer than {DESCRIPTION_MIN_WORDS} words — "
                 "too vague to categorize safely.")
        if len(description) > DESCRIPTION_MAX_CHARS:
            fail("DESCRIPTION_TOO_LONG", f"Description exceeds {DESCRIPTION_MAX_CHARS} characters.")

    # TOO_MANY_PARAMETERS / INVALID_PARAMETER / INVALID_PARAM_TYPE
    params = submission.get("parameters")
    if isinstance(params, list):
        if len(params) > MAX_PARAMETERS:
            fail("TOO_MANY_PARAMETERS",
                 f"{len(params)} parameters exceeds the maximum of {MAX_PARAMETERS}.")
        problem = _parameter_problem(params)
        if problem:
            fail("INVALID_PARAMETER", problem)
        for p in params:
            ptype = p.get("type") if isinstance(p, dict) else None
            if ptype is not None and (not isinstance(ptype, str) or ptype not in ALLOWED_PARAM_TYPES):
                fail("INVALID_PARAM_TYPE",
                     f"Parameter type '{ptype}' is not one of {sorted(ALLOWED_PARAM_TYPES)}.")
                break  # one representative issue is enough

    # BAD_VERSION_FORMAT
    version = submission.get("version")
    if isinstance(version, str) and not VERSION_PATTERN.match(version):
        fail("BAD_VERSION_FORMAT", f"version '{version}' is not semver x.y.z.")

    # INVALID_SIDE_EFFECTS — a typo here would silently switch off safety checks.
    side_effects = submission.get("side_effects")
    if isinstance(side_effects, str) and side_effects not in SIDE_EFFECTS:
        fail("INVALID_SIDE_EFFECTS",
             f"side_effects '{side_effects}' must be one of {', '.join(SIDE_EFFECTS)}.")

    # DUPLICATE_ID
    if tool_id is not None and tool_id in known_ids:
        fail("DUPLICATE_ID", f"id '{tool_id}' is already registered.")

    # DANGLING_REPLACED_BY — the successor must exist, deprecated or not.
    replaced_by = submission.get("replaced_by")
    if isinstance(replaced_by, str):
        targets = known_ids if reference_ids is None else reference_ids
        if replaced_by != tool_id and replaced_by not in targets:
            fail("DANGLING_REPLACED_BY", f"replaced_by '{replaced_by}' does not exist in the catalog.")

    # CIRCULAR_VERSION_REF — follow the replaced_by chain looking for a loop.
    # The submitted tool may not be in ``known_tools`` yet, so the loop tests for
    # "back at a tool we already visited" BEFORE looking the next tool up.
    if tool_id is not None and isinstance(replaced_by, str):
        seen = {tool_id}
        cursor: Any = replaced_by
        while isinstance(cursor, str):
            if cursor in seen:
                fail("CIRCULAR_VERSION_REF", "replaced_by chain forms a cycle.")
                break
            seen.add(cursor)
            cursor = (known_tools or {}).get(cursor, {}).get("replaced_by")

    return GuardrailResult(tool_id=tool_id, accepted=not issues, issues=issues)


def _parameter_problem(params: list[Any]) -> str | None:
    """The first structural problem in a parameter list, or None."""
    names: set[str] = set()
    for position, p in enumerate(params):
        where = f"parameters[{position}]"
        if not isinstance(p, dict):
            return f"{where} must be an object."
        extra = sorted(str(k) for k in set(p) - PARAM_FIELDS)
        if extra:
            return f"{where} has unknown field(s): {', '.join(extra)}."
        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            return f"{where} needs a non-empty string 'name'."
        if name in names:
            return f"{where} repeats the parameter name '{name}'."
        names.add(name)
        if "required" in p and not isinstance(p["required"], bool):
            return f"{where}.required must be true or false."
        if "description" in p and not isinstance(p["description"], str):
            return f"{where}.description must be a string."
    return None


def validate_catalog(
    tools: list[Any], existing_ids: set[str] | None = None
) -> list[GuardrailResult]:
    """Validate a batch of tools together; one result per tool, in order.

    Tools may point at each other with ``replaced_by`` in any order. The first
    tool with a given id is accepted; later ones are duplicates.
    """
    by_id = {t["id"]: t for t in tools if isinstance(t, dict) and isinstance(t.get("id"), str)}
    seen = set(existing_ids or ())
    references = seen | set(by_id)
    results: list[GuardrailResult] = []
    for raw in tools:
        result = validate_submission(raw, known_ids=seen, known_tools=by_id, reference_ids=references)
        if result.tool_id is not None:
            seen.add(result.tool_id)
        results.append(result)
    return results
