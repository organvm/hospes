"""Clock and identifier policy for live operation and synthetic fixtures.

Normal operation uses the live UTC clock and UUID4 identities. Synthetic
fixture construction installs a context-local frozen clock and purpose-scoped,
ordinal UUID5 identities so equal generations produce equal bytes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5


@dataclass
class _DeterministicScope:
    generated_at: datetime
    generation_id: str
    ordinals: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def next_id(self, purpose: str) -> str:
        normalized = purpose.strip()
        if not normalized:
            raise ValueError("identity purpose cannot be empty")
        self.ordinals[normalized] += 1
        return str(
            uuid5(
                NAMESPACE_URL,
                "hospes:synthetic:"
                f"{self.generation_id}:{normalized}:{self.ordinals[normalized]}",
            )
        )


_SCOPE: ContextVar[_DeterministicScope | None] = ContextVar(
    "hospes_generation_scope", default=None
)


def now() -> datetime:
    """Return frozen fixture time or the live production UTC clock."""
    scope = _SCOPE.get()
    return scope.generated_at if scope is not None else datetime.now(UTC)


def new_id(purpose: str) -> str:
    """Return purpose-ordinal UUID5 in fixtures and UUID4 in production."""
    scope = _SCOPE.get()
    return scope.next_id(purpose) if scope is not None else str(uuid4())


def deterministic_generation_id(generated_at: datetime, fixture_version: int) -> str:
    """Derive one cohesive identifier from the fixture version and frozen time."""
    aware = generated_at.astimezone(UTC)
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:synthetic-demo:v{fixture_version}:{aware.isoformat()}",
        )
    )


@contextmanager
def deterministic_scope(
    generated_at: datetime, generation_id: str
) -> Iterator[None]:
    """Freeze time and identity generation for one synthetic bundle build."""
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("deterministic generation time must include a timezone")
    token = _SCOPE.set(
        _DeterministicScope(generated_at.astimezone(UTC), generation_id)
    )
    try:
        yield
    finally:
        _SCOPE.reset(token)


__all__ = [
    "deterministic_generation_id",
    "deterministic_scope",
    "new_id",
    "now",
]
