"""The adapter registry and its boot assertion.

``SourceAdapter`` is a Protocol rather than an ABC (SOURCE_ADAPTERS.md §2.1), so
the registry is the only place that maps an ``ats_type`` value to a concrete
class. Registration is lazy: importing every adapter module at package import
would make ``common/`` → ``sources/`` import order matter, and a Phase 1 build
does not have all twelve modules to import.

The completeness assertion is deliberately scoped to ``PHASE_1_ADAPTERS`` rather
than to the whole ``AtsType`` enum. The enum ships complete in migration 0001
because Postgres cannot remove an enum value; the adapters arrive one phase at a
time. Asserting over the enum would make the partial roster a boot failure,
which is the opposite of what the assertion is for.
"""

from __future__ import annotations

import importlib
from typing import Final, cast

from scout_careers.common.errors import AdapterConfigError
from scout_careers.common.types import AtsType
from scout_careers.sources.base import SourceAdapter

#: What Phase 1 is expected to ship. Grows one entry per adapter, alongside the
#: adapter module and its six required tests.
PHASE_1_ADAPTERS: Final[frozenset[AtsType]] = frozenset(
    {
        AtsType.GREENHOUSE,
        AtsType.LEVER,
        AtsType.ASHBY,
        AtsType.MAIL_ALERT,
    }
)

#: ``manual`` has no adapter class by design: a hand-entered posting arrives
#: through ``POST /postings/import`` and exists in the enum only so that it has a
#: ``source`` row and therefore a stable ``(source_id, external_id)`` identity.
NO_ADAPTER_BY_DESIGN: Final[frozenset[AtsType]] = frozenset({AtsType.MANUAL})

#: Module path and exported class name per adapter. Resolved on demand.
ADAPTER_MODULES: Final[dict[AtsType, tuple[str, str]]] = {
    AtsType.GREENHOUSE: ("scout_careers.sources.greenhouse", "GreenhouseAdapter"),
    AtsType.LEVER: ("scout_careers.sources.lever", "LeverAdapter"),
    AtsType.ASHBY: ("scout_careers.sources.ashby", "AshbyAdapter"),
    AtsType.WORKDAY: ("scout_careers.sources.workday", "WorkdayAdapter"),
    AtsType.SMARTRECRUITERS: (
        "scout_careers.sources.smartrecruiters",
        "SmartRecruitersAdapter",
    ),
    AtsType.WORKABLE: ("scout_careers.sources.workable", "WorkableAdapter"),
    AtsType.RECRUITEE: ("scout_careers.sources.recruitee", "RecruiteeAdapter"),
    AtsType.GOOGLE: ("scout_careers.sources.google", "GoogleCareersAdapter"),
    AtsType.AMAZON: ("scout_careers.sources.amazon", "AmazonJobsAdapter"),
    AtsType.MICROSOFT: ("scout_careers.sources.microsoft", "MicrosoftCareersAdapter"),
    AtsType.MAIL_ALERT: ("scout_careers.sources.mail_alerts", "MailAlertAdapter"),
}

#: Populated by :func:`load_adapters`. Never mutated by anything else.
ADAPTERS: dict[AtsType, type[SourceAdapter]] = {}


def _class_name(candidate: object) -> str:
    """Return a class's name for an error message, whatever was handed in."""
    name = getattr(candidate, "__name__", None)
    return str(name) if isinstance(name, str) else repr(candidate)


def register(adapter_cls: object) -> type[SourceAdapter]:
    """Register a concrete adapter class under its ``name``.

    Args:
        adapter_cls: The adapter class. Its ``name`` classvar is the key. Typed
            as ``object`` because the whole point of the call is to prove
            structurally that it satisfies the protocol — declaring the
            parameter as ``type[SourceAdapter]`` would make the check that
            catches a half-written adapter statically unreachable, which is the
            one thing this function exists to do.

    Returns:
        The same class, narrowed, so this is usable as a decorator.

    Raises:
        AdapterConfigError: When the class does not satisfy ``SourceAdapter``,
            or a different class is already registered for that type.
    """
    if not isinstance(adapter_cls, SourceAdapter):
        raise AdapterConfigError(
            f"{_class_name(adapter_cls)} does not satisfy the SourceAdapter protocol"
        )
    checked = cast("type[SourceAdapter]", adapter_cls)
    existing = ADAPTERS.get(checked.name)
    if existing is not None and existing is not checked:
        raise AdapterConfigError(
            f"two adapters registered for {checked.name.value}: "
            f"{existing.__name__} and {checked.__name__}"
        )
    ADAPTERS[checked.name] = checked
    return checked


def load_adapters(
    wanted: frozenset[AtsType] = PHASE_1_ADAPTERS,
) -> dict[AtsType, type[SourceAdapter]]:
    """Import and register the adapter modules for ``wanted``.

    Args:
        wanted: The adapter types to resolve.

    Returns:
        ``ADAPTERS``, after registration.

    Raises:
        AdapterConfigError: When a module or its class is missing.
    """
    for ats in sorted(wanted, key=lambda value: value.value):
        if ats in ADAPTERS:
            continue
        module_path, class_name = ADAPTER_MODULES[ats]
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            raise AdapterConfigError(
                f"no adapter registered for {ats.value}: {module_path} does not exist"
            ) from exc
        adapter_cls = getattr(module, class_name, None)
        if adapter_cls is None:
            raise AdapterConfigError(
                f"no adapter registered for {ats.value}: {module_path} defines no {class_name}"
            )
        register(adapter_cls)
    return ADAPTERS


def get_adapter(ats: AtsType) -> type[SourceAdapter]:
    """Return the adapter class for an ``ats_type`` value.

    Args:
        ats: The adapter type from ``source.adapter``.

    Returns:
        The registered class.

    Raises:
        AdapterConfigError: When nothing is registered for that type.
    """
    if ats in NO_ADAPTER_BY_DESIGN:
        raise AdapterConfigError(f"{ats.value} has no adapter class by design")
    load_adapters(frozenset({ats}))
    return ADAPTERS[ats]


def verify_registry(wanted: frozenset[AtsType] = PHASE_1_ADAPTERS) -> None:
    """Boot assertion: every expected adapter exists and satisfies the protocol.

    A missing adapter is a boot failure, not a runtime 500.

    Args:
        wanted: The adapter types this build is expected to ship.

    Raises:
        AdapterConfigError: When one is missing or does not satisfy the protocol.
    """
    load_adapters(wanted)
    missing = sorted(ats.value for ats in wanted if ats not in ADAPTERS)
    if missing:
        raise AdapterConfigError(f"no adapter registered for {', '.join(missing)}")
    for ats in wanted:
        # Deliberately re-checked here rather than trusted from register(): a
        # module that mutates ADAPTERS directly must still fail at boot.
        candidate: object = ADAPTERS[ats]
        if not isinstance(candidate, SourceAdapter):
            raise AdapterConfigError(
                f"{_class_name(candidate)} does not satisfy the SourceAdapter protocol"
            )
        adapter_cls = cast("type[SourceAdapter]", candidate)
        if adapter_cls.name is not ats:
            raise AdapterConfigError(
                f"{_class_name(adapter_cls)} is registered as {ats.value} "
                f"but declares name={adapter_cls.name.value}"
            )


__all__ = [
    "ADAPTERS",
    "ADAPTER_MODULES",
    "NO_ADAPTER_BY_DESIGN",
    "PHASE_1_ADAPTERS",
    "get_adapter",
    "load_adapters",
    "register",
    "verify_registry",
]
