"""Fixtures for the skill tests."""

from __future__ import annotations

import pytest

from lightrag.fault_tree.domain_validation import load_domain_bundle
from mcp_server.repository import DomainRepository
from mcp_server.service import DomainService
from tests.mcp.helpers import DEMO_BUNDLE, PACK_DIR


@pytest.fixture(scope="session")
def demo_service() -> DomainService:
    bundle = load_domain_bundle(DEMO_BUNDLE)
    repository = DomainRepository(PACK_DIR, [bundle])
    return DomainService(repository)
