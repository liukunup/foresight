#!/usr/bin/env python3
"""Run a read-only SQL statement against the ForeSight API.

Usage:
    python3 query.py --sql "SELECT COUNT(*) FROM messages"
    python3 query.py --sql "SELECT * FROM messages LIMIT 5" --format json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONNECTION_FILE = Path.home() / ".config" / "foresight" / "connection.toml"


def load_connection(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"connection file not found: {path}. Run scripts/install_skill.sh first."
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


def request_json(url: str, token: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API returned {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach the API: {exc.reason}") from exc


def print_table(columns: list[str], rows: list[list]) -> None:
    if not columns:
        return
    widths = [len(column) for column in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    header = " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    print(header)
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", required=True, help="Read-only SQL statement")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument(
        "--connection",
        type=Path,
        default=DEFAULT_CONNECTION_FILE,
        help="Path to connection.toml",
    )
    args = parser.parse_args()

    connection = load_connection(args.connection)
    base_url = str(connection.get("api_base_url", "http://127.0.0.1:8000")).rstrip("/")
    token_env = str(connection.get("token_env", "FORESIGHT_API_TOKEN"))
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise SystemExit(f"{token_env} is not set")

    body = request_json(
        f"{base_url}/api/query",
        token,
        {"sql": args.sql, "params": {}, "limit": args.limit, "offset": args.offset},
    )
    if args.format == "json":
        print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print_table(body.get("columns", []), body.get("rows", []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
