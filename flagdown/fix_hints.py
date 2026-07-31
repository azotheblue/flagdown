#!/usr/bin/env python3
"""
Recalculate hint costs and enforce sequential unlock on existing CTFd challenges.

Examples:
    python fix_hint_costs.py --dry-run
    python fix_hint_costs.py --category 1intel-malware
"""

import argparse
import sys

import requests
from dotenv import load_dotenv

from .core import (
    CTFD_URL,
    CTFD_TOKEN,
    CTFdClient,
    compute_hint_costs,
    hint_prerequisites,
    hint_prerequisites_from_hint,
)

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Fix hint costs and sequential unlock on CTFd challenges"
    )
    parser.add_argument("--category", help="Only challenges whose category contains this string")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without patching")
    args = parser.parse_args()

    if not CTFD_TOKEN:
        print("[ERROR] CTFD_TOKEN not set in .env")
        return 1

    client = CTFdClient(CTFD_URL, CTFD_TOKEN)
    if not client.test_connection():
        return 1

    challenges = client.get_challenges(use_cache=False)
    updated = 0
    skipped = 0
    errors = 0

    for ch in challenges:
        cid = ch.get("id")
        name = ch.get("name", "")
        category = ch.get("category", "")
        value = ch.get("value", 0)

        if args.category and args.category.lower() not in category.lower():
            continue

        try:
            hints = client.get_hints(cid)
        except requests.RequestException as e:
            print(f"  [ERROR] {name} (id={cid}): could not list hints: {e}")
            errors += 1
            continue

        if not hints:
            continue

        hints = sorted(hints, key=lambda h: h.get("id", 0))
        hint_ids = [h["id"] for h in hints]
        new_costs = compute_hint_costs(value, len(hints))
        old_costs = [h.get("cost", 0) for h in hints]
        new_prereqs = [hint_prerequisites(hint_ids, i) for i in range(len(hints))]
        old_prereqs = [hint_prerequisites_from_hint(h) for h in hints]

        costs_changed = old_costs != new_costs
        prereqs_changed = old_prereqs != new_prereqs
        if not costs_changed and not prereqs_changed:
            skipped += 1
            continue

        changes = []
        if costs_changed:
            changes.append(f"costs {old_costs} -> {new_costs}")
        if prereqs_changed:
            changes.append("sequential unlock enforced")
        print(
            f"  {'[DRY-RUN]' if args.dry_run else '[OK]'} {name} ({value} pts): "
            + ", ".join(changes)
        )

        if args.dry_run:
            updated += 1
            continue

        try:
            for hint, cost, prereqs in zip(hints, new_costs, new_prereqs):
                client.update_hint(hint["id"], cost=cost, prerequisite_ids=prereqs)
            updated += 1
        except requests.RequestException as e:
            print(f"  [ERROR] {name} (id={cid}): {e}")
            errors += 1

    print(f"\nUpdated: {updated}, skipped: {skipped}, errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
