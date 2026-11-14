"""Offline inventory tests for the upload ingestion MCP tools."""

from __future__ import annotations

from pathlib import Path

from ingestion_mcp_server.server import build_server
from ingestion_mcp_server.service import IngestionToolService
from lightrag.fault_tree.ingestion import DocumentIngestionService
from lightrag.fault_tree.ontology import OntologyRegistry

PACK = Path(__file__).resolve().parents[2] / "domain_packs" / "industrial_fault"


def test_server_exposes_the_complete_upload_surface(tmp_path: Path) -> None:
    uploads = tmp_path / "inbox"
    uploads.mkdir()
    sdk = build_server(
        IngestionToolService(
            DocumentIngestionService(
                pack_dir=PACK,
                storage_dir=tmp_path / "storage",
                upload_dirs=[uploads],
                ontology=OntologyRegistry.from_file(PACK / "ontology.yml"),
            )
        )
    )

    tools = {tool.name for tool in sdk._tool_manager.list_tools()}

    assert tools == {
        "list_uploaded_documents",
        "read_uploaded_document",
        "ingest_uploaded_document",
        "get_upload_ingestion_status",
    }
