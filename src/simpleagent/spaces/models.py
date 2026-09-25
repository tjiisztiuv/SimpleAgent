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

import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC
from typing import Any, Literal

from simpleagent.agents.base import PERMISSIONS, SAFE
from simpleagent.permissions import Mode, parse_mode


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

# 指挥台的调度者住在这个保留空间里（左栏不显示）。它的会话就是一次次调度，
# 派出去的子会话落在各自的目标空间，meta 里记着 parent_session_id 指回来
COMMAND_SPACE_ID = "sp_command"
COMMAND_SPACE_NAME = "指挥台"


# 空间简介的上限：每个空间的简介都进调度者的 system prompt，每次请求都要带上
DESCRIPTION_MAX = 200


def one_line(text: str) -> str:
    """控制字符换成空格、连续空白（含换行）拍平成一个空格、去掉首尾空白。

    控制字符要去掉：TOML 字符串里不许有，写进 space.toml 就读不回来，整个空间会从列表里消失。
    """
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in str(text or ""))
    return " ".join(text.split())


def clean_description(text: str) -> str:
    """整理空间简介（拍平成一行）；超长直接报错（API 层转 400）。

    报错而不是悄悄截断：简介决定任务派给谁，截掉的半句话可能正是关键。
    """
    text = one_line(text)
    if len(text) > DESCRIPTION_MAX:
        raise ValueError(f"简介最多 {DESCRIPTION_MAX} 字，现在 {len(text)} 字")
    return text


def locked_reason(session_executor: str, space_executor: str) -> str:
    """会话被锁住（执行者和空间当前的对不上）时给人看的原因，runner 和 API 共用。"""
    return (
        f"这个会话由 {session_executor} 跑，空间已切到 {space_executor}；"
        "新建会话继续，或把空间切回去"
    )


# W 里程碑时外部 CLI 的「只读」存的是 safe
LEGACY_PERMISSIONS = {"safe": SAFE}


def normalize_permission(value: str | None) -> str | None:
    """空间的权限模式统一存 Mode 的值；None 表示没单独设过（见 Space.effective_mode）。

    兼容旧值 safe 和中文名；认不出来抛 ValueError（API 层转 400）。
    """
    if not value:
        return None
    if value in LEGACY_PERMISSIONS:
        return LEGACY_PERMISSIONS[value]
    return parse_mode(value).value


def validate_executor(
    executor: str, *, cli_model: str | None, permission: str | None, command: str | None
) -> None:
    """「谁跑」这组字段的合法性，新建（from_spec）和切换（change_executor）共用。

    permission 要先过 normalize_permission。
    """
    if executor not in EXECUTORS:
        raise ValueError(f"未知的执行者：{executor}")
    if executor == "simpleagent":
        if cli_model:
            raise ValueError("内置执行者的模型用 profile 选，不要填 cli_model")
    elif command is None and executor not in DEFAULT_CLI_COMMAND:
        raise ValueError(f"执行者 {executor} 没有默认命令，请显式填 command")
    if permission is None or executor == "simpleagent":
        return
    if permission not in PERMISSIONS:
        choices = "、".join(f"「{Mode(p).label}」" for p in PERMISSIONS)
        raise ValueError(f"外部 CLI 暂时只有{choices}两档，不支持「{Mode(permission).label}」")


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
    # 由指挥台派发的子会话：指回调度者的那个会话。创建时定下，之后不变（记出身，左栏「派」标记看它）
    parent_session_id: str | None = None
    # 最近一次是哪个调度会话让它跑的（派发、追问都会更新）。指挥台的卡片挂在这个调度者下面
    dispatched_by: str | None = None
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
            parent_session_id=d.get("parent_session_id"),
            dispatched_by=d.get("dispatched_by"),
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
    # 权限模式（Mode 的值，中文名和旧值 safe 也认）；None = 不单独设，见 Space.effective_mode
    permission: str | None = None
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
    description: str = ""  # 这个空间负责什么：指挥台的调度者靠它决定把任务派给谁
    executor: str = "simpleagent"  # 只管谁跑：simpleagent | claude-code | opencode
    profile: str = "default"  # executor=simpleagent 时的模型
    cli_model: str | None = None  # 外部 CLI 的模型 preset；None = 用本机默认配置
    # 权限模式：read-only / workspace / full（外部 CLI 只有前后两档）。
    # None = 没单独设过：内置执行者跟 config.toml 的 [permissions].mode，外部 CLI 按只读
    permission: str | None = None
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
        permission = normalize_permission(spec.permission)
        validate_executor(
            spec.executor,
            cli_model=spec.cli_model,
            permission=permission,
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
            permission=permission,
            cwd=spec.cwd,
            created_at=created,
            last_opened_at=created,
            generic=GenericConfig(tmp_dir="auto") if spec.kind == "generic" else None,
            agent=agent,
            verify=verify,
        )

    def effective_mode(self, default: Mode) -> Mode:
        """这个空间实际生效的权限模式。default 是 config.toml 的 [permissions].mode。

        没单独设过的：内置执行者跟配置默认；外部 CLI 按只读——它的审批卡管不到，宁可少给。
        """
        if self.permission:
            return Mode(self.permission)
        return default if self.executor == "simpleagent" else Mode.READ_ONLY

    def to_dict(self) -> dict[str, Any]:
        """整份空间定义序列化为可 JSON 化的 dict（嵌套 dataclass 一并展开）。"""
        return asdict(self)
