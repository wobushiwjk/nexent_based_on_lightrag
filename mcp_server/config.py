"""Runtime configuration for the read-only domain MCP server.

Relative paths are resolved against the repository root, not the process
working directory, so a Nexent-launched subprocess sees the same pack as a
developer shell.  Deployments that copy the server elsewhere should set both
``MCP_DOMAIN_PACK`` and ``MCP_DOMAIN_BUNDLES`` explicitly.
"""

from __future__ import annotations  #让类型注解延后处理

import os   #读取环境变量
from dataclasses import dataclass   #帮忙创建主要用于保存数据的类
from pathlib import Path    #处理文件路径，例如拼接路径、判断文件是否存在

REPO_ROOT = Path(__file__).resolve().parents[1]     #_file_ 是当前这个python文件的路径，resolve()返回绝对路径，parents[1]返回上上级目录
DEFAULT_PACK = REPO_ROOT / "domain_packs" / "industrial_fault"  #Path / "目录名" 是pathlib的路径拼接语法
DEFAULT_BUNDLE = DEFAULT_PACK / "seeds" / "demo_bundle.json"


@dataclass(frozen=True) #dataclass是装饰器，能自动生成保存数据时常用的方法，比如初始化函数；frozen表示对象创建后不能随意修改字段
class ServerConfig:
    """Paths and defaults used to build one server instance."""
    
    #类型注解， 描述每个字段预期保存什么
    pack_dir: Path
    bundle_paths: tuple[Path, ...]  #由零个或多个Path组成的元组，...表示数量不限
    default_tenant: str | None  #字符串或没有值
    server_name: str
    log_level: str
    host: str = "127.0.0.1"
    port: int = 8000
    storage_dir: Path | None = None

    @classmethod    #从环境变量创建配置
    def from_env(cls) -> "ServerConfig":    #from_env是类方法：调用它来创建一个ServerConfig对象，cls代表这个类本身，最后的"ServerConfig"是返回类型注解，表示返回一个ServerConfig对象
        pack_raw = os.environ.get("MCP_DOMAIN_PACK", str(DEFAULT_PACK)) #os.environ.get()读取名为MCP_DOMAIN_PACK的环境变量，如果没有设置就使用默认值DEFAULT_PACK
        pack_dir = _resolve(pack_raw)

        bundles_raw = os.environ.get("MCP_DOMAIN_BUNDLES")
        if bundles_raw: #如果设置了MCP_DOMAIN_BUNDLES环境变量，就把它的值按os.pathsep分割成多个路径，然后用_resolve()函数解析每个路径，最后组成一个元组
            bundle_paths = tuple(
                _resolve(part) for part in bundles_raw.split(os.pathsep) if part.strip()    #os.pathsep是操作系统的路径分隔符，part.strip()去掉路径前后的空格，如果路径不为空就解析它
            )
        else:   #如果没设置MCP_DOMAIN_BUNDLES环境变量，就使用默认的bundle路径
            bundle_paths = (DEFAULT_BUNDLE.resolve(),)
        default_tenant = os.environ.get("MCP_DEFAULT_TENANT", "").strip() or None   #如果结果为空就用None
        storage_raw = os.environ.get("MCP_DOMAIN_STORAGE_DIR", "").strip()
        storage_dir = _resolve(storage_raw) if storage_raw else None
        log_level = os.environ.get("MCP_LOG_LEVEL", "INFO").strip().upper()
        host = os.environ.get("MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port_raw = os.environ.get("MCP_PORT", "8000").strip() or "8000"
        try:
            port = int(port_raw)    #把端口转成整数
        except ValueError as exc:   #如果文本不是整数，就捕获转换错误
            raise ValueError(f"MCP_PORT must be an integer, got {port_raw!r}") from exc
        if port < 1 or port > 65535:
            raise ValueError(f"MCP_PORT must be between 1 and 65535, got {port}")
        return cls(     #创建配置对象
            pack_dir=pack_dir,
            bundle_paths=bundle_paths,
            default_tenant=default_tenant,
            server_name=os.environ.get("MCP_SERVER_NAME", "pump-copilot-domain"),
            log_level=log_level,
            host=host,
            port=port,
            storage_dir=storage_dir,
        )


def _resolve(value: str | Path) -> Path:    #开头的_是惯例，表示这是模块内部使用的辅助函数
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()

#__all__是一个约定，用来声明当别人用from mcp_server.config import * 时，哪些名字会被导入，给别人表达哪些名称算公开接口，帮助表达意图
__all__ = ["DEFAULT_BUNDLE", "DEFAULT_PACK", "REPO_ROOT", "ServerConfig"]
