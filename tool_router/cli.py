"""tool-router CLI.

Subcommands: `version`, `validate` (guardrail over a submissions file), `serve`
(REST API), `mcp` (MCP server), `init`/`config` (configuration), and
`import-mcp` (auto-import tool definitions).
"""

from __future__ import annotations

import argparse
import json
import sys

from tool_router import __version__
from tool_router.domain.errors import ToolRouterError
from tool_router.domain.naming import prepare_tool
from tool_router.engine.guardrail import validate_catalog


def _cmd_validate(args: argparse.Namespace) -> int:
    """Run the intake guardrail over a JSON file of tool submissions."""
    with open(args.file, encoding="utf-8") as fh:
        payload = json.load(fh)
    submissions = payload if isinstance(payload, list) else payload.get("tools", [payload])
    known_ids: set[str] = set(args.known_ids.split(",")) if args.known_ids else set()

    failures = 0
    for result in validate_catalog([prepare_tool(t) for t in submissions], known_ids):
        tool_id = result.tool_id or "<no id>"
        if result.accepted:
            print(f"ACCEPT  {tool_id}")
        else:
            failures += 1
            print(f"REJECT  {tool_id}  [{result.primary_reason}] {result.issues[0].message}")
    print(f"\n{len(submissions) - failures} accepted, {failures} rejected")
    return 0


def _load_startup_catalog(args: argparse.Namespace):
    """Combine --catalog and --import-mcp. Returns (ok, catalog or None).

    The report goes to stderr: in `mcp` mode stdout carries the protocol itself.
    """
    from tool_router.catalog_loader import build_catalog

    try:
        catalog, report = build_catalog(
            args.catalog, args.import_mcp, env_names=args.mcp_env, timeout=args.mcp_timeout,
        )
    except (ToolRouterError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False, None
    for line in report:
        print(line, file=sys.stderr)
    return True, catalog


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the REST service (requires the `api` extra)."""
    try:
        import uvicorn
    except ImportError:
        print("The service extra is not installed — run: pip install .[api]")
        return 1
    from tool_router.api import create_app

    if args.rebuild:
        import os
        os.environ["TOOL_ROUTER_REBUILD"] = "1"
    ok, catalog = _load_startup_catalog(args)
    if not ok:
        return 1
    try:
        app = create_app(catalog)
    except ToolRouterError as exc:           # e.g. invalid tools in the catalog
        print(f"error: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_init(_: argparse.Namespace) -> int:
    """Interactive first-run wizard — the user never edits a file (storage.md)."""
    import getpass

    from tool_router.store import save_secret, save_stored_config

    print("tool-router setup — press Enter to keep a [default]\n")
    provider = input("Generation LLM provider (gemini / none) [gemini]: ").strip() or "gemini"
    values = {"llm_provider": provider}
    if provider == "gemini":
        model = input("Model id [gemini-2.5-flash]: ").strip()
        if model:
            values["llm_model"] = model
        key = getpass.getpass("Gemini API key (input hidden, Enter to skip): ").strip()
        if key:
            where = save_secret("gemini_key", key)
            print(f"key stored via {where} — never logged, never committed")
        emb = input("Embedding provider (local / gemini) [local]: ").strip() or "local"
        values["embedding_provider"] = emb
    path = save_stored_config(values)
    print(f"config written to {path}\nRun: tool-router serve --catalog your_catalog.json")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """Non-interactive config management: show / set / reset."""
    from tool_router.store import config_dir, load_secret, load_stored_config, save_stored_config

    if args.action == "show":
        stored = load_stored_config()
        for key, value in sorted(stored.items()):
            print(f"{key} = {value}")
        print(f"gemini key = {'present (masked)' if load_secret('gemini_key') else 'absent'}")
        print(f"(stored in {config_dir()})")
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            print("usage: tool-router config set <key> <value>")
            return 1
        save_stored_config({args.key: args.value})
        print(f"{args.key} = {args.value}")
        return 0
    if args.action == "reset":
        path = config_dir() / "config.json"
        if path.exists():
            path.unlink()
        print("stored config cleared (secrets left intact)")
        return 0
    print("usage: tool-router config {show|set|reset}")
    return 1


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    """Where `serve` and `mcp` get their tools from."""
    parser.add_argument("--catalog", default=None, help="catalog JSON file to load")
    parser.add_argument(
        "--import-mcp", action="append", default=None, metavar="COMMAND",
        help='start this MCP server, scan its tools and load them (quote the whole command, '
             'e.g. "python my_server.py"; repeat for several servers)',
    )
    parser.add_argument(
        "--mcp-env", action="append", default=None, metavar="NAME",
        help="pass this environment variable on to the scanned MCP server(s); repeatable",
    )
    parser.add_argument(
        "--mcp-timeout", type=float, default=30.0, metavar="SECONDS",
        help="give up on an MCP scan after this long (default: 30)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tool-router", description="Intelligent tool router")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="print version")

    p_validate = sub.add_parser("validate", help="run the registry guardrail over a JSON file")
    p_validate.add_argument("file", help="JSON file: a tool object or a list of tools")
    p_validate.add_argument("--known-ids", default="", help="comma-separated already-registered ids")

    p_serve = sub.add_parser("serve", help="run the REST routing service")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    _add_source_options(p_serve)
    p_serve.add_argument("--rebuild", action="store_true",
                         help="wipe caches and re-derive the index (escape hatch)")

    sub.add_parser("init", help="interactive setup wizard (provider, key, model)")

    p_config = sub.add_parser("config", help="show / set / reset stored configuration")
    p_config.add_argument("action", choices=["show", "set", "reset"])
    p_config.add_argument("key", nargs="?", default=None)
    p_config.add_argument("value", nargs="?", default=None)

    p_mcp = sub.add_parser("mcp", help="run the MCP server (stdio) exposing find_tools")
    _add_source_options(p_mcp)

    p_imp_mcp = sub.add_parser(
        "import-mcp", help="auto-import tools from an MCP server into registry schema"
    )
    p_imp_mcp.add_argument("--command", dest="server_command", required=True,
                           help="MCP server launch command")
    p_imp_mcp.add_argument("--args", nargs="*", default=[], help="arguments for the command")
    p_imp_mcp.add_argument("--prefix", default=None, help="cluster/id prefix (default: the server's name)")
    p_imp_mcp.add_argument("--env", action="append", default=None, metavar="NAME",
                           help="pass this environment variable on to the server; repeatable")
    p_imp_mcp.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS",
                           help="give up after this long (default: 30)")
    p_imp_mcp.add_argument("-o", "--output", default=None, help="write tools JSON here (default: stdout)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "config":
        return _cmd_config(args)
    if args.command == "mcp":
        if not args.catalog and not args.import_mcp:
            parser.error("mcp needs tools: give --catalog and/or --import-mcp")
        from tool_router.mcp_server import run_mcp
        ok, catalog = _load_startup_catalog(args)
        if not ok:
            return 1
        try:
            run_mcp(catalog)
        except ToolRouterError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "import-mcp":
        from tool_router.importers.mcp_importer import MCPImportError, import_from_mcp
        try:
            tools = import_from_mcp(args.server_command, args.args, args.prefix,
                                    env_names=args.env, timeout=args.timeout)
        except MCPImportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return _emit_imported(tools, args.output)
    parser.print_help()
    return 1


def _emit_imported(tools: list[dict], output: str | None) -> int:
    """Write the imported tools that pass the guardrail; report the rest on stderr.

    Reports go to stderr so that, without -o, stdout is clean catalog JSON.
    """
    from tool_router.catalog_loader import split_valid

    accepted, rejected = split_valid(tools)
    for tool in accepted:
        print(f"IMPORT OK      {tool['id']}  ({tool.get('side_effects', 'read')})", file=sys.stderr)
    for message in rejected:
        print(f"IMPORT REJECT  {message}", file=sys.stderr)
    summary = f"{len(accepted)}/{len(tools)} tools pass the guardrail"
    if not accepted:
        print(f"{summary} · nothing written", file=sys.stderr)
        return 1
    payload = json.dumps({"tools": accepted}, indent=1)
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"{summary} · written to {output}", file=sys.stderr)
    else:
        print(payload)
        print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
