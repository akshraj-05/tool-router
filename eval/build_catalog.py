"""Rebuild the GitHub and Playwright catalogs from the source projects (see SOURCES.md).

    python eval/build_catalog.py PATH/github-mcp-server PATH/playwright-mcp

Which tools: every tool listed in the GitHub server's README, and Playwright's
default groups (core automation and tab management). Definitions are converted by
the router's own MCP importer and pass its guardrail; nothing is edited by hand.
Only files are read; neither server is run.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from toolspotter.catalog_loader import split_valid  # noqa: E402
from toolspotter.importers.mcp_importer import _convert  # noqa: E402

DEFAULT_GROUPS = ("Core automation", "Tab management")


def _tool(name: str, description: str, schema: dict, hints: dict) -> SimpleNamespace:
    annotations = SimpleNamespace(
        readOnlyHint=hints.get("readOnlyHint"), destructiveHint=hints.get("destructiveHint")
    )
    return SimpleNamespace(
        name=name, description=description, inputSchema=schema, annotations=annotations
    )


def github_tools(root: Path) -> list[SimpleNamespace]:
    readme = (root / "README.md").read_text(encoding="utf-8")
    listed = readme.split("START AUTOMATED TOOLS -->")[1].split("END AUTOMATED TOOLS")[0]
    names = dict.fromkeys(re.findall(r"^- \*\*([a-z0-9_]+)\*\*", listed, re.M))
    tools = []
    for name in names:
        path = root / "pkg" / "github" / "__toolsnaps__" / f"{name}.snap"
        if not path.exists():
            print(f"no definition for {name}", file=sys.stderr)
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        tools.append(_tool(raw["name"], raw.get("description", ""),
                           raw.get("inputSchema") or {}, raw.get("annotations") or {}))
    return tools


def playwright_tools(root: Path) -> list[SimpleNamespace]:
    readme = (root / "README.md").read_text(encoding="utf-8")
    tools = []
    for section in readme.split("<summary><b>")[1:]:
        if section.split("</b>")[0] not in DEFAULT_GROUPS:
            continue
        body = section.split("</details>")[0]
        for block in re.split(r"^- \*\*(?=browser_)", body, flags=re.M)[1:]:
            name = block.split("**")[0]
            description = re.search(r"^  - Description: (.*)$", block, re.M).group(1)
            read_only = re.search(r"Read-only: \*\*(true|false)\*\*", block).group(1) == "true"
            properties, required = {}, []
            for param, kind, optional, text in re.findall(
                r"^    - `(\w+)` \((\w+)(, optional)?\): (.*)$", block, re.M
            ):
                properties[param] = {"type": kind, "description": text.strip()}
                if not optional:
                    required.append(param)
            tools.append(_tool(name, description,
                               {"properties": properties, "required": required},
                               {"readOnlyHint": read_only}))
    return tools


def main(github: str, playwright: str) -> None:
    for folder, prefix, tools in (("github", "github", github_tools(Path(github))),
                                  ("playwright", "browser", playwright_tools(Path(playwright)))):
        used: set[str] = set()
        converted = [_convert(t, prefix, used) for t in tools]
        accepted, rejected = split_valid(converted)
        print(f"{folder}: {len(accepted)} of {len(converted)} tools accepted")
        for message in rejected:
            print("  skipped", message)
        (HERE / folder / "catalog.json").write_text(
            json.dumps({"tools": accepted}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
