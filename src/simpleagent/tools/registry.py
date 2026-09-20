"""工具注册表：生成 tools 字段，执行模型发来的 tool_call。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from simpleagent.events import ToolResult
from simpleagent.permissions import (
    ApprovalRequest,
    Decision,
    Judgment,
    Scope,
)
from simpleagent.tools.base import Tool, ToolContext, ToolError
from simpleagent.tools.output import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_LINES,
    truncate,
)

if TYPE_CHECKING:
    # 只用于类型标注。不能在这里做运行时导入：serve/__init__ 会拉起 runner，
    # runner 又回来导入本模块的调用方 agent.loop，形成循环导入。
    from simpleagent.permissions import Approver, Policy


def format_validation_error(error: ValidationError) -> str:
    # 不带 input：参数可能很长，模型自己知道传了什么
    return "；".join(
        f"{'.'.join(map(str, err['loc'])) or '参数'}: {err['msg']}"
        for err in error.errors(include_url=False, include_input=False)
    )


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        max_output_chars: int = DEFAULT_MAX_CHARS,
        max_output_lines: int = DEFAULT_MAX_LINES,
        approver: Approver | None = None,
        policy: Policy | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for item in tools:
            self.register(item)
        self.max_output_chars = max_output_chars
        self.max_output_lines = max_output_lines
        # 写操作执行前的审批器；None 表示没人可以问（headless 默认按拒绝处理）
        self.approver = approver
        # 权限判定器；None 表示不判定，所有工具直接放行（M2 的旧行为）
        self.policy = policy

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        # 按注册顺序输出：工具列表在会话内保持不变，请求前缀才能命中缓存
        return [item.schema() for item in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def is_readonly(self, name: str) -> bool:
        """未知工具当只读处理：它只会得到一条错误结果，不会有副作用。"""
        tool = self._tools.get(name)
        return True if tool is None else tool.readonly

    def judge(self, tool: Tool, args: Any, ctx: ToolContext) -> Judgment:
        """判定这次调用该怎么处理：allow / ask / deny，理由在 Judgment.reason 里。

        policy 为 None 时一律放行。scope 提取出错也不甩异常，退化成 ask——
        判断不了的时候交给能决定的人，不替用户做主。
        """
        if self.policy is None:
            return Judgment(Decision.ALLOW)
        try:
            scope = tool.scope(args, ctx) if tool.scope is not None else Scope()
        except Exception:  # noqa: BLE001  工具的 scope 写错不该让整个 loop 崩掉
            return Judgment(Decision.ASK, f"无法判断 `{tool.name}` 的影响范围，需要人工确认")
        return self.policy.decide(tool.permission, scope)

    async def execute(self, tool_call: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行一个 tool_call。任何失败都转成 is_error 的结果回给模型，不抛异常。

        Chat Completions 的 tool 消息没有错误标记，所以错误要写在 content 文本里。
        CancelledError 不在这里处理（它是 BaseException），由 agent loop 收尾。
        """
        call_id = tool_call.get("id") or ""
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        raw_arguments = function.get("arguments") or ""
        start = time.monotonic()

        def error(message: str, decision: str | None = None) -> ToolResult:
            return ToolResult(
                call_id,
                name,
                f"错误：{message}",
                is_error=True,
                duration_ms=(time.monotonic() - start) * 1000,
                decision=decision,
            )

        tool = self._tools.get(name)
        if tool is None:
            return error(f"未知工具 {name}。可用工具：{', '.join(self._tools) or '无'}")
        try:
            # 有些模型调用无参工具时 arguments 传空串
            data = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as e:
            return error(f"参数不是合法 JSON（{e}）：{raw_arguments[:200]}")
        try:
            args = tool.args_model.model_validate(data)
        except ValidationError as e:
            return error(f"参数校验失败：{format_validation_error(e)}")
        judgment = self.judge(tool, args, ctx)
        # 参数校验通过之后才有判定结果：上面几条失败路径的 decision 是 None
        decision = judgment.decision.value
        if judgment.decision is Decision.DENY:
            return error(judgment.reason, decision)
        if judgment.decision is Decision.ASK:
            reason = judgment.reason or f"工具 {name} 会改动文件或执行命令"
            if self.approver is None:
                # 无人值守：需要问但没有可以问的人，一律按拒绝处理。
                # 宁可让模型自己想办法，也不要假装被批准。
                return error(f"{reason}；当前处于无人值守模式，未被授权，默认拒绝", decision)
            answer = await self.approver.request(
                ApprovalRequest(
                    session_id=ctx.session_id or "",
                    tool_name=name,
                    arguments=raw_arguments,
                    reason=reason,
                )
            )
            if not answer.allow:
                return error(f"{reason}；已被拒绝", decision)
        try:
            content = await tool.fn(args, ctx)
        except ToolError as e:
            return error(str(e), decision)
        except Exception as e:  # 工具自身的 bug 也回给模型，不让整个 loop 崩掉
            return error(f"工具执行异常 {type(e).__name__}: {e}", decision)
        truncated = False
        if tool.truncate_output:
            before = len(content)
            content = self.trim(content, name, ctx)
            truncated = len(content) != before
        return ToolResult(
            call_id,
            name,
            content,
            duration_ms=(time.monotonic() - start) * 1000,
            decision=decision,
            truncated=truncated,
        )

    async def execute_many(
        self, tool_calls: list[dict[str, Any]], ctx: ToolContext
    ) -> AsyncIterator[list[ToolResult]]:
        """执行同一条消息里的多个 tool_call，按 tool_calls 的顺序分批产出结果。

        全是只读工具就并行（省时间），所有结果作为一批产出；只要有一个写操作就按原顺序
        逐个执行，每执行完一个就产出一批——模型常常“先读 A 再写 A”，并行的话可能读到
        旧内容，或者两个写互相覆盖。逐个产出是为了中途被中断时，已经执行完的写操作
        能如实记进历史，而不是被当成“没有结果”，让模型重做一遍。
        """
        names = [(call.get("function") or {}).get("name") or "" for call in tool_calls]
        if all(self.is_readonly(name) for name in names):
            yield list(await asyncio.gather(*(self.execute(call, ctx) for call in tool_calls)))
            return
        for call in tool_calls:
            yield [await self.execute(call, ctx)]

    def trim(self, content: str, name: str, ctx: ToolContext) -> str:
        """过长的输出只留开头，完整内容落盘。"""
        return truncate(
            content,
            self.max_output_chars,
            self.max_output_lines,
            save=lambda text: ctx.save_output(text, name),
        )
