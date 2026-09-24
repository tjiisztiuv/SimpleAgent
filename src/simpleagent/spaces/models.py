"""空间（Space）与会话元信息（SessionMeta）的数据模型。

设计要点（见 docs/design/client-ui.md）：
- Space 是唯一真值，落在 spaces/<id>/space.toml
- kind 只管目录形态（generic 用 tmp / agent 进 cwd）
- executor 只管谁跑（内置 loop / 外部 CLI），两者正交
- 会话消息只追加，落在 spaces/<id>/sessions/<sid>.jsonl
- 可变元信息（标题/状态/验证）走 sidecar spaces/<id>/sessions/<sid>.meta.json
- Task 不单独建模，就是 Session
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC
from typing import Any, Literal

from simpleagent.agents.base import PERMISSIONS, SAFE


def _now() -> str:
    from datetime import datetime

    # 毫秒分辨率，保证同一秒内多次写入也能稳定排序
    return datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")


# 谁在跑这个空间：内置 loop，还是外部 CLI。顺序就是向导里下拉框的顺序。
EXECUTORS = ("simpleagent", "claude-code", "opencode")
EXECUTOR_LABELS = {
    "simpleagent": "内置 SimpleAgent",
    "claude-code": "claude-code（外部 CLI）",
    "opencode": "opencode（外部 CLI）",
}
# 外部 CLI 默认拉起的可执行文件；space.toml 的 [agent].command 可以覆盖
DEFAULT_CLI_COMMAND = {"claude-code": "claude", "opencode": "opencode"}


def locked_reason(session_executor: str, space_executor: str) -> str:
    """会话被锁住（执行者和空间当前的对不上）时给人看的原因，runner 和 API 共用。"""
    return (
        f"这个会话由 {session_executor} 跑，空间已切到 {space_executor}；"
        "新建会话继续，或把空间切回去"
    )


def validate_executor(
    executor: str, *, cli_model: str | None, permission: str, command: str | None
) -> None:
    """「谁跑」这组字段的合法性，新建（from_spec）和切换（change_executor）共用。"""
    if executor not in EXECUTORS:
        raise ValueError(f"未知的执行者：{executor}")
    if executor == "simpleagent":
        if cli_model:
            raise ValueError("内置执行者的模型用 profile 选，不要填 cli_model")
        if permission != SAFE:
            raise ValueError("内置执行者的权限由审批器管，不要填 permission")
    elif command is None and executor not in DEFAULT_CLI_COMMAND:
        raise ValueError(f"执行者 {executor} 没有默认命令，请显式填 command")
    if permission not in PERMISSIONS:
        raise ValueError(f"未知的权限档：{permission}")


@dataclass
class GenericConfig:
    """通用任务空间：无专有目录，用 spaces/<id>/tmp。"""

    tmp_dir: str = "auto"  # auto => spaces/<id>/tmp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GenericConfig:
        return cls(tmp_dir=d.get("tmp_dir", "auto"))


@dataclass
class AgentBinding:
    """外部 CLI 的启动细节，只在 executor != simpleagent 时存在。

    「谁跑」（executor）和「在哪儿跑」（cwd）都是 Space 的顶层属性；这里只放
    「怎么把这个 CLI 拉起来」。不填就是按 executor 推导出的默认命令。
    """

    command: str = "claude"
    args: list[str] = field(default_factory=list)
    resume_flag: str = ""  # 用于追问，配合 meta 里的 agent_session_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentBinding:
        # 旧文件里 command 缺省时曾经回退到 [agent].name，这里保留这条兼容路径
        return cls(
            command=d.get("command") or d.get("name") or "claude",
            args=list(d.get("args", [])),
            resume_flag=d.get("resume_flag", ""),
        )


@dataclass
class VerifyConfig:
    """验证命令：session 停止/一轮结束时跑，用退出码判定。"""

    command: str | None = None
    trigger: Literal["off", "on_stop", "on_turn"] = "off"
    timeout: int = 300

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VerifyConfig:
        return cls(
            command=d.get("command"),
            trigger=d.get("trigger", "off"),
            timeout=int(d.get("timeout", 300)),
        )


@dataclass
class Verification:
    """单个 session 的验证状态。"""

    status: str = "unknown"  # unknown|running|passed|failed|stale
    command: str | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    output_ref: str | None = None  # 完整输出落盘路径（复用 tool_outputs 那套）
    output: str | None = None  # 截断后的输出，直接给 UI 看（完整版留给 output_ref）
    fingerprint: str | None = None  # 验证通过时的目录指纹，用于判 stale
    source: str = "auto"  # auto|manual

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Verification:
        return cls(
            status=d.get("status", "unknown"),
            command=d.get("command"),
            exit_code=d.get("exit_code"),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            output_ref=d.get("output_ref"),
            output=d.get("output"),
            fingerprint=d.get("fingerprint"),
            source=d.get("source", "auto"),
        )


@dataclass
class SessionMeta:
    """会话的可变元信息（不进 jsonl，单独 sidecar）。"""

    id: str
    space_id: str
    title: str = "新会话"
    status: str = "idle"  # running|idle|done|error|cancelled
    pinned: bool = False
    agent: str = "simpleagent"
    agent_session_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    verification: Verification = field(default_factory=Verification)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionMeta:
        return cls(
            id=d["id"],
            space_id=d["space_id"],
            title=d.get("title", "新会话"),
            status=d.get("status", "idle"),
            pinned=d.get("pinned", False),
            agent=d.get("agent", "simpleagent"),
            agent_session_id=d.get("agent_session_id"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            usage=d.get("usage", {}) or {},
            verification=Verification.from_dict(d.get("verification", {}) or {}),
        )


@dataclass
class SpaceSpec:
    """新建空间的入参（来自向导）。

    两个维度分开：kind 只管「在哪儿跑」，executor 只管「谁跑」，四种组合都合法。
    模型也分两栏：内置看 profile，外部 CLI 看 cli_model（None = 用它本机的默认配置）。
    """

    name: str
    kind: Literal["generic", "agent"]
    executor: str = "simpleagent"
    profile: str = "default"  # executor=simpleagent 时用
    cli_model: str | None = None  # executor 是外部 CLI 时用；None = 不注入配置
    permission: str = SAFE  # 外部 CLI 的权限档：safe 只读 / full 全放行
    pin_dir: str | None = None
    cwd: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    verify_command: str | None = None
    verify_trigger: Literal["off", "on_stop", "on_turn"] = "off"
    verify_timeout: int = 300


@dataclass
class Space:
    """一类任务的容器：名字 + 工作目录 + 用哪个 agent 跑 + 验证方式。"""

    id: str
    name: str
    kind: Literal["generic", "agent"]  # 只管目录形态：generic 用 tmp，agent 进 cwd
    executor: str = "simpleagent"  # 只管谁跑：simpleagent | claude-code | opencode
    profile: str = "default"  # executor=simpleagent 时的模型
    cli_model: str | None = None  # 外部 CLI 的模型 preset；None = 用本机默认配置
    permission: str = SAFE  # 外部 CLI 的权限档（safe 只读 / full 全放行）
    cwd: str | None = None  # kind=agent 时必填；kind=generic 时为空（用 tmp）
    opened: bool = True  # 是否在左栏显示（关闭只是不显示，不删数据）
    pinned: bool = False
    created_at: str = ""
    last_opened_at: str = ""
    keep_sessions: int = 50  # 超出只归档不删
    generic: GenericConfig | None = None
    agent: AgentBinding | None = None  # 仅 executor != simpleagent
    verify: VerifyConfig | None = None

    @classmethod
    def from_spec(cls, spec: SpaceSpec, space_id: str) -> Space:
        """从向导入参构造，顺便把非法组合拦在这儿（API 层直接转 400）。"""
        if spec.kind not in ("generic", "agent"):
            raise ValueError(f"未知的空间形态：{spec.kind}")
        validate_executor(
            spec.executor,
            cli_model=spec.cli_model,
            permission=spec.permission,
            command=spec.command,
        )
        if spec.kind == "agent" and not spec.cwd:
            raise ValueError("绑定目录的空间必须填工作目录")
        if spec.kind == "generic" and spec.cwd:
            raise ValueError("通用任务的工作目录由系统分配（spaces/<id>/tmp），不要指定 cwd")

        created = _now()
        # 验证命令两类空间都支持（generic 也能跑 pytest 之类），先在这里统一构造
        verify = None
        if spec.verify_command:
            verify = VerifyConfig(
                command=spec.verify_command,
                trigger=spec.verify_trigger,
                timeout=spec.verify_timeout,
            )
        # agent 段与 kind 解耦：通用任务也能用外部 agent 跑（工作目录落到 tmp）
        agent = None
        if spec.executor != "simpleagent":
            agent = AgentBinding(
                command=spec.command or DEFAULT_CLI_COMMAND[spec.executor],
                args=list(spec.args),
            )
        return cls(
            id=space_id,
            name=spec.name,
            kind=spec.kind,
            executor=spec.executor,
            profile=spec.profile,
            cli_model=spec.cli_model,
            permission=spec.permission,
            cwd=spec.cwd,
            created_at=created,
            last_opened_at=created,
            generic=GenericConfig(tmp_dir="auto") if spec.kind == "generic" else None,
            agent=agent,
            verify=verify,
        )

    def to_dict(self) -> dict[str, Any]:
        """整份空间定义序列化为可 JSON 化的 dict（嵌套 dataclass 一并展开）。"""
        return asdict(self)
