#!/usr/bin/env python3
"""Convert the six resume ``.docx`` files into reviewable seed JSON.

Run once, and again whenever a resume is edited. The JSON it writes — not the
``.docx`` — is what ``scout-careers seed variants`` loads.

**Why an intermediate file rather than parsing ``.docx`` at seed time.** A resume
is the operator's own document, edited in Word, and a parser that runs at seed
time makes every scoring result depend on a binary nobody can diff. The JSON is
reviewable in a pull request, and ``resume_variant.source_path`` records which
document it came from — so a bullet that turns up in a generated application can
be traced back to the file the operator actually wrote.

**Nothing here is invented.** Every bullet, skill and date is copied verbatim.
This is the input side of the claims ledger: a résumé line that overstates is a
line the ledger will later be asked to vouch for.

Structure, read off the documents rather than assumed:

- Bullets are ``List Paragraph`` runs carrying Word numbering. Everything else at
  the same level is a section heading, a role header, a block title or a context
  line, distinguished by position rather than by formatting — bold is on almost
  every paragraph in these files and says nothing.
- A role header carries ``Role — Employer<TAB>Location  |  Dates``.
- Section names differ between variants: the consulting resume says ``CORE
  COMPETENCIES`` and ``SELECTED ENGAGEMENTS`` where the others say ``TECHNICAL
  SKILLS`` and ``EXPERIENCE``. Both spellings map to one key, because the scorer
  should not care which document it is reading.

Usage:  python scripts/variants-from-docx.py <resume-dir> [--out seeds/variants]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from docx import Document
    from docx.text.paragraph import Paragraph
except ImportError:  # pragma: no cover - dev tooling only
    sys.exit("python-docx is required: pip install python-docx")

#: Variant key -> (filename, display name, who it is aimed at).
#:
#: The six of DATA_MODEL.md §5.1. `target` is what the operator is telling the
#: scorer about intent; it is never shown to a model and never scored.
VARIANTS: dict[str, tuple[str, str, str]] = {
    "ai_product": (
        "Manas_Sisodia_AI_Product.docx",
        "AI Product Engineer",
        "Product-facing AI roles at startups shipping user-visible LLM features.",
    ),
    "ai_enterprise": (
        "Manas_Sisodia_AI_Enterprise.docx",
        "AI Enterprise Engineer",
        "Forward-deployed and enterprise AI roles: deployment, integration, customers.",
    ),
    "ai_platform": (
        "Manas_Sisodia_AI_Platform.docx",
        "AI Platform Engineer",
        "Infrastructure beneath AI products — serving, retrieval, evaluation, cost.",
    ),
    "backend": (
        "Manas_Sisodia_Backend.docx",
        "Backend Engineer",
        "Backend and distributed-systems roles with no AI framing required.",
    ),
    "combined": (
        "Manas_Sisodia_Combined.docx",
        "Backend + AI Engineer",
        "Roles asking for both, where narrowing to one half would lose the match.",
    ),
    "consulting": (
        "Manas_Sisodia_Consulting.docx",
        "Technology Consultant",
        "Consulting and solution-engineering roles where delivery outranks stack.",
    ),
}

#: Heading spellings that mean the same section. Left side is what a document
#: says; right side is what the scorer reads.
SECTION_ALIASES: dict[str, str] = {
    "SUMMARY": "summary",
    "PROFILE": "summary",
    "TECHNICAL SKILLS": "skills",
    "SKILLS": "skills",
    "CORE SKILLS": "skills",
    "CORE COMPETENCIES": "skills",
    "EXPERIENCE": "experience",
    "SELECTED ENGAGEMENTS": "experience",
    "PROFESSIONAL EXPERIENCE": "experience",
    "PROJECTS": "projects",
    "SELECTED PROJECTS": "projects",
    "EDUCATION": "education",
    "ACHIEVEMENTS": "achievements",
    "AWARDS": "achievements",
}

#: A skills line reads ``Label: item, item (detail), item``. Splitting on commas
#: outside brackets keeps "AWS (ECS Fargate, RDS)" as one item rather than three
#: fragments that name nothing.
_SKILL_SPLIT = re.compile(r",\s*(?![^(]*\))")


def is_bullet(paragraph: Paragraph) -> bool:
    """Whether Word is numbering this paragraph, i.e. it renders as a bullet."""
    properties = paragraph._p.pPr
    return properties is not None and properties.numPr is not None


def split_columns(text: str) -> tuple[str, str]:
    """Split a header on its tab into ``(title, right-hand column)``."""
    if "\t" in text:
        left, _, right = text.partition("\t")
        return left.strip(), right.strip()
    return text.strip(), ""


def parse_skill_line(line: str) -> dict[str, Any]:
    """Turn ``Languages: Python, TypeScript`` into a labelled group."""
    label, separator, rest = line.partition(":")
    if not separator:
        return {
            "label": "",
            "items": [item.strip() for item in _SKILL_SPLIT.split(line) if item.strip()],
        }
    return {
        "label": label.strip(),
        "items": [item.strip() for item in _SKILL_SPLIT.split(rest) if item.strip()],
    }


def parse(path: Path, key: str) -> dict[str, Any]:
    """Read one resume into the ``content`` document.

    Args:
        path: The ``.docx``.
        key: The variant key, used to build bullet IDs.

    Returns:
        The structured content.

    Bullet IDs are positional and stable — ``backend.exp.2.1`` is the second
    bullet of the third experience block. They have to be stable because
    ``match_score.evidence`` cites them: an ID that moved would turn a recorded
    coverage decision into a pointer at the wrong sentence.
    """
    document = Document(str(path))
    lines = [
        (paragraph.text.strip(), is_bullet(paragraph))
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    ]

    content: dict[str, Any] = {
        "header": {},
        "summary": "",
        "skills": [],
        "experience": [],
        "projects": [],
        "education": [],
        "achievements": [],
    }

    section: str | None = None
    role: dict[str, Any] | None = None
    block: dict[str, Any] | None = None
    project: dict[str, Any] | None = None

    for index, (text, bullet) in enumerate(lines):
        if index == 0:
            content["header"]["name"] = text
            continue
        if index == 1 and section is None:
            content["header"]["contact"] = text
            continue

        mapped = SECTION_ALIASES.get(text.upper())
        if mapped and not bullet:
            section = mapped
            role = block = project = None
            continue

        if section == "summary":
            content["summary"] = f"{content['summary']} {text}".strip()

        elif section == "skills":
            content["skills"].append(parse_skill_line(text))

        elif section == "experience":
            if bullet:
                if block is None:
                    # A bullet directly under a role, with no block title of its
                    # own. Give it one rather than dropping the bullet.
                    block = {"title": "", "bullets": []}
                    if role is not None:
                        role["blocks"].append(block)
                if block is not None:
                    block["bullets"].append(
                        {
                            "id": f"{key}.exp.{len(content['experience']) - 1}."
                            f"{len(role['blocks']) - 1}.{len(block['bullets'])}"
                            if role
                            else f"{key}.exp.orphan.{len(block['bullets'])}",
                            "text": text,
                        }
                    )
            elif " — " in text and ("|" in text or "\t" in text):
                title, right = split_columns(text)
                heading, _, employer = title.partition(" — ")
                location, _, dates = right.partition("|")
                role = {
                    "role": heading.strip(),
                    "employer": employer.strip(),
                    "location": location.strip(),
                    "dates": dates.strip(),
                    "context": "",
                    "blocks": [],
                }
                content["experience"].append(role)
                block = None
            elif role is not None and not role["blocks"] and not role["context"]:
                role["context"] = text
            else:
                block = {"title": text, "bullets": []}
                if role is not None:
                    role["blocks"].append(block)

        elif section == "projects":
            if bullet and project is not None:
                project["bullets"].append(
                    {
                        "id": f"{key}.proj.{len(content['projects']) - 1}.{len(project['bullets'])}",
                        "text": text,
                    }
                )
            elif not bullet:
                project = {"title": text, "bullets": []}
                content["projects"].append(project)

        elif section == "education":
            title, right = split_columns(text)
            content["education"].append({"qualification": title, "dates": right})

        elif section == "achievements":
            content["achievements"].append(text)

    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resume_dir", type=Path, help="Directory holding the six .docx files.")
    parser.add_argument("--out", type=Path, default=Path("seeds/variants"))
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []

    for key, (filename, name, target) in VARIANTS.items():
        path = args.resume_dir / filename
        if not path.is_file():
            missing.append(filename)
            continue
        content = parse(path, key)
        bullets = sum(
            len(block["bullets"]) for role in content["experience"] for block in role["blocks"]
        ) + sum(len(project["bullets"]) for project in content["projects"])
        payload = {
            "key": key,
            "name": name,
            "target": target,
            "source_path": str(path),
            "content": content,
            # Deliberately empty. `skill_set` holds controlled-vocabulary tokens,
            # and mapping the skills lines onto them is a reviewed decision, not
            # a parse — see `scripts/variant-skills.py`.
            "skill_set": [],
        }
        out = args.out / f"{key}.json"
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            f"  {key:<14} {len(content['experience'])} role(s), {bullets} bullet(s), "
            f"{sum(len(g['items']) for g in content['skills'])} skill item(s) -> {out}"
        )

    if missing:
        print(f"\n  missing: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
