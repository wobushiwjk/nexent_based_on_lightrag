"""MCP-facing response envelope.

Every tool returns the same shape, whether the call succeeded or failed.  A
client can therefore branch on ``ok`` without parsing tool-specific payloads（载荷）（解析各个工具特有的载荷）,
and an LLM can inspect ``error.code`` instead of guessing from prose（文章、文字）.
定义了MCP工具的统一响应格式，不管工具成功还是失败，返回值都由这些模型描述
"""

from __future__ import annotations

from typing import Any

#BaseModel: Pydantic的基础模型，继承它的类可以检查数据是否符合类型声明，也能转成字典或JSON
#ConfigDict: 配置Pydantic模型的行为
#Field: 为字段设置额外配置
from pydantic import BaseModel, ConfigDict, Field  

#错误信息
class ToolErrorPayload(BaseModel):
    """A machine-readable business error."""

    #模型配置，表示：构造这个模型时，如果提供了模型没有声明的额外字段就报错
    model_config = ConfigDict(extra="forbid")   

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)   #如果没有传details，就调用dict()创建一个新的空字典

#响应附加信息
class ToolMeta(BaseModel):
    """Versions and scope shared by every response."""

    model_config = ConfigDict(extra="forbid")

    ontology_version: str
    schema_version: str
    tenant_id: str | None = None
    read_only: bool = True

#完整的响应外壳
class ToolResponse(BaseModel):
    """The common success/failure envelope returned by every tool."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] | None = None
    error: ToolErrorPayload | None = None
    meta: ToolMeta | None = None
