"""把 DomainService 里的 Python 查询方法注册成 MCP 工具，并统一处理成功和错误的响应。"""

from __future__ import annotations  #让类型注解延后处理

import logging
from typing import Annotated, Any, Literal  #Python的类型标注工具

from mcp.server.fastmcp import FastMCP  #创建MCP服务并注册工具
from mcp.types import ToolAnnotations   #给工具加描述性质的标记，例如"只读"
from pydantic import Field  #声明参数约束，比如限制limit必须在1到50之间

#下面是项目自己的代码
from mcp_server.errors import INTERNAL_ERROR, DomainError   #未预期的内部错误，实际负责查询的服务类
from mcp_server.models import ToolResponse  #统一响应的数据结构
from mcp_server.service import DomainService    #实际负责查询的服务类

logger = logging.getLogger(__name__)

#工具注解对象，之后会附加到每个MCP工具上
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,  #只读
    destructiveHint=False,  #不会进行破坏性操作
    idempotentHint=True,    #重复调用不会改变数据 idempotent 幂等的
    openWorldHint=False,    #不是面向开放外部世界的操作
)

#给Agent的服务说明
SERVER_INSTRUCTIONS = """
Read-only domain tools for industrial equipment fault diagnosis.

Always pass tenant_id to data tools. Every tool returns one envelope:
{"ok": bool, "data": object|null, "error": {code, message, details}|null,
 "meta": {ontology_version, schema_version, tenant_id, read_only}}.
Inspect error.code instead of parsing prose.

Suggested flow:
1. get_ontology for classes, relations and constraints.
2. get_equipment or search_faults to find the asset or candidate faults.
3. get_fault_tree for the AND/OR structure.
4. trace_evidence for the verbatim source fragment behind a record.
5. validate_fault_tree before publishing a draft.

These tools never write to storage. Tenant isolation is enforced by the
repository; a tenant that is not loaded is reported as not_found.
""".strip()


#创建并配置MCP服务
def build_server(
    service: DomainService, #传入已经创建好的查询服务
    *,                      #星号之后的参数必须通过参数名传入
    name: str = "pump-copilot-domain",  
    log_level: str = "INFO",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:               #函数会返回一个FastMCP对象
    """Build a FastMCP instance bound to one validated domain service."""

    #把MCP服务的名称、说明、日志级别和网络地址交给FastMCP，创建服务对象mcp
    mcp = FastMCP(
        name=name,
        instructions=SERVER_INSTRUCTIONS,
        log_level=log_level,
        host=host,
        port=port,
    )

    #装饰器 == 告诉FastMCP：把紧跟着定义的这个Python函数注册成MCP工具
    @mcp.tool(
        name="get_ontology",    #客户端调用工具时使用的名字
        title="Get domain ontology",    #比较易读的标题
        description=(   #告诉客户端工具的用途
            "Return the ontology version, classes, relations, constraints and "
            "standards for the fault-tree domain. Shared by every tenant and "
            "read-only."
        ),
        annotations=READ_ONLY,  #附加工具属性，只读
    )
    def get_ontology() -> ToolResponse:
        #如果写成service.get_ontology()就会在此时立即执行方法，而不是交给_invoke统一处理
        return _invoke(service, None, service.get_ontology) #service.get_ontology不加括号表示方法本身，表示把这个方法交出去，以后再调用

    @mcp.tool(
        name="get_equipment",
        title="Get equipment",
        description=(
            "Look up one equipment record by UID, equipment tag, name or "
            "alias. Returns its source files, linked facts, linked faults and "
            "evidence pointers."
        ),
        annotations=READ_ONLY,
    )
    def get_equipment(tenant_id: str, equipment_ref: str) -> ToolResponse:
        return _invoke(
            service,
            tenant_id,
            #lambda是一个简短的匿名函数，作用是把查询延后执行，并把MCP收到的参数传给服务方法
            lambda: service.get_equipment(tenant_id, equipment_ref),
        )

    @mcp.tool(
        name="search_faults",
        title="Search faults",
        description=(
            "Rank fault events by name, alias, UID and description, and return "
            "the causal rules each fault belongs to. Returns an empty result "
            "list when nothing matches."
        ),
        annotations=READ_ONLY,
    )
    def search_faults(
        tenant_id: str,
        query: str,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> ToolResponse:
        return _invoke(
            service,
            tenant_id,
            lambda: service.search_faults(tenant_id, query, limit),
        )

    @mcp.tool(
        name="get_fault_tree",
        title="Get fault tree",
        description=(
            "Return a projected fault-tree draft with its nodes, edges, "
            "AND/OR rule expressions and evidence IDs. Pass draft_id or "
            "top_event_uid; with neither, return the tenant's only tree."
        ),
        annotations=READ_ONLY,
    )
    def get_fault_tree(
        tenant_id: str,
        draft_id: str | None = None,
        top_event_uid: str | None = None,
    ) -> ToolResponse:
        return _invoke(
            service,
            tenant_id,
            lambda: service.get_fault_tree(
                tenant_id,
                draft_id=draft_id,
                top_event_uid=top_event_uid,
            ),
        )

    @mcp.tool(
        name="trace_evidence",
        title="Trace evidence",
        description=(
            "Return the verbatim evidence fragment and source asset for an "
            "entity, fact, rule, logic gate or decision step. Logic-gate "
            "evidence resolves through the gate's rule."
        ),
        annotations=READ_ONLY,
    )
    def trace_evidence(
        tenant_id: str,
        target_type: Literal[
            "entity",
            "fact",
            "rule",
            "logic_gate",
            "decision_step",
        ],
        target_id: str,
    ) -> ToolResponse:
        return _invoke(
            service,
            tenant_id,
            lambda: service.trace_evidence(
                tenant_id,
                target_type,
                target_id,
            ),
        )

    @mcp.tool(
        name="validate_fault_tree",
        title="Validate fault tree",
        description=(
            "Validate a stored draft_id or an inline FaultTreeDraft against "
            "the tenant's rules and entities. Returns errors and warnings; it "
            "does not persist anything."
        ),
        annotations=READ_ONLY,
    )
    def validate_fault_tree(
        tenant_id: str,
        draft_id: str | None = None,
        draft: dict[str, Any] | None = None,
    ) -> ToolResponse:
        return _invoke(
            service,
            tenant_id,
            lambda: service.validate_fault_tree(
                tenant_id,
                draft_id=draft_id,
                draft=draft,
            ),
        )

    return mcp


def _invoke(
    service: DomainService,
    tenant_id: str | None,
    action: Any,
) -> ToolResponse:
    try:
        return service.success(action(), tenant_id)
    except DomainError as exc:
        return service.failure(exc, tenant_id)
    except Exception:
        logger.exception("unhandled error in domain MCP tool")
        return service.failure(
            DomainError(INTERNAL_ERROR, "unexpected server error"),
            tenant_id,
        )


__all__ = ["SERVER_INSTRUCTIONS", "build_server"]
