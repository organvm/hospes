"""Shared test fixtures for the HOSPES engine tests."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pytest

from hospes import authentication, pipeline, service
from hospes.paths import PIPELINE_FIXTURE

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
REPLIES = FIXTURES / "replies"


@pytest.fixture()
def fixture_rows():
    """Candidate rows loaded from the pipeline fixture."""
    return pipeline.load_candidates(PIPELINE_FIXTURE)


@pytest.fixture()
def tmp_out(tmp_path):
    """A throwaway out/ directory for tests that write artifacts."""
    d = tmp_path / "out"
    (d / "drafts").mkdir(parents=True)
    (d / "briefs").mkdir(parents=True)
    (d / "assets").mkdir(parents=True)
    return d


def reply_text(name: str) -> str:
    return (REPLIES / f"{name}.txt").read_text(encoding="utf-8")


def synthetic_bearer_authenticator(
    identities: Mapping[str, tuple[str, str, str]],
) -> authentication.StaticBearerAuthenticator:
    """Build token-bound synthetic API identities without trusting headers."""
    return authentication.StaticBearerAuthenticator(
        {
            bearer_value: service.HumanActor(
                actor_id=actor_id,
                role=service.HumanRole(role),
                tenant_id=tenant_id,
            )
            for bearer_value, (actor_id, role, tenant_id) in identities.items()
        }
    )
