"""Turning a resume's skills lines into ``resume_variant.skill_set``.

``skill_set`` is one half of the coverage intersection — the other is
``requirement.normalised_skill`` — so it has to speak the same controlled
vocabulary, and it has to be *derived* rather than typed by hand. A hand-written
list drifts from the resume the moment the resume is edited, and the drift is
invisible: the score simply becomes wrong about what the operator can claim.

So the seed JSON carries no ``skill_set`` at all. It is computed at seed time —
by :func:`variant_skill_set`, from the resume's skills line *and* its per-bullet
tags — which means re-running ``scout-careers seed variants`` after a
``skills.yaml`` edit refreshes it, exactly as ``extract reresolve`` refreshes
the requirement side. Both halves of the intersection stay derived from the
same file.

**Splitting respects brackets.** A resume writes
``AWS (ECS Fargate, RDS, ALB + WAF, IAM / OIDC)`` as one skill. Splitting on
every separator shreds it into fragments that name nothing — and the first
version of this dropped ``aws`` from the backend variant by splitting on the
slash inside ``IAM / OIDC``. Separators only count outside brackets.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from scout_careers.extract.vocabulary import Vocabulary, get_vocabulary

#: Separators between distinct skills, honoured only at bracket depth zero.
_SEPARATORS: frozenset[str] = frozenset({"/", "&", ";", ","})

_OPEN, _CLOSE = "([{", ")]}"

#: Trailing detail a resume adds and a vocabulary does not carry: "Pydantic v2",
#: "Next.js 14", "SQLAlchemy 2.0". Stripped only as a second attempt, after the
#: full phrase has failed, so an alias that deliberately names a version
#: ("java 17", "c++20") still wins on its own terms.
_VERSION = re.compile(r"\s+v?\d+(\.\d+)*$", re.IGNORECASE)


def split_skills(item: str) -> Iterator[str]:
    """Split one skills-line item into candidate skill names.

    Args:
        item: A single item, e.g. ``AWS (ECS Fargate, IAM / OIDC)``.

    Yields:
        The whole item first, then each bracket-depth-zero fragment, then the
        contents of any bracket.

    Whole first, because the most specific reading is the one most likely to be
    a real alias — ``hybrid retrieval (RRF)`` should resolve as itself before
    anything tries ``RRF`` alone.
    """
    yield item

    depth = 0
    current: list[str] = []
    bracketed: list[str] = []
    inner: list[str] = []

    for char in item:
        if char in _OPEN:
            depth += 1
            if depth == 1:
                continue
        elif char in _CLOSE:
            depth -= 1
            if depth == 0:
                bracketed.append("".join(inner).strip())
                inner = []
                continue
        if depth > 0:
            inner.append(char)
            continue
        if char in _SEPARATORS:
            if current:
                yield "".join(current).strip()
                current = []
            continue
        current.append(char)

    if current:
        yield "".join(current).strip()
    for group in bracketed:
        for part in re.split(r"[,/;&]", group):
            if part.strip():
                yield part.strip()


def resolve_item(item: str, vocabulary: Vocabulary) -> set[str]:
    """Return every vocabulary token one skills-line item names.

    Args:
        item: One item from a resume's skills line.
        vocabulary: The controlled vocabulary.

    Returns:
        Tokens, possibly several — ``AWS (Bedrock, RDS)`` legitimately names one
        skill, but ``Backend: FastAPI, Prisma`` splits into items that each name
        their own.

    A version suffix is dropped only after the fuller forms have failed, so a
    deliberately versioned alias is never bypassed by its own stripped form.
    """
    tokens: set[str] = set()
    for candidate in split_skills(item):
        if not candidate:
            continue
        token = vocabulary.resolve(candidate)
        if token is None:
            stripped = _VERSION.sub("", candidate).strip()
            token = vocabulary.resolve(stripped) if stripped != candidate else None
        if token is not None:
            tokens.add(token)
    return tokens


def skill_set_for(
    skills: Sequence[Mapping[str, object]], *, vocabulary: Vocabulary | None = None
) -> tuple[str, ...]:
    """Resolve a resume's skills line. Half of ``skill_set`` — see
    :func:`variant_skill_set` for the whole of it.

    Args:
        skills: The ``content.skills`` groups, each ``{label, items}``.
        vocabulary: Injectable; defaults to the packaged one.

    Returns:
        Sorted, de-duplicated tokens.

    Sorted so the stored array is stable between runs: an unordered set written
    to a ``TEXT[]`` produces a different row every seed, and a diff that changes
    every time is a diff nobody reads.
    """
    vocab = vocabulary or get_vocabulary()
    tokens: set[str] = set()
    for group in skills:
        items = group.get("items") or ()
        if isinstance(items, Iterable) and not isinstance(items, str | bytes):
            for item in items:
                tokens |= resolve_item(str(item), vocab)
    return tuple(sorted(tokens))


def unresolved_items(
    skills: Sequence[Mapping[str, object]], *, vocabulary: Vocabulary | None = None
) -> tuple[str, ...]:
    """Return the items that name no token at all.

    Args:
        skills: The ``content.skills`` groups.
        vocabulary: Injectable.

    Returns:
        The unmatched items, sorted.

    The supply-side half of the proposal queue. A phrase here is something the
    operator can claim and the vocabulary cannot express — so it can never be
    matched against a requirement, however often employers ask for it.
    """
    vocab = vocabulary or get_vocabulary()
    missing: set[str] = set()
    for group in skills:
        items = group.get("items") or ()
        if isinstance(items, Iterable) and not isinstance(items, str | bytes):
            for item in items:
                if not resolve_item(str(item), vocab):
                    missing.add(str(item))
    return tuple(sorted(missing))


# ---------------------------------------------------------------------------
# Bullets
# ---------------------------------------------------------------------------
#
# ``skill_set`` says what the operator can claim. It does not say *which
# sentence proves it*, and a generated document needs that: the resume line the
# score rests on is the line that has to appear. So each bullet carries its own
# tags — ``skills`` (the same controlled vocabulary) and ``claim_keys`` (rows in
# the ledger).
#
# These are hand-written rather than derived, and deliberately so. Deriving
# them would mean resolving free prose against the vocabulary, which is the
# job the LLM does on the demand side and does imperfectly; a wrong tag here
# would not merely mis-rank a posting, it would put an unbacked number into a
# document sent to an employer. The tags are small, they change only when the
# resume changes, and a person can check them. What is machine-checked is that
# every tag *resolves* — an unknown token or an unapproved claim key is refused
# at seed time, before anything reaches the table.
#
# **One deviation from MATCH_SCORING.md §4.1.** Its sketch shows a bullet
# carrying `claim_ids: [38, 39, 40]`; these carry `claim_keys:
# ['pqbot.tests', ...]`. `claim.id` is `Identity(always=True)`, so those
# integers differ between the operator's machine and any other, which makes
# them unusable in a hand-edited file that is read and diffed by a person.
# CLAIMS_LEDGER.md §2.1 settles it in the same direction — "Keys are stable and
# referenced from `resume_variant.content` bullets" — so the key is what is
# stored and scoring resolves it through `claim.key`, which is UNIQUE.
#
# The union in `variant_skill_set` is §4.1's own rule ("`skill_set` is the
# flattened union of every `skills` array plus every `skill_lines[].tokens`
# entry"), which the first implementation did not follow.


@dataclass(frozen=True, slots=True)
class Bullet:
    """One tagged resume line.

    ``id`` is what a ``claim_usage`` row records as its ``location``, so a
    generated document can be walked back to the bullet that authorised each
    number in it.
    """

    id: str
    text: str
    skills: tuple[str, ...]
    claim_keys: tuple[str, ...]


def _bullets_of(container: Mapping[str, Any], key: str = "bullets") -> Iterator[Mapping[str, Any]]:
    entries = container.get(key) or ()
    if isinstance(entries, Iterable) and not isinstance(entries, str | bytes):
        for entry in entries:
            if isinstance(entry, Mapping):
                yield entry


def _strings(entry: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = entry.get(key) or ()
    if isinstance(value, str) or not isinstance(value, Iterable):
        return ()
    return tuple(str(item) for item in value)


def iter_bullets(content: Mapping[str, Any]) -> Iterator[Bullet]:
    """Walk every bullet in a variant's ``content``, experience then projects.

    Args:
        content: A variant's ``content`` object.

    Yields:
        One :class:`Bullet` per line, in document order.

    Order matters: it is the order the bullets appear in the rendered resume,
    which is the order a reader meets the evidence in.
    """
    for role in content.get("experience") or []:
        if not isinstance(role, Mapping):
            continue
        for block in role.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            for entry in _bullets_of(block):
                yield Bullet(
                    id=str(entry.get("id", "")),
                    text=str(entry.get("text", "")),
                    skills=_strings(entry, "skills"),
                    claim_keys=_strings(entry, "claim_keys"),
                )
    for project in content.get("projects") or []:
        if not isinstance(project, Mapping):
            continue
        for entry in _bullets_of(project):
            yield Bullet(
                id=str(entry.get("id", "")),
                text=str(entry.get("text", "")),
                skills=_strings(entry, "skills"),
                claim_keys=_strings(entry, "claim_keys"),
            )


def bullet_tag_problems(
    content: Mapping[str, Any],
    *,
    approved_claims: Collection[str],
    vocabulary: Vocabulary | None = None,
) -> tuple[str, ...]:
    """Every reason this variant's bullet tags cannot be trusted.

    Args:
        content: A variant's ``content`` object.
        approved_claims: Claim keys the operator has approved. A key outside
            this set is refused whether it is unknown or merely rejected —
            the two are the same failure downstream, and distinguishing them
            in the message is what the message is for.
        vocabulary: Injectable; defaults to the packaged one.

    Returns:
        Human-readable problems, sorted. Empty means the tags resolve.

    All of them, not the first: a seed file is edited by hand, and a loader
    that stops at the first bad line turns one review pass into six.
    """
    vocab = vocabulary or get_vocabulary()
    approved = set(approved_claims)
    problems: list[str] = []

    for bullet in iter_bullets(content):
        for token in bullet.skills:
            if token not in vocab.skills:
                problems.append(f"{bullet.id}: skill {token!r} is not in {vocab.version}")
        for key in bullet.claim_keys:
            if key not in approved:
                problems.append(f"{bullet.id}: claim {key!r} is not an approved claim")

    return tuple(sorted(problems))


def apply_bullet_tags(
    content: Mapping[str, Any], tags: Mapping[str, Mapping[str, Sequence[str]]]
) -> dict[str, Any]:
    """Merge hand-written bullet tags into a copy of ``content``.

    Args:
        content: A variant's ``content``, as transcribed from the ``.docx``.
        tags: Bullet ID to ``{skills, claims}``.

    Returns:
        A new content object whose bullets carry ``skills`` and ``claim_keys``.

    The tags live in one file and the bullet text in another, and they are
    joined here rather than written into the JSON. ``variants-from-docx.py``
    overwrites those JSON files wholesale every time the resume changes; tags
    stored there would be silently destroyed by an ordinary resume edit, and
    nothing downstream would notice — the scores would just quietly stop
    resting on anything.
    """
    merged = deepcopy(dict(content))

    def tag(entry: dict[str, Any]) -> None:
        found = tags.get(str(entry.get("id", "")))
        if found is None:
            return
        entry["skills"] = [str(item) for item in found.get("skills", ())]
        entry["claim_keys"] = [str(item) for item in found.get("claims", ())]

    for role in merged.get("experience") or []:
        for block in role.get("blocks") or []:
            for entry in block.get("bullets") or []:
                tag(entry)
    for project in merged.get("projects") or []:
        for entry in project.get("bullets") or []:
            tag(entry)
    return merged


def variant_skill_set(
    content: Mapping[str, Any], *, vocabulary: Vocabulary | None = None
) -> tuple[str, ...]:
    """The tokens a variant can be matched on: its skills line **and** its bullets.

    Args:
        content: A variant's ``content``, tags already merged.
        vocabulary: Injectable.

    Returns:
        Sorted, de-duplicated tokens.

    The union, not the skills line alone. A resume's skills line lists tools —
    ``FastAPI``, ``Redis``, ``pgvector``. It does not list ``testing``,
    ``ownership``, ``security_practice`` or ``system_design``, because no one
    writes "Testing" under Skills; they write "312 backend tests green, SAST
    closed at zero critical findings" and expect the reader to draw the
    conclusion. Employers ask for exactly those, constantly.

    Deriving ``skill_set`` from the skills line alone scored all of them
    MISSING across all six variants — 8 to 11 tokens per variant — while a
    hand-verified bullet sat in the same document proving each one. The bullet
    tags are checked against the same closed vocabulary and are read by a person
    before they ship, so they are at least as trustworthy a source as the alias
    resolution that produces the other half; and they carry the proof sentence
    with them, which the skills line does not.
    """
    vocab = vocabulary or get_vocabulary()
    tokens = set(skill_set_for(content.get("skills") or [], vocabulary=vocab))
    for bullet in iter_bullets(content):
        tokens |= set(bullet.skills)
    return tuple(sorted(tokens))


def unproven_skills(
    content: Mapping[str, Any], *, vocabulary: Vocabulary | None = None
) -> tuple[str, ...]:
    """Tokens the skills line claims that no bullet demonstrates.

    Not an error — the skills line is itself the operator's claim, and some
    tokens honestly have no bullet behind them. It matters because such a token
    can score a requirement MET while leaving document generation with no
    sentence to cite for it: the score would be defensible and the resume it
    produced would not visibly support it.
    """
    vocab = vocabulary or get_vocabulary()
    declared = set(skill_set_for(content.get("skills") or [], vocabulary=vocab))
    proven = {token for bullet in iter_bullets(content) for token in bullet.skills}
    return tuple(sorted(declared - proven))


def bullet_ids(content: Mapping[str, Any]) -> frozenset[str]:
    """Every bullet ID in a variant, for detecting tags that name nothing."""
    return frozenset(bullet.id for bullet in iter_bullets(content))


def untagged_bullets(content: Mapping[str, Any]) -> tuple[str, ...]:
    """Bullet IDs carrying no ``skills`` at all.

    Not an error. A bullet with no skill tags simply cannot be selected as
    proof of any requirement — it is inert rather than wrong, and the summary
    line says how many there are so the operator can decide whether that is
    what they meant.
    """
    return tuple(bullet.id for bullet in iter_bullets(content) if not bullet.skills)


__all__ = [
    "Bullet",
    "apply_bullet_tags",
    "bullet_ids",
    "bullet_tag_problems",
    "iter_bullets",
    "resolve_item",
    "skill_set_for",
    "split_skills",
    "unproven_skills",
    "unresolved_items",
    "untagged_bullets",
    "variant_skill_set",
]
