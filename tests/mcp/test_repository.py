"""Repository loading, indexing, and tenant isolation."""

from __future__ import annotations

import pytest

from mcp_server.errors import DomainError, DomainRepositoryError
from mcp_server.repository import DomainRepository
from tests.mcp.helpers import (
    BAD_BUNDLE,
    DEMO_BUNDLE,
    PACK_DIR,
    retag_bundle,
)

pytestmark = pytest.mark.offline


def test_repository_indexes_the_demo_bundle(repository):
    index = repository.tenant_index("tenant-demo")

    assert repository.tenants == ("tenant-demo",)
    assert repository.default_tenant == "tenant-demo"
    assert len(index.entities) == 9
    assert len(index.evidence) == 6
    assert len(index.facts) == 2
    assert len(index.rules) == 2
    assert index.tree is not None
    assert index.trace is not None
    assert index.equipment_by_tag["p-101a"] == "EQ-P101A"
    assert index.evidence_by_target[("rule", "RULE-CAVITATION-AND")] == (
        "EV-MANUAL-3",
        "EV-RECORD-2",
    )
    assert [rule.rule_uid for rule in index.rules_by_effect["FAULT-LOW-PRESSURE"]] == [
        "RULE-LOW-PRESSURE-OR"
    ]
    assert {rule.rule_uid for rule in index.rules_by_input["FAULT-FILTER-BLOCKED"]} == {
        "RULE-CAVITATION-AND",
        "RULE-LOW-PRESSURE-OR",
    }


def test_missing_tenant_is_not_found_without_enumeration(repository):
    with pytest.raises(DomainError) as exc_info:
        repository.tenant_index("tenant-other")

    error = exc_info.value
    assert error.code == "not_found"
    assert error.details == {"tenant_id": "tenant-other"}
    assert "tenant-demo" not in error.message


def test_invalid_bundle_is_rejected_at_startup():
    with pytest.raises(DomainRepositoryError) as exc_info:
        DomainRepository.from_paths(PACK_DIR, [BAD_BUNDLE])

    codes = {issue.code for issue in exc_info.value.issues}
    assert "evidence-quote-not-verbatim" in codes
    assert "evidence-reference-missing" in codes


def test_duplicate_tenant_bundle_is_rejected(demo_bundle):
    with pytest.raises(DomainRepositoryError, match="duplicate bundle"):
        DomainRepository(
            PACK_DIR,
            [demo_bundle, demo_bundle.model_copy(deep=True)],
        )


def test_multiple_tenants_require_an_explicit_tenant(demo_bundle):
    other = retag_bundle(
        demo_bundle,
        "tenant-other",
        rename_fault_uid="FAULT-CAVITATION",
        canonical_name="其他租户专用故障",
    )
    repository = DomainRepository(PACK_DIR, [demo_bundle, other])

    assert repository.tenants == ("tenant-demo", "tenant-other")
    assert repository.default_tenant is None
    with pytest.raises(DomainError) as exc_info:
        repository.tenant_index("")
    assert exc_info.value.code == "invalid_argument"

    assert repository.tenant_index("tenant-demo").tenant_id == "tenant-demo"
    assert repository.tenant_index("tenant-other").tenant_id == "tenant-other"


def test_default_tenant_must_be_loaded(demo_bundle):
    with pytest.raises(DomainRepositoryError, match="default tenant"):
        DomainRepository(
            PACK_DIR,
            [demo_bundle],
            default_tenant="tenant-missing",
        )


def test_from_paths_loads_the_demo_bundle():
    repository = DomainRepository.from_paths(PACK_DIR, [DEMO_BUNDLE])
    assert repository.tenants == ("tenant-demo",)


def test_from_paths_rejects_a_missing_bundle(tmp_path):
    with pytest.raises(DomainRepositoryError, match="not found"):
        DomainRepository.from_paths(PACK_DIR, [tmp_path / "missing.json"])
