"""权限：判定一次工具调用该不该执行，以及需要确认时找谁确认。

这里刻意把两件事分开：

- ``Policy.decide()`` 是**纯判定**：输入「这次调用碰什么」，输出 allow / ask / deny。
  它不做 IO、不认识 ``Tool`` 对象、不知道屏幕前有没有人。要问「这次能不能不问就放行」，
  问它。危险命令和目录边界都在它里面，同样的输入永远得到同样的输出，所以好测。
- ``Approver`` 是**异步询问接口**：只有 decide 给出 ask 时才被调用，负责真的去问人
  （终端读一行、客户端推一帧等回调、无人值守时直接说不行）。它不需要懂命令危不危险。

权限模式（只读 / 工作区 / 全放行）是 Policy 的一个旋钮，决定哪些调用不用问就放行。
它对应 dsh 的「沙箱模式 + 审批策略」预设：模式定下每类调用是放行、问还是拒，
「问」落到谁手里由前端挑的 Approver 决定（有人问人，无人按白名单或直接拒）。

为什么要分两层：不分开的话，每个前端都得各自实现一遍「`rm -rf /` 该拦」，REPL 记得拦、
定时任务忘了拦，就会在某个凌晨三点让 agent 自己格式化磁盘。把危险判定收进 Policy，
三个前端共用同一条底线，谁也绕不过去。

依赖方向是单向的：本模块只用标准库，不 import tools。工具各自声明
permission / scope，由注册表取出来喂给 Policy，避免两边互相引用形成循环导入。
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# 工具自带的默认等级。allow=直接执行，ask=交给审批器问人，deny=一律拒绝（连问都不问）
Permission = Literal["allow", "ask", "deny"]


class Mode(StrEnum):
    """权限模式：一个旋钮定下哪些调用不用问。

    | 调用                         | 只读 | 工作区 | 全放行 |
    |------------------------------|------|--------|--------|
    | 读文件、搜索、只读 MCP       | 放行 | 放行   | 放行   |
    | 改工作目录里的文件           | 问   | 放行   | 放行   |
    | 改工作目录外的文件           | 拒   | 拒     | 放行   |
    | bash、其他有副作用的工具     | 问   | 问     | 放行   |
    | 危险命令、被禁用的工具       | 拒   | 拒     | 拒     |

    工作区模式下 bash 仍然要问：没有 OS 沙箱，就管不住一条命令会写到哪里。
    """

    READ_ONLY = "read-only"
    WORKSPACE = "workspace"
    FULL = "full"

    @property
    def label(self) -> str:
        return MODE_LABELS[self]


MODE_LABELS = {Mode.READ_ONLY: "只读", Mode.WORKSPACE: "工作区", Mode.FULL: "全放行"}
# 一句话讲清这个模式放行什么：REPL 的 /mode、客户端的权限下拉共用
MODE_SUMMARY = {
    Mode.READ_ONLY: "改文件、跑命令都先问你",
    Mode.WORKSPACE: "工作目录里改文件不用问；跑命令要问，写到工作目录外直接拒",
    Mode.FULL: "什么都不问（危险命令照样拦）",
}


def parse_mode(text: str) -> Mode:
    """把用户敲的模式名转成 Mode：英文值和中文名都认（`workspace`、`工作区`）。"""
    value = text.strip().lower()
    for mode in Mode:
        if value in (mode.value, mode.label):
            return mode
    choices = "、".join(f"{mode.label}（{mode.value}）" for mode in Mode)
    raise ValueError(f"未知的权限模式：{text}。可选：{choices}")


@dataclass(frozen=True)
class Scope:
    """一次调用的作用范围：够用来判断能不能自动放行，就够了。

    paths 是**会被改动**的绝对路径：只读工具不需要报（它们不受工作目录边界限制），
    写工具必须报。command 只有当次要跑 shell 命令时才给。

    file_edit 表示这次调用的全部副作用就是改 paths 里的文件（write_file / edit_file），
    工作区模式只自动放行这一类。要工具显式声明而不是从「有 paths、没 command」推出来：
    以后的工具可能既改文件又有别的副作用，推断错了就是少问一次，声明漏了只是多问一次。
    """

    paths: tuple[Path, ...] = ()
    command: str | None = None
    file_edit: bool = False


@dataclass(frozen=True)
class Judgment:
    """Policy 的判定结果。reason 有两个去处：deny 时回给模型，ask 时给人看。"""

    decision: Decision
    reason: str = ""


# ------------------------------------------------------------------- 审批接口
@dataclass(frozen=True)
class ApprovalDecision:
    allow: bool
    always: bool = False  # always 由审批器自己记住，注册表不替它记账


@dataclass(frozen=True)
class ApprovalRequest:
    session_id: str
    tool_name: str
    arguments: str  # 模型给的原始 JSON 字符串
    reason: str = ""  # Policy 给的判定理由，显示给人看


class Approver(Protocol):
    """执行前的确认接口。终端 / 客户端 / 无人值守各实现一份。"""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...


class WhitelistApprover:
    """无人值守用的审批器：只放行白名单里的工具，其余一律拒绝。

    定时任务和 `sa run` 用它——半夜三点跑起来的 agent 面前没有人可以按 y。
    拒绝原因会回给模型，多数时候它会自己换个只读的办法把事情做完。
    """

    def __init__(self, allowed: Iterable[str] = ()) -> None:
        self.allowed = frozenset(allowed)

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(allow=req.tool_name in self.allowed)


# --------------------------------------------------------- bash 危险命令识别
# 按 shell 分隔符切成独立的片段：`a; b`、`a && b`、`a | b` 里的每一段单独看，
# 否则 `echo hi; rm -rf /` 会因为第一个词是 echo 而被放过。
_SEGMENT_SPLIT = re.compile(r"\s*(?:\|\||;|&&|\||\n)\s*")

# 出现就不该让模型执行的程序：破坏不可恢复，或者会让系统当场失去响应。
# bash 整体已经是 ask，所以这里列的是「问了也不该答应」的那部分。
_DENY_PROGRAMS = {
    "mkfs": "会格式化磁盘，数据不可恢复",
    "newfs": "会格式化磁盘，数据不可恢复",
    "dd": "直接读写块设备或覆盖任意文件，写错一个参数就是整盘数据",
    "fdisk": "修改分区表，整块盘的数据都会丢",
    "parted": "修改分区表，整块盘的数据都会丢",
    "diskutil": "管理磁盘和分区，误操作会抹掉整个卷",
    "shutdown": "会关闭系统",
    "reboot": "会重启系统",
    "halt": "会停止系统",
    "poweroff": "会关闭系统",
    "init": "切换系统运行级别，会重启或关机",
    "nvram": "修改固件变量，改错可能开不了机",
    "csrutil": "开关系统完整性保护，会影响整机安全策略",
    "launchctl": "装卸系统级守护进程，改错会影响开机启动项",
    "purge": "强制清空磁盘缓存，会让整台机器变卡",
}

# 必须看整条命令才能认出来的危险结构（管道喂解释器 / fork bomb / 写裸设备），
# 切成片段就认不出了，所以放在最前面匹配原文。
_RAW_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"), "把内容直接喂给 shell 执行"),
    (
        re.compile(r"\|\s*(?:sudo\s+)?(?:python3?|ruby|perl|osascript)\b"),
        "把内容直接喂给解释器执行",
    ),
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*\|[^}]*&"), "fork bomb：会耗尽系统资源直到死机"),
    (re.compile(r">\s*/dev/(?:sd|nvme|disk|rdisk)"), "把输出直接写进裸块设备，会毁掉整块盘"),
)

# sudo / env / FOO=bar 这些包裹词：剥掉之后才是真正的命令
_WRAPPERS = frozenset({"sudo", "su", "nohup", "env", "command", "time"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def segments(command: str) -> list[str]:
    """把一条命令按 `;` `&&` `||` `|` 换行切成独立的片段。"""
    return [part for part in _SEGMENT_SPLIT.split(command) if part.strip()]


def _tokens(segment: str) -> list[str]:
    """剥掉包裹词和它们自己的选项，拿到真正的命令名和参数。"""
    try:
        parts = shlex.split(segment, comments=False)
    except ValueError:  # 引号不配对之类：退化成按空白切，宁可粗也不要崩
        parts = segment.split()
    while parts and (parts[0] in _WRAPPERS or _ASSIGNMENT.match(parts[0])):
        parts.pop(0)
    # sudo -S rm -rf /：剥掉包裹词之后开头可能是它的选项
    while len(parts) > 1 and parts[0].startswith("-"):
        parts.pop(0)
    return parts


def _flags_and_targets(args: list[str]) -> tuple[list[str], list[str]]:
    """把参数分成选项和目标；`--` 之后的一律算目标。"""
    if "--" in args:
        index = args.index("--")
        return args[:index], args[index + 1 :]
    return [a for a in args if a.startswith("-")], [a for a in args if not a.startswith("-")]


def _absolute(token: str, cwd: Path) -> Path:
    """把命令里的一个路径参数转成绝对路径。

    不用 resolve()：目标常常还不存在（要新建的目录）或者要交给 shell 二次展开，
    resolve() 会报错或给出与 shell 不一致的结果。只做 ~ / $VAR 展开和 normpath。
    带通配符时按「通配符所在的目录」算：`rm -rf *` 等价于删空当前目录。
    """
    if "*" in token or "?" in token:
        head = token.split("*")[0].split("?")[0].rstrip("/")
        return _absolute(head, cwd) if head else cwd
    text = os.path.expandvars(os.path.expanduser(token))
    path = Path(text)
    return Path(os.path.normpath(str(path if path.is_absolute() else cwd / path)))


def _is_critical(target: Path, cwd: Path, home: Path) -> bool:
    """这个目标是不是「删掉就出大事」的位置。

    判的是目标本身，不是它的祖先：`rm -rf node_modules` 在项目里是常规操作，
    但 `rm -rf ~/Downloads`、`rm -rf /usr`、`rm -rf .`（删空整个项目）不行。
    """
    return target in {Path("/"), home, cwd} or target.parent in {Path("/"), home}


def _inspect_segment(segment: str, cwd: Path, home: Path) -> str | None:
    tokens = _tokens(segment)
    if not tokens:
        return None
    program = PurePosixPath(tokens[0]).name.lower()
    args = tokens[1:]
    reason = _DENY_PROGRAMS.get(program) or _DENY_PROGRAMS.get(program.split(".")[0])
    if reason:  # mkfs / mkfs.ext4 / mkfs.apfs 这类都属于同一个家族
        return f"拒绝执行 `{program}`：{reason}。这条命令请用户自己执行。"
    if program in ("rm", "mv"):
        flags, targets = _flags_and_targets(args)
        recursive = any(f in ("-r", "-R", "--recursive") for f in flags) or any(
            f.startswith("-") and not f.startswith("--") and ("r" in f or "R" in f) for f in flags
        )
        if recursive and targets:
            if any(_is_critical(_absolute(t, cwd), cwd, home) for t in targets):
                return (
                    f"拒绝执行 `{program} {' '.join(flags)} {' '.join(targets)}`："
                    "递归删/移的目标是家目录、系统根目录或整个工作目录，"
                    "一旦执行就无法撤销。这条命令请用户自己执行。"
                )
        return None
    if program in ("chmod", "chown") and args:
        flags, targets = _flags_and_targets(args)
        recursive = any(f.startswith("-") and "R" in f for f in flags)
        if recursive and any(_is_critical(_absolute(t, cwd), cwd, home) for t in targets):
            return (
                "拒绝递归修改家目录或系统根目录的权限/属主："
                "会让系统或用户数据整体不可访问。这条命令请用户自己执行。"
            )
    return None


def inspect_command(command: str, cwd: Path, home: Path | None = None) -> str | None:
    """检查一条 shell 命令，返回拒绝原因；None 表示没发现「问了也不该答应」的危险。

    尺子很窄：**只拦必然造成不可恢复损失的命令**。删错文件、装错包、push 错分支这些
    还能弥补的都不在此列，它们照常走 ask 由人决定。拦得太宽，模型会学着绕开审批，
    反而更危险。
    """
    home = home or Path.home()
    for pattern, reason in _RAW_PATTERNS:
        if pattern.search(command):
            return f"拒绝执行这条命令：{reason}。这类命令请用户自己确认后手动执行。"
    for segment in segments(command):
        if reason := _inspect_segment(segment, cwd, home):
            return reason
    return None


# ---------------------------------------------------------------------- Policy
def _resolve(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # 目录不存在或不可读时退回原样：判定走向拒绝而不是崩溃
        return Path(path)


class Policy:
    """判定一次工具调用该怎么处理。无 IO，工作目录在构造时固定。

    mode 可以在会话中途改（REPL 的 /mode、客户端的空间设置）：decide() 每次现读，
    改完从下一次工具调用起生效，不用重建 agent。

    代码里的默认模式是只读（M3 的原始行为）；产品默认用哪个模式由各入口从配置里读了传进来。
    """

    def __init__(
        self,
        cwd: Path,
        *,
        home: Path | None = None,
        allow_read_outside: bool = True,
        mode: Mode = Mode.READ_ONLY,
    ) -> None:
        self.cwd = _resolve(cwd)
        self.home = _resolve(home or Path.home())
        # 只读工具不受工作目录边界限制；写工具由 Scope.paths 里的绝对路径把关
        self.allow_read_outside = allow_read_outside
        self.mode = mode

    def decide(self, permission: Permission, scope: Scope) -> Judgment:
        # 顺序很关键：
        # - 危险命令排在模式之前：全放行也拦，它是所有模式、所有前端共用的底线。
        # - 越界检查排在默认等级之前：否则工具只要声明 permission="allow" 就能绕过工作目录边界。
        if permission == "deny":
            return Judgment(Decision.DENY, "这个工具已被配置为禁用。")
        if scope.command and (reason := inspect_command(scope.command, self.cwd, self.home)):
            return Judgment(Decision.DENY, reason)
        if self.mode is Mode.FULL:
            return Judgment(Decision.ALLOW)
        for path in scope.paths:
            if not self._inside(path):
                return Judgment(
                    Decision.DENY,
                    f"拒绝：目标路径在工作目录之外——{path}\n"
                    f"当前工作目录是 {self.cwd}，「{self.mode.label}」模式只能改这个目录里的文件。"
                    "确实要写到外面，请用户切到「全放行」模式。",
                )
        if permission == "allow":
            return Judgment(Decision.ALLOW)
        if self.mode is Mode.WORKSPACE and scope.file_edit:
            return Judgment(Decision.ALLOW)
        return Judgment(Decision.ASK)

    def _inside(self, path: Path) -> bool:
        try:
            path.relative_to(self.cwd)
        except ValueError:
            return False
        return True
