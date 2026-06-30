from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from .client import OAClient, OAConnectorError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-agent", description="OA agent connector using existing OA endpoints")
    parser.add_argument("--base-url", default=os.getenv("OA_BASE_URL"), help="OA base URL, e.g. https://example.com/oa/")
    parser.add_argument("--cookie-file", default=os.getenv("OA_COOKIE_FILE", ".oa-session.cookies"))
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login")
    login.add_argument("--username", default=os.getenv("OA_USERNAME"))
    login.add_argument("--password", default=os.getenv("OA_PASSWORD"))

    todos = sub.add_parser("todos")
    todos.add_argument("--page", type=int, default=1)
    todos.add_argument("--page-size", type=int, default=20)

    detail = sub.add_parser("detail")
    detail.add_argument("fd_id")
    detail.add_argument("--allow-non-todo", action="store_true")

    approve = sub.add_parser("approve")
    approve.add_argument("fd_id")
    approve.add_argument("--note", required=True)
    approve.add_argument("--future-node-id")
    approve.add_argument("--execute", action="store_true")

    reject = sub.add_parser("reject")
    reject.add_argument("fd_id")
    reject.add_argument("--note", required=True)
    reject.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.base_url:
        print("missing --base-url or OA_BASE_URL", file=sys.stderr)
        return 2
    client = OAClient(args.base_url, cookie_file=args.cookie_file, verify_tls=not args.insecure)
    try:
        if args.command == "login":
            username = args.username or input("OA username: ")
            password = args.password or getpass.getpass("OA password: ")
            result = client.login(username, password)
            print(json.dumps({"ok": result, "cookieFile": args.cookie_file}, ensure_ascii=False, indent=2))
        elif args.command == "todos":
            todos = [todo.to_dict() for todo in client.list_todos(args.page, args.page_size)]
            print(json.dumps({"items": todos}, ensure_ascii=False, indent=2))
        elif args.command == "detail":
            print(json.dumps(client.get_detail(args.fd_id, require_in_todo=not args.allow_non_todo), ensure_ascii=False, indent=2))
        elif args.command == "approve":
            result = client.approve(args.fd_id, args.note, execute=args.execute, future_node_id=args.future_node_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "reject":
            result = client.reject(args.fd_id, args.note, execute=args.execute)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except OAConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
