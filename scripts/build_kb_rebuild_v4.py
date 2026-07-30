from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_rebuild import DEFAULT_LEGACY_DB, DEFAULT_REBUILD_ROOT, build_corpus, validate_corpus  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the isolated v4 cleaned/structured private corpus. No search index is built."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_REBUILD_ROOT, help="Ignored v4 corpus directory.")
    parser.add_argument("--legacy-db", type=Path, default=DEFAULT_LEGACY_DB, help="Read-only governed v3 source DB.")
    parser.add_argument("--validate-only", action="store_true", help="Validate an existing v4 corpus without rebuilding it.")
    args = parser.parse_args()

    result = validate_corpus(args.root) if args.validate_only else build_corpus(root=args.root, legacy_db=args.legacy_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
