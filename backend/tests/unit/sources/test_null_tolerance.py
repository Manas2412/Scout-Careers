"""Every defaulted upstream field tolerates an explicit JSON null.

These tests exist because a live run failed and the suite did not. Eight of
forty-three sources — Stripe, Figma, Postman, OpenAI, Cohere, ElevenLabs,
Notion, Supabase — returned `schema_error` on `metadata: null` and
`isRemote: null`, while every fixture-backed adapter test passed.

The reason is structural, not careless: a pydantic default applies when a key is
ABSENT, and hand-written fixtures contain the fields their author remembered.
Real APIs send `null` and absent interchangeably, and only real data has the
combination nobody thought of.

So this module does not test a fixture. It enumerates every defaulted field on
every upstream model by reflection and asserts each one accepts null. A new
field with a default is covered the day it is added, without anyone remembering
to extend a list.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from scout_careers.sources.ashby import _AshbyJob, _AshbyPage
from scout_careers.sources.greenhouse import _GreenhouseJob, _GreenhousePage
from scout_careers.sources.lever import _LeverCategories, _LeverPosting

#: The minimum a model needs to validate at all — everything else is defaulted.
REQUIRED: dict[type[BaseModel], dict[str, Any]] = {
    _GreenhouseJob: {"id": 1, "absolute_url": "https://example.test/j/1", "title": "Engineer"},
    _AshbyJob: {"id": "a1", "title": "Engineer", "jobUrl": "https://example.test/j/1"},
    _LeverPosting: {"id": "b2", "text": "Engineer", "hostedUrl": "https://example.test/j/1"},
    _LeverCategories: {},
}


def _defaulted_fields(model: type[BaseModel]) -> list[str]:
    """Field names that carry a default or a default factory."""
    return [
        name
        for name, field in model.model_fields.items()
        if field.default is not PydanticUndefined or field.default_factory is not None
    ]


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        pytest.param(model, field_name, id=f"{model.__name__}.{field_name}")
        for model in REQUIRED
        for field_name in _defaulted_fields(model)
    ],
)
def test_every_defaulted_field_accepts_an_explicit_null(
    model: type[BaseModel], field_name: str
) -> None:
    """A null on a defaulted field is missing data, not a contract breach.

    Refusing a whole board because one recruiter left one boolean unset is the
    wrong trade: the other 870 postings on that board are fine.
    """
    payload = {**REQUIRED[model], field_name: None}
    instance = model.model_validate(payload)

    # It parsed, and the field is not left as None where the type says otherwise.
    value = getattr(instance, field_name)
    annotation = model.model_fields[field_name].annotation
    if annotation is not None and type(None) not in getattr(annotation, "__args__", ()):
        assert value is not None, f"{field_name} coerced to None despite a non-optional type"


# ---------------------------------------------------------------------------
# The exact shapes that failed in production, named so a regression is obvious
# ---------------------------------------------------------------------------


def test_greenhouse_board_with_null_metadata_parses() -> None:
    """Stripe, Figma and Postman: boards with no custom fields send null."""
    page = _GreenhousePage.model_validate(
        {
            "jobs": [
                {
                    "id": 6789012,
                    "absolute_url": "https://boards.greenhouse.io/stripe/jobs/6789012",
                    "title": "Software Engineer, Payments Infrastructure",
                    "content": "&lt;p&gt;Build things.&lt;/p&gt;",
                    "metadata": None,
                    "departments": None,
                    "offices": None,
                    "location": {"name": "Bengaluru, India"},
                }
            ]
        }
    )
    job = page.jobs[0]
    assert job.metadata == []
    assert job.departments == []
    assert job.offices == []


def test_ashby_posting_with_null_is_remote_parses() -> None:
    """OpenAI, Cohere, ElevenLabs, Notion, Supabase: isRemote left unset."""
    page = _AshbyPage.model_validate(
        {
            "jobs": [
                {
                    "id": "b3d9f0c1",
                    "title": "Member of Technical Staff",
                    "jobUrl": "https://jobs.ashbyhq.com/openai/b3d9f0c1",
                    "isRemote": None,
                    "isListed": None,
                    "secondaryLocations": None,
                    "descriptionPlain": "About the team.",
                }
            ]
        }
    )
    job = page.jobs[0]
    assert job.isRemote is False
    # Null must mean "listed", not "hidden": defaulting the other way would
    # silently drop live postings from a board that simply omits the flag.
    assert job.isListed is True
    assert job.secondaryLocations == []


def test_lever_posting_with_null_categories_parses() -> None:
    """Latent, found while fixing the other two: a null categories object."""
    posting = _LeverPosting.model_validate(
        {
            "id": "6f1a2c8e",
            "text": "Senior Software Engineer",
            "hostedUrl": "https://jobs.lever.co/netflix/6f1a2c8e",
            "categories": None,
            "lists": None,
        }
    )
    assert posting.lists == []
    assert posting.categories.allLocations == []
    assert posting.categories.location is None
