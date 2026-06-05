"""Safe real-mode wiring check for TrueFoundry + Bedrock.

This command never invokes destructive tools. It validates config, optionally sends one
small AI Gateway chat completion, lists MCP tools, and verifies the DynamoDB table when
the production state backend is enabled.

Run:
    python scripts/real_doctor.py
    python scripts/real_doctor.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deadman.config as config  # noqa: E402


def _check(name: str, ok: bool, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, **extra}


def _tool_name(tool: dict) -> str:
    return str(tool.get("name") or tool.get("tool") or tool.get("id") or "")


def _print_human(result: dict[str, Any]) -> None:
    print("=" * 72)
    print("DEADMAN real-mode doctor")
    print("=" * 72)
    readiness = result["readiness"]
    print(f"mode={readiness['mode']} state_backend={readiness['state_backend']} "
          f"demo_enabled={readiness['demo_enabled']}")

    if readiness["errors"]:
        print("\nReadiness errors:")
        for issue in readiness["errors"]:
            print(f"  - {issue['field']}: {issue['message']}")
    if readiness["warnings"]:
        print("\nReadiness warnings:")
        for issue in readiness["warnings"]:
            print(f"  - {issue['field']}: {issue['message']}")

    print("\nChecks:")
    for check in result["checks"]:
        status = "PASS" if check["ok"] else "FAIL"
        line = f"  [{status}] {check['name']}"
        if check.get("detail"):
            line += f" — {check['detail']}"
        print(line)


def _ai_check(prompt: str) -> dict[str, Any]:
    from deadman import realmode_ai

    response = realmode_ai.complete(prompt)
    text = response.get("text", "")
    return _check(
        "AI Gateway completion",
        True,
        f"served_by={response.get('served_by')} depth={response.get('fallback_depth')} "
        f"cache={response.get('from_cache')}",
        text_sample=text[:160],
    )


def _mcp_check(required_tools: set[str]) -> dict[str, Any]:
    from deadman import realmode_mcp

    tools = realmode_mcp.list_tools()
    names = sorted(name for name in (_tool_name(t) for t in tools) if name)
    missing = sorted(required_tools - set(names))
    ok = not missing
    detail = f"{len(names)} tool(s) visible via {realmode_mcp.selected_transport()} transport"
    if missing:
        detail += f"; missing expected tools: {', '.join(missing)}"
    return _check("MCP Gateway tool listing", ok, detail, tools=names[:50], missing=missing)


def _dynamodb_check() -> dict[str, Any]:
    if config.STATE_BACKEND != "dynamodb":
        return _check("DynamoDB state table", True, "skipped; DEADMAN_STATE_BACKEND is not dynamodb")

    try:
        import boto3  # type: ignore[import]

        client_kwargs: dict[str, Any] = {"region_name": config.AWS_REGION}
        endpoint_url = os.getenv("AWS_ENDPOINT_URL")
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        dynamodb = boto3.client("dynamodb", **client_kwargs)
        dynamodb.describe_table(TableName=config.DYNAMODB_TABLE)
        return _check("DynamoDB state table", True, f"found {config.DYNAMODB_TABLE}")
    except Exception as exc:  # noqa: BLE001
        return _check("DynamoDB state table", False, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate DEADMAN real-mode wiring safely.")
    parser.add_argument("--skip-ai", action="store_true", help="Do not call the AI Gateway.")
    parser.add_argument("--skip-mcp", action="store_true", help="Do not connect to the MCP Gateway.")
    parser.add_argument("--skip-dynamodb", action="store_true", help="Do not check the DynamoDB state table.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--prompt",
        default="Reply with one short sentence confirming DEADMAN real-mode AI Gateway connectivity.",
        help="Prompt for the safe AI Gateway smoke call.",
    )
    parser.add_argument(
        "--required-tool",
        action="append",
        default=[],
        help="Tool name expected in the MCP tool list. Can be supplied multiple times.",
    )
    args = parser.parse_args()

    required_tools = set(args.required_tool) or {"cw.get_metrics", "github.revert_pr"}
    result: dict[str, Any] = {"readiness": config.readiness(), "checks": []}

    result["checks"].append(
        _check(
            "configuration",
            not result["readiness"]["errors"] and config.MODE == "real",
            "readyz-compatible real config" if config.MODE == "real" else "set DEADMAN_MODE=real",
        )
    )

    if not args.skip_ai and not result["readiness"]["errors"]:
        try:
            result["checks"].append(_ai_check(args.prompt))
        except Exception as exc:  # noqa: BLE001
            result["checks"].append(_check("AI Gateway completion", False, str(exc)))

    if not args.skip_mcp and not result["readiness"]["errors"]:
        try:
            result["checks"].append(_mcp_check(required_tools))
        except Exception as exc:  # noqa: BLE001
            result["checks"].append(_check("MCP Gateway tool listing", False, str(exc)))

    if not args.skip_dynamodb:
        result["checks"].append(_dynamodb_check())

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    return 0 if all(c["ok"] for c in result["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
