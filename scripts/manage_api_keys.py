from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.api_keys import ApiKeyRegistry  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local Mining QA API keys.")
    parser.add_argument("--registry", default=str(PROJECT_ROOT / "data" / "api_keys.json"), help="Registry JSON path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a new API key")
    create.add_argument("--name", required=True, help="Human-readable key name")
    create.add_argument("--purpose", default=None, help="Optional key purpose")

    subparsers.add_parser("list", help="List API key metadata")

    disable = subparsers.add_parser("disable", help="Disable an API key by key_id")
    disable.add_argument("key_id")

    enable = subparsers.add_parser("enable", help="Enable an API key by key_id")
    enable.add_argument("key_id")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    registry = ApiKeyRegistry(Path(args.registry))

    if args.command == "create":
        record, plain_key = registry.create(args.name, purpose=args.purpose)
        print(json.dumps({"record": record.to_dict(), "api_key": plain_key}, ensure_ascii=False, indent=2))
        print("Save the api_key now. It is not stored in plaintext.", file=sys.stderr)
        return 0

    if args.command == "list":
        payload = {"keys": [record.to_dict() for record in registry.load()]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "disable":
        record = registry.set_enabled(args.key_id, False)
        if not record:
            print(f"API key not found: {args.key_id}", file=sys.stderr)
            return 1
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "enable":
        record = registry.set_enabled(args.key_id, True)
        if not record:
            print(f"API key not found: {args.key_id}", file=sys.stderr)
            return 1
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
