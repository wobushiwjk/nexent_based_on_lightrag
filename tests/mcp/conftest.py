"""Fixtures for the read-only MCP service tests."""

from __future__ import annotations

import pytest

from lightrag.fault_tree.contracts import DomainBundle
from lightrag.fault_tree.domain_validation import load_domain_bundle
from mcp_server.repository import DomainRepository
from mcp_server.service import DomainService
from tests.mcp.helpers import DEMO_BUNDLE, PACK_DIR


@pytest.fixture(scope="session")
def pack_dir():
    return PACK_DIR


@pytest.fixture()
def demo_bundle() -> DomainBundle:
    return load_domain_bundle(DEMO_BUNDLE)


@pytest.fixture()
def repository(pack_dir, demo_bundle) -> DomainRepository:
    return DomainRepository(pack_dir, [demo_bundle])


@pytest.fixture()
def service(repository) -> DomainService:
    return DomainService(repository)
