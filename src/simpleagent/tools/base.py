"""工具抽象：执行上下文、Tool、@tool 装饰器。

一个工具 = pydantic 参数模型 + async 函数。参数模型同时用来生成给模型看的 JSON Schema
和校验模型传来的参数，两边不会对不上。
"""

from __future__ import annotations

import hashlib
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel

from simpleagent.permissions import Scope

ToolFn = Callable[[Any, "ToolContext"], Awaitable[str]]
# (参数对象, 上下文) -> 这次调用会改动什么。由工具自己实现，注册表拿它去问 Policy
ScopeFn = Callable[[Any, "ToolContext"], Scope]

OUTPUT_RETENTION_SECONDS = 7 * 24 * 3600  # 落盘的工具输出保留 7 天，每次落盘时顺手清理


@dataclass
class ToolContext:
    """工具执行时拿到的环境。后续加入审批器（M3）、进度上报、取消信号。"""

    cwd: Path
    # 过长输出落盘的目录；None 表示不落盘（截断时模型只能看到开头）
    output_dir: Path | None = None
    # 启动子进程时要去掉的环境变量（配置里各 profile 的 api_key_env），
    # 否则用户 export 的 key 会被 bash 继承，`env` 一下就进了模型上下文和 trace
    hidden_env: frozenset[str] = field(default_factory=frozenset)
    # 当前会话 id（持久化 / 事件路由用）；默认 None（无会话上下文时）
    session_id: str | None = None

    def resolve(self, path: str) -> Path:
        """相对路径基于 ctx.cwd 解析，不用进程的当前目录（daemon 里两者不一样）。"""
        return (self.cwd / Path(path).expanduser()).resolve()

    def subprocess_env(self) -> dict[str, str]:
        """子进程用的环境变量：当前进程的环境去掉 hidden_env。"""
        return {name: value for name, value in os.environ.items() if name not in self.hidden_env}

    def save_output(self, content: str, prefix: str = "output") -> Path | None:
        """把完整输出写到 <output_dir>/<prefix>-<时间>-<摘要>.txt，返回路径。

        写失败返回 None：落盘只是兜底，不能让工具调用失败。
        """
        if self.output_dir is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:8]
        path = self.output_dir / f"{prefix}-{stamp}-{digest}.txt"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError:
            return None
        self._prune_outputs(keep=path)
        return path

    def _prune_outputs(self, keep: Path) -> None:
        """删掉超过保留期的落盘输出，免得目录无限增长；清理失败不影响本次调用。"""
        assert self.output_dir is not None
        deadline = time.time() - OUTPUT_RETENTION_SECONDS
        try:
            for old in self.output_dir.glob("*.txt"):
                if old != keep and old.stat().st_mtime < deadline:
                    old.unlink(missing_ok=True)
        except OSError:
            pass


class ToolError(Exception):
    """可以预期的失败（比如路径不存在）：消息原样回给模型，让它自己调整参数。"""


# JSON Schema 里值是子 schema 的字段；其余字段（default、enum 等）里的 title 不是 schema 标题
_SCHEMA_MAPS = ("properties", "$defs")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")
_SCHEMA_VALUES = ("items", "additionalProperties", "not")


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """去掉 pydantic 自动生成的 title：对模型没有信息量，白占 token。"""
    cleaned = {key: value for key, value in schema.items() if key != "title"}
    for key in _SCHEMA_MAPS:
        if key in cleaned:
            cleaned[key] = {name: clean_schema(sub) for name, sub in cleaned[key].items()}
    for key in _SCHEMA_LISTS:
        if key in cleaned:
            cleaned[key] = [clean_schema(sub) for sub in cleaned[key]]
    for key in _SCHEMA_VALUES:
        if isinstance(cleaned.get(key), dict):
            cleaned[key] = clean_schema(cleaned[key])
    return cleaned


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn
    # 只读工具（不改动文件系统、不产生副作用）可以并行执行；
    # 含写操作时同一批调用要按原顺序依次执行，避免互相覆盖。
    readonly: bool = True
    # 是否由注册表统一截断过长输出；自己会分页的工具（read_file）设成 False
    truncate_output: bool = True
    # 权限等级：allow 直接执行、ask 先问审批器、deny 一律拒绝。见 permissions.Policy
    permission: Literal["allow", "ask", "deny"] = "allow"
    # 报告这次调用的作用范围（会改动哪些路径 / 要跑什么命令）。None 表示不涉及修改。
    # 之所以让工具自己声明而不是注册表统一反射：MCP、Skills 注册进来的工具也能各自标注，
    # 加工具时不用回头改 Policy。
    scope: ScopeFn | None = None
    # 现成的 JSON Schema（MCP 工具自带）。给了就原样交给模型，args_model 只做最宽松的校验；
    # None 表示从 args_model 生成（内置工具）
    parameters: dict[str, Any] | None = None
    # 需要人确认时显示的说明；None 用注册表的默认说法（「会改动文件或执行命令」）。
    # MCP 工具发邮件、建日程，默认说法就不对了，由它们各自说明
    confirm_reason: str | None = None

    def schema(self) -> dict[str, Any]:
        """请求体 tools 字段里的一项（OpenAI function calling 格式）。"""
        parameters = self.parameters
        if parameters is None:
            parameters = clean_schema(self.args_model.model_json_schema())
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


def tool(
    name: str,
    description: str,
    *,
    readonly: bool = True,
    truncate_output: bool = True,
    permission: Literal["allow", "ask", "deny"] = "allow",
    scope: ScopeFn | None = None,
) -> Callable[[ToolFn], Tool]:
    """把 `async def fn(args: SomeArgs, ctx: ToolContext) -> str` 包装成 Tool。

    参数模型从第一个参数的类型注解推出。readonly=False 表示这个工具会改动文件或
    执行命令（write_file / edit_file / bash）；truncate_output=False 表示工具自己控制
    输出长度，注册表不再截断。permission 是这个工具的默认权限等级，scope 报告本次
    调用会改动什么，两者都由注册表在执行前拿去问权限 Policy。
    """

    def decorate(fn: ToolFn) -> Tool:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"工具 {name} 必须是 async 函数")
        params = list(inspect.signature(fn).parameters)
        args_model = get_type_hints(fn).get(params[0]) if params else None
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"工具 {name} 的第一个参数必须标注为 pydantic BaseModel 子类")
        return Tool(
            name,
            description,
            args_model,
            fn,
            readonly,
            truncate_output,
            permission,
            scope,
        )

    return decorate
