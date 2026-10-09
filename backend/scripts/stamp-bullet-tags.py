#!/usr/bin/env python3
"""Write each bullet tag's ``text`` fingerprint from the current seed JSON.

Run this **after** a deliberate resume edit, and read what it prints.

Bullet IDs are positional — ``{variant}.exp.{role}.{block}.{n}`` — so editing a
resume renumbers them. ``seed variants`` already refused a tag whose ID named no
bullet, but a tag whose ID named a *different* bullet passed silently: the
position still existed, the skills were real tokens, the claims were approved,
and nothing downstream could tell that a claim had moved onto an unrelated line.

The ``text`` field closes that. It holds the first
:data:`~scout_careers.cli.seed.TAG_FINGERPRINT_CHARS` characters of the bullet
the tag was written against, and the seed refuses a tag whose line no longer
starts with it.

This script is the only thing that writes that field, and it is deliberately NOT
part of ``seed variants``. Restamping is how a *correct* move is accepted, so it
has to be an act the operator chooses, with the moves in front of them:

    MOVED is the one to read. Every MOVED line is a tag that is now attached to
    different content. Some are a rewording of the same claim and fine to
    accept; some are a claim that has landed on someone else's sentence. The
    script cannot tell them apart. You can.

It edits the YAML in place by line, not by round-tripping through a parser,
because the file's header comment is the explanation of why the ledger exists
and PyYAML would discard it.

Usage:
    python scripts/stamp-bullet-tags.py                 # show what would change
    python scripts/stamp-bullet-tags.py --write         # apply it
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAGS = ROOT / "seeds" / "bullet_tags.yaml"
VARIANTS = ROOT / "seeds" / "variants"
FINGERPRINT_CHARS = 40

#: `  some_variant.exp.0.1.2:` at the top level of the `bullets:` mapping.
ID_LINE = re.compile(r"^  ([a-z_]+\.(?:exp|proj)\.[0-9.]+):\s*$")
TEXT_LINE = re.compile(r"^    text:\s")


def bullet_texts(directory: Path) -> dict[str, str]:
    """Bullet ID to full text, across every variant."""
    found: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))["content"]
        for role in content.get("experience") or []:
            for block in role.get("blocks") or []:
                for bullet in block.get("bullets") or []:
                    found[str(bullet["id"])] = str(bullet["text"])
        for project in content.get("projects") or []:
            for bullet in project.get("bullets") or []:
                found[str(bullet["id"])] = str(bullet["text"])
    return found


def quote(value: str) -> str:
    """A double-quoted YAML scalar. Bullets contain colons, em dashes and %."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Apply the changes.")
    args = parser.parse_args()

    if not TAGS.exists():
        print(f"  no tag file at {TAGS}")
        return 1

    texts = bullet_texts(VARIANTS)
    lines = TAGS.read_text(encoding="utf-8").splitlines(keepends=True)

    out: list[str] = []
    added: list[str] = []
    moved: list[tuple[str, str, str]] = []
    orphaned: list[str] = []
    unchanged = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        out.append(line)
        index += 1

        match = ID_LINE.match(line)
        if match is None:
            continue

        key = match.group(1)
        # Consume this tag's body, dropping any existing `text:` line so the
        # new one is written in a single known place.
        body: list[str] = []
        old_stamp: str | None = None
        while index < len(lines) and not ID_LINE.match(lines[index]) and lines[index].startswith(" "):
            if TEXT_LINE.match(lines[index]):
                old_stamp = lines[index].split(":", 1)[1].strip().strip('"')
            else:
                body.append(lines[index])
            index += 1

        if key not in texts:
            orphaned.append(key)
            out.extend(body)
            continue

        stamp = texts[key][:FINGERPRINT_CHARS]
        out.append(f"    text: {quote(stamp)}\n")
        out.extend(body)

        if old_stamp is None:
            added.append(key)
        elif old_stamp != stamp:
            moved.append((key, old_stamp, stamp))
        else:
            unchanged += 1

    print()
    print(f"  {unchanged} unchanged   {len(added)} newly stamped   {len(moved)} MOVED   "
          f"{len(orphaned)} naming no bullet")

    if moved:
        print()
        print("  MOVED — each of these tags is now on different content. Read every one:")
        for key, was, now in moved:
            print(f"    {key}")
            print(f"      was: {was}")
            print(f"      now: {now}")

    if orphaned:
        print()
        print("  These tags name no bullet and were left alone; `seed variants` will refuse them:")
        for key in orphaned:
            print(f"    {key}")

    if not args.write:
        print()
        print("  Nothing written. Re-run with --write once the MOVED list is right.")
        return 0

    TAGS.write_text("".join(out), encoding="utf-8")
    print()
    print(f"  Written to {TAGS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
