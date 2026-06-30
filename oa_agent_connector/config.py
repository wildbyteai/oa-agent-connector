from __future__ import annotations

import argparse
import json
from pathlib import Path


def default_state_dir() -> Path:
    return Path.home() / ".oa-agent-connector"


def build_config(base_url: str, state_dir: str | None = None, server_name: str = "oa") -> dict:
    resolved_state_dir = Path(state_dir).expanduser() if state_dir else default_state_dir()
    return {
        "mcpServers": {
            server_name: {
                "command": "oa-agent-mcp",
                "env": {
                    "OA_BASE_URL": base_url,
                    "OA_AGENT_STATE_DIR": str(resolved_state_dir),
                },
            }
        }
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oa-agent-mcp-config",
        description="Print MCP config with the current computer's absolute OA state directory.",
    )
    parser.add_argument("--base-url", required=True, help="OA address provided by the user.")
    parser.add_argument("--state-dir", help="Optional absolute state directory. Defaults to the user's home directory.")
    parser.add_argument("--server-name", default="oa", help="MCP server name in the client config.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = build_config(args.base_url, state_dir=args.state_dir, server_name=args.server_name)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
