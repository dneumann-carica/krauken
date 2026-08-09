"""krauken-ipc: operator/debug CLI for the daemon's Unix socket. This is the
debuggability tool that a plain `curl --unix-socket` would otherwise have
given us for free, had we used HTTP-over-socket instead of NDJSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from krauken.ipc.client import AsyncIPCClient

DEFAULT_SOCKET = "/run/krauken/daemon.sock"


async def _run(args: argparse.Namespace) -> None:
    client = AsyncIPCClient(args.socket)
    if args.command == "ping":
        result = await client.call("system.ping")
        print(json.dumps(result))
    elif args.command == "call":
        call_args = {}
        for kv in args.arg or []:
            key, _, value = kv.partition("=")
            call_args[key] = value
        result = await client.call(args.op, call_args)
        print(json.dumps(result, indent=2))
    else:  # pragma: no cover
        sys.exit(f"unknown command {args.command}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="krauken-ipc")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    call = sub.add_parser("call")
    call.add_argument("op")
    call.add_argument("--arg", action="append", help="key=value, repeatable")

    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except Exception as e:  # noqa: BLE001 -- CLI top-level error reporting
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
