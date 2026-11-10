"""CLI for managing memsilo per-user access tokens.

Usage:
    python scripts/manage_users.py add [user_id] --display-name "cust 001"
    python scripts/manage_users.py bulk-add users.csv   # header: user_id,display_name
    python scripts/manage_users.py erase <user_id> --yes [--revoke]
    python scripts/manage_users.py export <user_id>
    python scripts/manage_users.py rotate <user_id>
    python scripts/manage_users.py revoke <user_id>
    python scripts/manage_users.py list

    python scripts/manage_users.py --users-file /path/to/users.json list

`--json` is a global option and goes BEFORE the subcommand
(`manage_users.py --json add ...`); add, bulk-add and erase then print one JSON
document. export always prints JSON. add/rotate/revoke/list/bulk-add need no
API key and never import mem0; erase and export build the full Mem0, so the LLM
key must be set (a missing key is a one-line error, exit 1), but they never call
the LLM provider: a placeholder value works. erase and export refuse an id that
is not a well-formed user id (mem0's Qdrant store treats "*" as match-any).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

# This script is run directly (`python scripts/manage_users.py ...`, `just users
# ...`, and the same inside the Docker image), which puts `scripts/` — not the
# repo root — on sys.path, and `package-mode = false` means nothing is installed
# into the venv either. Without this bootstrap the `config`/`users` imports below
# fail with ModuleNotFoundError. Same pattern as mcp_server.py's, but resolving
# to the *parent* of scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ConfigError, config
from memory import EraseIncomplete, count_user, erase_user, export_user
from users import UserStore, generate_user_id, validate_user_id

USERS_FILE = config.users_file


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2))


def _read_bulk_rows(path: str, store: UserStore) -> list[tuple[str, str]]:
    """Parse and fully validate a bulk-add CSV; raise ValueError naming the row."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None or [h.strip() for h in header] != ["user_id", "display_name"]:
                raise ValueError(
                    "bulk-add: the first line must be the header: user_id,display_name"
                )
            existing = {u["user_id"] for u in store.list_users()}
            seen: set[str] = set()
            rows: list[tuple[str, str]] = []
            for line_no, row in enumerate(reader, start=2):
                if not row or not any(c.strip() for c in row):
                    continue  # blank line
                if len(row) != 2:
                    raise ValueError(f"bulk-add: row {line_no}: expected 2 columns, got {len(row)}")
                uid, name = row[0].strip(), row[1].strip()
                if uid:
                    try:
                        validate_user_id(uid)
                    except ValueError as e:
                        raise ValueError(f"bulk-add: row {line_no}: {e}") from None
                    if uid in seen:
                        raise ValueError(
                            f"bulk-add: row {line_no}: duplicate user_id {uid!r} in file"
                        )
                    if uid in existing:
                        raise ValueError(f"bulk-add: row {line_no}: user_id {uid!r} already exists")
                    seen.add(uid)
                if not name:
                    raise ValueError(f"bulk-add: row {line_no}: display_name is empty")
                rows.append((uid, name))
    except OSError as e:
        raise ValueError(f"bulk-add: cannot read {path!r}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ValueError(f"bulk-add: {path!r} is not valid UTF-8") from None
    if not rows:
        raise ValueError("bulk-add: the file has no rows")
    return rows


def _default_memory_factory():
    # Lazy: only erase/export need Mem0, and building it needs the LLM key.
    config.require_llm_key()
    # Mem0 reads MEM0_TELEMETRY once, at import (lazy, inside build_memory), and
    # importing it loads onnxruntime, which reads ORT_DISABLE_TELEMETRY then: set
    # them first - same rule as mcp_server.py. Mem0's is off unless config
    # enables it; onnxruntime's and huggingface_hub's are always off.
    from memory import build_memory, telemetry_env

    os.environ.update(telemetry_env(config.telemetry))

    return build_memory(config)


def main(argv: list[str] | None = None, memory_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="manage_users.py")
    # An explicit path beats every other source (MEMSILO_USERS_FILE, config.yaml
    # and `.env`). Anything that must be certain which store it writes (a test,
    # a script operating on a second store) says so here.
    parser.add_argument(
        "--users-file",
        default=None,
        metavar="PATH",
        help="Path to the token store. Overrides MEMSILO_USERS_FILE and config.yaml.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON document instead of text (before the subcommand).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add")
    add_p.add_argument("user_id", nargs="?", default=None)
    add_p.add_argument("--display-name", required=True)

    bulk_p = sub.add_parser("bulk-add")
    bulk_p.add_argument("file", metavar="FILE")

    rotate_p = sub.add_parser("rotate")
    rotate_p.add_argument("user_id")

    revoke_p = sub.add_parser("revoke")
    revoke_p.add_argument("user_id")

    sub.add_parser("list")

    erase_p = sub.add_parser("erase")
    erase_p.add_argument("user_id")
    erase_p.add_argument("--yes", action="store_true", help="Confirm the irreversible erase.")
    erase_p.add_argument("--revoke", action="store_true", help="Also revoke the user's token.")

    export_p = sub.add_parser("export")
    export_p.add_argument("user_id")

    args = parser.parse_args(argv)
    store = UserStore(args.users_file or USERS_FILE)

    try:
        if args.command == "add":
            user_id = args.user_id if args.user_id is not None else generate_user_id()
            token = store.add_user(user_id, args.display_name)
            if args.json:
                _emit({"user_id": user_id, "display_name": args.display_name, "token": token})
            else:
                print(f"User {user_id!r} added. Token (shown once):\n{token}")
        elif args.command == "bulk-add":
            rows = _read_bulk_rows(args.file, store)
            added = []
            for uid, name in rows:
                uid = uid or generate_user_id()
                added.append(
                    {"user_id": uid, "display_name": name, "token": store.add_user(uid, name)}
                )
            if args.json:
                _emit(added)
            else:
                print(f"{len(added)} users added. Tokens (shown once):")
                for a in added:
                    print(f"{a['user_id']}  {a['display_name']}\n  {a['token']}")
        elif args.command == "rotate":
            token = store.rotate_token(args.user_id)
            if args.json:
                _emit({"user_id": args.user_id, "token": token})
            else:
                print(f"Token rotated for {args.user_id!r}. New token (shown once):\n{token}")
        elif args.command == "revoke":
            store.revoke_user(args.user_id)
            print(f"User {args.user_id!r} revoked.")
        elif args.command == "list":
            for u in store.list_users():
                status = "revoked" if u.get("revoked") else "active"
                print(f"{u['user_id']:20s} {u['display_name']:20s} {status:8s} {u['created_at']}")
        elif args.command == "erase":
            # Before anything else, flags included: "*" is a match-any wildcard
            # in mem0's Qdrant store, so `erase '*'` would erase every customer.
            validate_user_id(args.user_id)
            if not args.yes:
                print(
                    f"Refusing to erase all memories of {args.user_id!r} without --yes "
                    "(this cannot be undone). Nothing was deleted."
                )
                return 2
            if args.revoke and args.user_id not in {u["user_id"] for u in store.list_users()}:
                raise ValueError(f"user_id {args.user_id!r} not found; nothing was erased")
            mem = (memory_factory or _default_memory_factory)()
            # Revoke first: a customer's token must stop working before their
            # data goes, and a failed erase can be re-run (revoke is idempotent).
            if args.revoke:
                store.revoke_user(args.user_id)
            try:
                erased = erase_user(mem, args.user_id, history_db_path=config.history_db)
                remaining = count_user(mem, args.user_id)
            except EraseIncomplete as e:
                # Not "re-run": once the memories are gone from Qdrant, a later
                # erase has no ids to look for and would report success while
                # this text stays in the history database.
                note = " The token is already revoked." if args.revoke else ""
                if args.json:
                    _emit(
                        {
                            "user_id": args.user_id,
                            "error": str(e),
                            "memory_ids": e.memory_ids,
                            "history_rows": e.history_rows,
                            "message_rows": e.message_rows,
                            "revoked": args.revoke,
                        }
                    )
                print(
                    f"erase incomplete for {args.user_id!r}: {e}.{note} Purge these ids by hand: "
                    "see 'Recovering from an incomplete erase' in the README.",
                    file=sys.stderr,
                )
                return 1
            except Exception as e:  # noqa: BLE001 - one-line error, no traceback
                # delete_all failed but every collected id's history was purged
                # and verified (else EraseIncomplete): what is left is still in
                # Qdrant, so a re-run finds it.
                cmd = f"`erase {args.user_id} --yes{' --revoke' if args.revoke else ''}`"
                note = " The token was already revoked;" if args.revoke else ""
                print(
                    f"erase failed for {args.user_id!r}: {e}.{note} re-run {cmd} (safe to repeat).",
                    file=sys.stderr,
                )
                return 1
            result = {"user_id": args.user_id, "erased": erased, "revoked": args.revoke}
            result["remaining"] = remaining
            if args.json:
                _emit(result)
            else:
                msg = f"Erased {erased} memories of {args.user_id!r}."
                print(msg + (" Token revoked." if args.revoke else ""))
            if remaining:
                print(
                    f"{remaining} memories of {args.user_id!r} still remain after erase.",
                    file=sys.stderr,
                )
                return 1
        elif args.command == "export":
            validate_user_id(args.user_id)  # "*" would export every customer
            mem = (memory_factory or _default_memory_factory)()
            items = export_user(mem, args.user_id)
            _emit(items)
            if not items and args.user_id not in {u["user_id"] for u in store.list_users()}:
                print(
                    f"no memories found and {args.user_id!r} is not in the user store; "
                    "check the id",
                    file=sys.stderr,
                )
        return 0
    except (ValueError, ConfigError) as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
