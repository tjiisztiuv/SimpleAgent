"""Agent loop：请求模型 → 执行模型要求的工具 → 把结果交回模型，直到模型不再调用工具。

要不要调用工具、调用哪个、参数是什么，都由模型决定；这里只负责执行和回传。
状态都在 Session 里，前端（REPL / headless / daemon）只消费 run() 产出的事件。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from simpleagent.agent.context import (
    CLEAR_MIN_GAIN,
    KEEP_RECENT,
    SUMMARY_MAX_CHARS,
    ContextUsage,
    breakdown,
    cleared_result,
    compact_prompt,
    compacted_head,
    estimate_message,
    find_cut,
    is_context_overflow,
    measure,
    save_cleared,
    select_clearable,
)
from simpleagent.agent.session import Session
from simpleagent.config import ContextConfig
from simpleagent.events import (
    ContextEdited,
    Event,
    MaxStepsReached,
    MessageDone,
    TextDelta,
    ToolCallStart,
)
from simpleagent.llm.client import LLM
from simpleagent.tools import ToolContext, ToolRegistry

INTERRUPTED_RESULT = "错误：执行被中断，没有结果"


class CompactError(Exception):
    """摘要请求失败（API 报错、模型没给出摘要）。这时历史没有任何改动。"""


def fill_missing_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给没有结果的 tool_call 补一条「执行被中断」的结果，返回新列表（不改动传入的）。

    工作台早先的会话历史只镜像了事件：中断时 _repair 补的结果没落盘，留下悬空的 tool_call，
    直接发给 API 会被拒绝。迁移到 SessionStore 时用它修一遍。
    """
    fixed: list[dict[str, Any]] = []
    pending: list[str] = []  # 上一条 assistant 发起、还没见到结果的调用

    def flush() -> None:
        fixed.extend(
            {"role": "tool", "tool_call_id": call_id, "content": INTERRUPTED_RESULT}
            for call_id in pending
        )
        pending.clear()

    for message in messages:
        if message.get("role") == "tool":
            if message.get("tool_call_id") in pending:
                pending.remove(message["tool_call_id"])
        else:
            flush()  # 这一批的结果到此为止，还缺的就地补上
        fixed.append(message)
        if message.get("role") == "assistant":
            pending = [call.get("id") or "" for call in message.get("tool_calls") or []]
    flush()
    return fixed


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        system_prompt: str,
        cwd: Path,
        max_steps: int = 20,
        output_dir: Path | None = None,
        hidden_env: frozenset[str] = frozenset(),
        context: ContextConfig | None = None,
    ):
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.max_steps = max_steps
        self.output_dir = output_dir  # 过长的工具输出落盘到这里
        self.hidden_env = hidden_env  # 工具启动子进程时去掉的环境变量（API key）
        self.context = context or ContextConfig()  # 上下文快满时怎么腾地方
        self._task: asyncio.Task | None = None

    def cancel(self) -> None:
        """取消当前正在跑的这轮对话（对应 HTTP 接口的 /cancel）。

        通过取消底层的 asyncio 任务实现：loop 的 except BaseException 会先把历史修成
        合法状态，再把 CancelledError 抛出，由调用方收尾。
        """
        if self._task is not None:
            self._task.cancel()

    def context_usage(self, session: Session) -> ContextUsage:
        """下一次请求预计占多少上下文：上次的实际用量 + 之后新增部分的估算。

        上次的实际用量作废了（刚清理、压缩过）就全量估算，再乘上校准系数。
        """
        return measure(
            session.messages,
            system_prompt=self.system_prompt,
            tools=self.tools.schemas(),
            profile=self.llm.profile,
            anchor=session.context_anchor,
            calibration=session.context_calibration,
        )

    def _estimate(self, session: Session) -> int:
        """把现在的历史发出去，按字符估是多少（不用实际用量、不校准）：算校准系数用。"""
        return measure(
            session.messages,
            system_prompt=self.system_prompt,
            tools=self.tools.schemas(),
            profile=self.llm.profile,
        ).tokens

    def _factor(self, session: Session) -> float:
        """当前模型的校准系数；还没有就是 1。"""
        calibration = session.context_calibration
        if calibration is None or calibration.model != self.llm.profile.model:
            return 1.0
        return calibration.factor

    def context_breakdown(self, session: Session) -> dict[str, int]:
        """上下文由哪些部分构成（按字符估）：system / tools / user / assistant / tool。"""
        return breakdown(
            session.messages,
            system_prompt=self.system_prompt,
            tools=self.tools.schemas(),
            profile=self.llm.profile,
        )

    async def run(self, session: Session, user_input: str) -> AsyncIterator[Event]:
        """处理一条用户输入。中断或出错时先把历史修成合法状态，再把异常原样抛出。"""
        turn_start = len(session.messages)
        session.add({"role": "user", "content": user_input})
        self._task = asyncio.current_task()
        ctx = ToolContext(
            cwd=self.cwd,
            output_dir=self.output_dir,
            hidden_env=self.hidden_env,
            session_id=session.id,
        )
        partial: list[str] = []  # 本次请求已经输出的正文，中断时保存
        compact_failed = False  # 本轮摘要压缩失败过就不再试，免得每一步都白花一次调用
        try:
            for step in range(1, self.max_steps + 1):
                # 每次请求前都查一下，而不只在每轮开始时：一轮里连着调几十次工具也会撑爆。
                # 清理旧工具结果免费，摘要压缩要多调一次 LLM：按清完的样子算还超过阈值才压缩。
                # 要压缩就先压（在原历史上，前缀和上一次请求一致，摘要请求吃得到缓存），再看剩下的
                # 要不要清。反过来的话，清理改掉了前缀，被清的结果反正也要压进摘要，白清一次
                if not compact_failed and self._needs_compact(session):
                    edited, turn_start = await self._compact_in_turn(session, step, turn_start)
                    if edited is not None:
                        compact_failed = edited.error is not None
                        yield edited
                if (edited := self._clear_tool_results(session)) is not None:
                    yield edited
                overflow_retried = False
                while True:
                    request = [{"role": "system", "content": self.system_prompt}, *session.messages]
                    sent = len(session.messages)  # 这次请求覆盖历史的前几条，给预算当基准
                    estimate = self._estimate(session)  # 和实际用量一比就是校准系数
                    done: MessageDone | None = None
                    try:
                        async for event in self.llm.stream(
                            request, tools=self.tools.schemas(), step=step
                        ):
                            if isinstance(event, TextDelta):
                                partial.append(event.text)
                            elif isinstance(event, MessageDone):
                                # 先记进历史再往外发：消费方在这里停下，历史也是完整的
                                done = event
                                session.add(event.message)
                                partial.clear()
                                session.record_stats(event.usage, requests=1)
                                if event.usage and event.usage.prompt_tokens:
                                    session.mark_sent(
                                        event.usage.prompt_tokens,
                                        sent,
                                        self.llm.profile.model,
                                        estimate,
                                    )
                            yield event
                        break
                    except Exception as e:
                        # 服务端说上下文超长、还没有任何输出：预算估低了。强制压缩一次（不看阈值），
                        # 再重试这一步；每步只重试一次，压缩不了或重试还超长就原样抛出。
                        # 只留最后一轮：估算已经不可信，按它留 1/4 的话留下的可能还是超长
                        if overflow_retried or partial or not is_context_overflow(e):
                            raise
                        overflow_retried = True
                        edited, turn_start = await self._compact_in_turn(
                            session, step, turn_start, keep_recent=0
                        )
                        if edited is not None:
                            yield edited
                        if edited is None or edited.error is not None:
                            raise
                assert done is not None, "stream 结束时必须产出 MessageDone"

                tool_calls = done.message.get("tool_calls")
                if not tool_calls:
                    return  # 模型没有调用工具，这一轮结束
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    # 未知工具按只读、默认等级上报：它只会得到一条错误结果
                    known = self.tools.get(name)
                    yield ToolCallStart(
                        call.get("id") or "",
                        name,
                        function.get("arguments") or "",
                        step=step,
                        permission="allow" if known is None else known.permission,
                        readonly=self.tools.is_readonly(name),
                    )
                # 结果按 tool_calls 的顺序回传；全是只读就并行，含写操作就依次执行。
                # 每批结果先记进历史再往外发：中途被中断时，已执行完的调用保留真实结果
                async for batch in self.tools.execute_many(tool_calls, ctx):
                    session.add_many(result.as_message() for result in batch)
                    for result in batch:
                        yield result
            yield MaxStepsReached(self.max_steps)
        except BaseException:  # Ctrl+C（CancelledError）、API 错误、消费方提前退出
            self._repair(session, turn_start, "".join(partial))
            raise

    async def compact(
        self,
        session: Session,
        instructions: str | None = None,
        step: int = 0,
        keep_recent: float = KEEP_RECENT,
        cut: int | None = None,
    ) -> ContextEdited | None:
        """把早期对话压成摘要，保留最近 keep_recent × 输入上限的原文。没有可压缩的返回 None。

        cut 直接指定切点（手动 /compact 用 last_turn_cut，保留最后一轮）：按 1/4 算的话，
        对话不长时全都放得下，find_cut 会挑最早的切点、只压最前面一小段，和手动压缩的预期不符。

        摘要请求 = system + messages[:切点] + 压缩指令，tools 照带：messages[:切点] 正好是
        上一次请求的前缀，输入基本都命中缓存，要付全价的只有指令和摘要。另起一个「总结专用」
        的 system prompt、把旧消息拼成文本发过去看着干净，但全部输入都得按全价算。
        失败（API 报错、没给出摘要）抛 CompactError，历史不动。
        """
        before = self.context_usage(session)
        messages = session.messages
        if cut is None:
            cut = find_cut(messages, int(before.limit * keep_recent))
        if cut is None:
            return None
        request = [
            {"role": "system", "content": self.system_prompt},
            *messages[:cut],
            {
                "role": "user",
                "content": compact_prompt(min(SUMMARY_MAX_CHARS, before.limit // 10), instructions),
            },
        ]
        done: MessageDone | None = None
        try:
            # 流式正文不往外转：这不是给用户的回复。模型不听话调了工具也不执行，只取正文
            async for event in self.llm.stream(
                request,
                tools=self.tools.schemas(),
                step=step,
                continue_turn=self._continues_turn(messages, cut),
            ):
                if isinstance(event, MessageDone):
                    done = event
        except Exception as e:  # noqa: BLE001  Ctrl+C 是 BaseException，照常往外抛
            raise CompactError(f"{type(e).__name__}: {e}") from e
        if done is None:
            raise CompactError("没有收到回复")
        session.record_stats(done.usage, requests=1)
        summary = str(done.message.get("content") or "").strip()
        if not summary:
            raise CompactError("模型没有给出摘要")
        head = compacted_head(summary, cut, str(messages[cut].get("role")))
        session.compact(cut, head)
        # 压缩后重新测：上次的实际用量作废了，全量估算再乘校准系数，和随后 /context 显示的一致
        after = self.context_usage(session).tokens
        return ContextEdited("compact", before.tokens, after, before.limit, cut, usage=done.usage)

    @staticmethod
    def _continues_turn(messages: list[dict[str, Any]], cut: int) -> bool:
        """摘要请求要不要把压缩指令当成本轮的延续（continue_turn）。

        目标是 messages[:cut] 发出去的样子和上一次请求一字不差，才吃得到前缀缓存。上一次请求里，
        思考内容从它的最后一条 user 之后开始回传；上一次请求发的是除了结尾那条还没发出去的
        新 user 之外的全部历史。这条 user 在切点之前：接着它回传（continue_turn）；在切点之后
        （压的全是更早的轮次，比如 /compact 只留最后一轮）：上一次请求里这些思考内容本来就
        去掉了，指令也按新一轮算。
        """
        sent = messages[:-1] if messages and messages[-1].get("role") == "user" else messages
        last_user = max((i for i, m in enumerate(sent) if m.get("role") == "user"), default=-1)
        return last_user < cut

    async def _compact_in_turn(
        self, session: Session, step: int, turn_start: int, keep_recent: float = KEEP_RECENT
    ) -> tuple[ContextEdited | None, int]:
        """一轮进行中的压缩（自动触发、超长兜底共用）。返回 (事件, 调整后的本轮起点)。

        失败不抛异常，转成带 error 的事件；没有可压缩的返回 None。压掉了前 count 条的话，
        本轮起点跟着前移，起点本身被压掉了就从保留部分算起——_repair 中断时靠它找本轮的消息。
        """
        count = len(session.messages)
        try:
            edited = await self.compact(session, step=step, keep_recent=keep_recent)
        except CompactError as e:
            usage = self.context_usage(session)
            failed = ContextEdited("compact", usage.tokens, usage.tokens, usage.limit, error=str(e))
            return failed, turn_start
        if edited is not None:
            turn_start = max(turn_start, edited.count) + len(session.messages) - count
        return edited, turn_start

    def _needs_compact(self, session: Session) -> bool:
        """按清理之后的样子算，还超过 compact_at 才压缩：清理够用就不花这次调用。"""
        compact_at = self.context.compact_at
        if not compact_at:
            return False
        usage = self.context_usage(session)
        _, gain = self._clear_plan(session, usage)
        return (usage.tokens - gain) / usage.limit >= compact_at

    def _clear_plan(
        self, session: Session, before: ContextUsage
    ) -> tuple[list[tuple[int, str]], float]:
        """这次该清哪些工具结果、大约能省多少 token；不该清返回 ([], 0)。不改历史、不写文件。

        清理会改动历史中间的内容，前缀缓存从第一条被改的消息起全部失效，所以不能每次请求
        清一点：过了阈值就把能清的一次清完，落到阈值以下很远，要再涨一大段才会再触发。
        能省下的太少就不清——为一点点地方打破缓存不划算，留给后面的摘要压缩。
        """
        clear_at = self.context.clear_at
        if not clear_at or before.ratio < clear_at:
            return [], 0.0
        messages = session.messages
        picked = select_clearable(messages, self.context.keep_tool_results)
        # 按不带路径的占位符估收益，乘上校准系数，和输入上限是同一个单位（按字符估的会偏高）
        gain = self._factor(session) * sum(
            estimate_message(messages[i])
            - estimate_message(cleared_result(messages[i], name, None))
            for i, name in picked
        )
        if not picked or gain < CLEAR_MIN_GAIN * before.limit:
            return [], 0.0
        return picked, gain

    def _clear_tool_results(self, session: Session) -> ContextEdited | None:
        """上下文快满时，把较早的工具结果换成占位符（原文落盘）。没动历史就返回 None。"""
        before = self.context_usage(session)
        picked, _ = self._clear_plan(session, before)
        if not picked:
            return None
        messages = session.messages
        # 收益够了才落盘：不为最后没清的结果白写文件
        changes = {
            i: cleared_result(
                messages[i], name, save_cleared(self.output_dir, str(messages[i]["content"]))
            )
            for i, name in picked
        }
        session.replace(changes)
        # 清理后重新测：上次的实际用量作废了，全量估算再乘校准系数，和随后 /context 显示的一致。
        # 以前用「清理前 − 省下的估算」，从基本准确的数里减掉偏高的估算，删得多时明显偏小
        after = self.context_usage(session).tokens
        return ContextEdited("clear", before.tokens, after, before.limit, len(changes))

    @staticmethod
    def _repair(session: Session, turn_start: int, partial_text: str) -> None:
        """把本轮历史修成合法状态，否则下一次请求会被 API 拒绝。

        改历史一律走 Session 的方法：写的顺序会被记进 JSONL，恢复时能重放出同一个结果。
        """
        messages = session.messages
        turn = messages[turn_start:]
        answered = {m.get("tool_call_id") for m in turn if m.get("role") == "tool"}
        # 1. 有 tool_call 还没有结果（工具执行时被中断）：补一条结果
        for message in turn:
            for call in message.get("tool_calls") or []:
                if call.get("id") not in answered:
                    session.add(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": INTERRUPTED_RESULT,
                        }
                    )
        # 2. 模型已经输出了一部分正文：保留下来，下一轮能接上
        if partial_text:
            session.add({"role": "assistant", "content": partial_text})
        # 3. 这一轮什么都没留下：撤回用户消息
        if len(session.messages) == turn_start + 1:
            session.truncate(turn_start)
