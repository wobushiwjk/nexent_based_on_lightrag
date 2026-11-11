"""Fault-tree ontology and extraction support."""

from lightrag.fault_tree.contracts import (
    ONTOLOGY_VERSION,
    SCHEMA_VERSION,
    DecisionTrace,
    DomainBundle,
    Evidence,
    Fact,
    FaultCauseRule,
    FaultTreeDraft,
    ValidationIssue,
    ValidationResult,
)
from lightrag.fault_tree.domain_validation import (
    load_domain_bundle,
    validate_crosswalk,
    validate_domain_bundle,
    validate_fault_tree_draft,
    validate_schema_manifest,
)
from lightrag.fault_tree.ontology import OntologyRegistry
from lightrag.fault_tree.evidence_store import EvidenceStore
from lightrag.fault_tree.graph import (
    GraphProjection,
    project_candidate_batch,
    project_domain_graph,
)
from lightrag.fault_tree.projection import (
    CandidateBatchInput,
    ProjectedDomain,
    ProjectionError,
    SourceChunk,
    project_domain,
    span_matches,
)
from lightrag.fault_tree.tree_builder import (
    AmbiguousRuleError,
    MissingRuleError,
    TreeBuildError,
    build_fault_tree_draft,
)
from lightrag.fault_tree.schemas import (
    EvidenceCandidate,
    Expression,
    FaultCandidateBatch,
    FaultEntityPayload,
    FaultRelationPayload,
)
from lightrag.fault_tree.service import configure_fault_tree_validator
from lightrag.fault_tree.validator import FaultCandidateValidator
from lightrag.fault_tree.views import (
    GRAPH_VIEW_IDS,
    GraphViewError,
    build_graph_view,
    get_graph_view,
)

__all__ = [
    "AmbiguousRuleError",
    "CandidateBatchInput",
    "DecisionTrace",
    "DomainBundle",
    "Evidence",
    "EvidenceCandidate",
    "EvidenceStore",
    "Expression",
    "Fact",
    "FaultCandidateBatch",
    "FaultCandidateValidator",
    "FaultCauseRule",
    "FaultEntityPayload",
    "FaultRelationPayload",
    "FaultTreeDraft",
    "GRAPH_VIEW_IDS",
    "GraphViewError",
    "GraphProjection",
    "MissingRuleError",
    "ONTOLOGY_VERSION",
    "OntologyRegistry",
    "ProjectedDomain",
    "ProjectionError",
    "SCHEMA_VERSION",
    "SourceChunk",
    "TreeBuildError",
    "ValidationIssue",
    "ValidationResult",
    "build_fault_tree_draft",
    "build_graph_view",
    "configure_fault_tree_validator",
    "get_graph_view",
    "load_domain_bundle",
    "project_candidate_batch",
    "project_domain",
    "project_domain_graph",
    "span_matches",
    "validate_crosswalk",
    "validate_domain_bundle",
    "validate_fault_tree_draft",
    "validate_schema_manifest",
]
