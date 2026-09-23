"""配置加载：~/.simpleagent/config.toml → pydantic 模型。

数据目录可用环境变量 SIMPLEAGENT_HOME 覆盖（测试时指向临时目录）；从源码仓库跑时默认
~/.simpleagent-dev，跟日常安装的 sa 分开。
API key 不写进配置文件，只记录环境变量名；值从环境变量或 <home>/.env 读取。
"""

from __future__ import annotations

import os
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

CONFIG_FILENAME = "config.toml"
ENV_FILENAME = ".env"
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

DEFAULT_SYSTEM_PROMPT = "你是 SimpleAgent，一个运行在用户本地电脑上的个人助手。回答简洁、准确。"


class ConfigError(Exception):
    pass


def dev_checkout(module_file: str | Path = __file__) -> Path | None:
    """从源码仓库跑（`uv run sa` / editable 安装）时返回仓库根目录；装成快照时返回 None。

    判据：<仓库>/src/simpleagent/config.py 往上两级有 pyproject.toml 和 .git（worktree 里
    .git 是文件）。装进 site-packages 的快照往上两级是 lib/python3.x，两样都没有。
    """
    root = Path(module_file).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / ".git").exists():
        return root
    return None


def home_dir() -> Path:
    """数据目录：SIMPLEAGENT_HOME > 开发模式的 ~/.simpleagent-dev > ~/.simpleagent。

    开发时和日常用的 sa 装在同一台机器上，默认分开放，免得测试数据混进日常数据。
    """
    if env := os.environ.get("SIMPLEAGENT_HOME"):
        return Path(env).expanduser()
    return Path.home() / (".simpleagent-dev" if dev_checkout() else ".simpleagent")


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """解析 KEY=VALUE 格式的 .env 文件（支持注释、export 前缀和引号）。

    只返回字典，不写入 os.environ：后续工具启动的子进程不会继承这些 key。
    """
    path = path or home_dir() / ENV_FILENAME
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


class Quirks(BaseModel):
    """各家 OpenAI 兼容实现之间的差异。"""

    model_config = ConfigDict(extra="forbid")

    # 流式 delta 里思考内容所在的字段；None 表示不解析
    reasoning_field: str | None = "reasoning_content"
    # 历史里的思考内容是否回传：none=全部去掉，current_turn=只回传最后一条 user 之后的，all=全部回传
    reasoning_echo: Literal["none", "current_turn", "all"] = "none"
    # 是否支持 stream_options.include_usage（不支持的服务会报错或忽略）
    stream_usage: bool = True
    # 是否允许一次返回多个 tool_calls；false 时显式发送 parallel_tool_calls=false
    parallel_tool_calls: bool = True


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    api_key_env: str | None = None
    context_window: int = 128_000
    max_tokens: int | None = None
    temperature: float | None = None
    timeout: float = 600.0
    max_retries: int = 2
    # 厂商私有参数原样合并进请求体，用来试验新 feature（如思考开关）
    extra_body: dict[str, Any] = Field(default_factory=dict)
    quirks: Quirks = Field(default_factory=Quirks)

    @field_validator("api_key_env")
    @classmethod
    def _check_env_name(cls, value: str | None) -> str | None:
        # 常见误用是把 key 本身填进来；报错信息里不能回显这个值
        if value is not None and not ENV_NAME_RE.fullmatch(value):
            raise ValueError(
                "要填环境变量名（如 DEEPSEEK_API_KEY），不是 key 本身；"
                f"key 写到 {home_dir() / ENV_FILENAME}，格式 DEEPSEEK_API_KEY=sk-..."
            )
        return value

    def api_key(self) -> str:
        if self.api_key_env is None:
            return "not-needed"  # 本地服务（如 Ollama）不校验 key，但 SDK 要求非空
        # 环境变量优先，方便临时覆盖 .env 里的值
        key = os.environ.get(self.api_key_env) or read_env_file().get(self.api_key_env)
        if not key:
            raise ConfigError(
                f"找不到 {self.api_key_env}：设置这个环境变量，"
                f"或写到 {home_dir() / ENV_FILENAME}（格式 {self.api_key_env}=sk-...）"
            )
        return key


class TraceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # 是否把原始 SSE chunk 也写进 trace（体积大，学习流式协议时打开）
    raw_chunks: bool = False


class ToolOutputConfig(BaseModel):
    """工具结果回给模型之前的大小上限；超出的部分只留开头，完整内容落盘。"""

    model_config = ConfigDict(extra="forbid")

    max_chars: int = Field(30_000, ge=1000)  # 约 8k token
    max_lines: int = Field(500, ge=10)


# 过长的工具输出完整保存在 <home>/<TOOL_OUTPUT_DIRNAME>/
TOOL_OUTPUT_DIRNAME = "tool_outputs"


class ContextConfig(BaseModel):
    """上下文管理：快满的时候怎么腾地方。比例都相对「输入上限」= 窗口 − 给输出留的余量。"""

    model_config = ConfigDict(extra="forbid")

    # 占到这个比例就清理旧工具结果（换成占位符，原文落盘）；0 = 关闭
    clear_at: float = Field(0.6, ge=0, le=1)
    # 最近几个工具结果保留原样
    keep_tool_results: int = Field(3, ge=1)
    # 清理之后还占到这个比例，就调 LLM 把早期对话压成摘要（保留最近约 1/4 的原文）；0 = 关闭
    compact_at: float = Field(0.8, ge=0, le=1)


class InstructionsConfig(BaseModel):
    """项目指令（M7）：会话开始时把 AGENTS.md 读进 system prompt。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # 每个目录按顺序取第一个存在的文件：没有 AGENTS.md 的目录用 CLAUDE.md（给 Claude Code 写的）
    filenames: list[str] = Field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md"], min_length=1)
    # 所有指令文件加起来进 system prompt 的字数上限，超出的截掉并告诉模型原文在哪
    max_chars: int = Field(32_000, ge=1000)


class MemoryConfig(BaseModel):
    """长期记忆（M7）：<home>/memory/，MEMORY.md 索引进 system prompt，正文按需读。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # memory_write / memory_delete 要不要先确认。记忆会进之后每个会话的 system prompt，
    # 默认让人看一眼；sa run 里没人确认，默认拒绝（要写就 --allow memory_write）
    confirm_writes: bool = True


class SkillsConfig(BaseModel):
    """技能（M7）：SKILL.md 的名字和描述进 system prompt，正文由 load_skill 按需加载。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # 除了 <home>/skills/ 之外还从哪些目录找。相对路径按项目根目录到 cwd 的每一层解析；
    # 绝对路径（含 ~）照原样，比如加上 "~/.claude/skills" 就能用 Claude Code 的个人技能
    dirs: list[str] = Field(default_factory=lambda: [".agents/skills", ".claude/skills"])


class DebugConfig(BaseModel):
    """交互时的 debug 输出：显示 API 调用和工具调用的详细过程。

    输出走 stderr，和正文分开，可以 `2>debug.log` 单独存一份。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # on 档再加：每轮的消息清单（role + 字符数）、请求体里的非默认开关。只打结构不打正文。
    verbose: bool = False
    # verbose 档再加：实际发给模型的消息正文（第 2 轮起只打新增的）和模型返回的结构。
    # 会把系统提示、历史消息、工具结果打到 stderr，日志别随手外传。隐含 verbose。
    full: bool = False
    # full 档里每段正文的字符上限，0 = 不截断
    body_chars: int = Field(600, ge=0)


class PanelConfig(BaseModel):
    """控制面板。"""

    model_config = ConfigDict(extra="forbid")

    # 消息点开后多久移进归档；没点开过的不会自动归档
    archive_after_minutes: int = Field(30, ge=1)


# MCP server 名要拼进工具名 mcp__<server>__<tool>：不能有连续的下划线，也不能太长
MCP_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*")
MCP_SERVER_NAME_MAX = 24
# env 里的键名按 _ 切开后有这些段，就当成密钥：值不该写进 config.toml
_SECRET_PARTS = frozenset(
    {"KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS"}
)


def looks_secret(name: str) -> bool:
    """环境变量名看起来是不是密钥（GITHUB_TOKEN、OPENAI_API_KEY……）。按段匹配，KEYBOARD 不算。"""
    return any(part in _SECRET_PARTS for part in name.upper().split("_"))


class McpServerConfig(BaseModel):
    """一个 stdio MCP server：怎么启动、给它哪些环境变量、它的工具怎么暴露给模型。"""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    # 不是秘密的设置，原样传给 server。密钥用 env_vars，值不进 config.toml
    env: dict[str, str] = Field(default_factory=dict)
    # 要传给 server 的环境变量名；值和 profile 的 api_key_env 一样，先查环境变量再查 .env
    env_vars: list[str] = Field(default_factory=list)
    cwd: str | None = None  # server 进程的工作目录；None 继承 sa 的
    enabled: bool = True
    # eager：sa 启动时就拉起来；lazy：用上次缓存的工具清单登记工具，模型第一次调用时才启动
    start: Literal["eager", "lazy"] = "eager"
    # auto：先 server/discover 探测，不行退回 initialize；modern / legacy：只讲一代
    protocol: Literal["auto", "modern", "legacy"] = "auto"
    startup_timeout: float = Field(60.0, gt=0)  # 启动 + 列工具的总时限（npx 第一次要下载）
    tool_timeout: float = Field(60.0, gt=0)  # 单次调用的时限
    # 用 server 原本的工具名。enabled_tools 不写 = 全部；disabled_tools 从中去掉
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = Field(default_factory=list)
    # 相信 server 标的 readOnlyHint：标了只读的免确认、可并行。不信任这个 server 就关掉
    trust_annotations: bool = True
    # 按工具覆盖上面算出来的权限，比如 { write_file = "allow" }
    permissions: dict[str, Literal["allow", "ask"]] = Field(default_factory=dict)

    @field_validator("env_vars")
    @classmethod
    def _check_env_vars(cls, names: list[str]) -> list[str]:
        for name in names:
            if not ENV_NAME_RE.fullmatch(name):
                raise ValueError(
                    "要填环境变量名（如 GITHUB_TOKEN），不是值本身；"
                    f"值写到 {home_dir() / ENV_FILENAME}"
                )
        return names

    @field_validator("env")
    @classmethod
    def _check_env(cls, env: dict[str, str]) -> dict[str, str]:
        # 报错里只提键名，不回显值：值很可能就是贴进来的密钥
        for name in env:
            if looks_secret(name):
                raise ValueError(
                    f"{name} 看起来是密钥，值不要写进 config.toml："
                    f'改成 env_vars = ["{name}"]，值放环境变量或 {home_dir() / ENV_FILENAME}'
                )
        return env


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str
    profiles: dict[str, Profile]
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    show_reasoning: bool = True
    # 一轮对话里最多请求模型几次，防止模型无限调用工具
    max_steps: int = Field(20, ge=1)
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    instructions: InstructionsConfig = Field(default_factory=InstructionsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    panel: PanelConfig = Field(default_factory=PanelConfig)
    # MCP server，按配置顺序启动；工具名是 mcp__<名字>__<工具>
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_default_profile(self) -> Config:
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile '{self.default_profile}' 不在 profiles 中")
        return self

    @field_validator("mcp_servers")
    @classmethod
    def _check_mcp_server_names(
        cls, servers: dict[str, McpServerConfig]
    ) -> dict[str, McpServerConfig]:
        for name in servers:
            if len(name) > MCP_SERVER_NAME_MAX or not MCP_SERVER_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"MCP server 名 '{name}' 不合法：只能用字母、数字、- 和单个 _，"
                    f"最长 {MCP_SERVER_NAME_MAX} 个字符（它要拼进工具名 mcp__<名字>__<工具>）"
                )
        return servers

    def api_key_env_names(self) -> frozenset[str]:
        """所有 profile 用到的 key 环境变量名：工具启动子进程时要从环境里去掉。"""
        return frozenset(p.api_key_env for p in self.profiles.values() if p.api_key_env)


def config_path() -> Path:
    return home_dir() / CONFIG_FILENAME


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}\n先运行 `sa init` 生成默认配置。")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return Config.model_validate(data)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件 TOML 语法错误：{path}\n{e}") from e
    except ValidationError as e:
        # 不用 str(e)：pydantic 默认会带上 input_value，可能把误填的 key 打印出来
        details = "\n".join(
            f"- {'.'.join(map(str, err['loc']))}：{err['msg']}"
            for err in e.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"配置文件内容不合法：{path}\n{details}") from e


def example_config() -> str:
    return resources.files("simpleagent").joinpath("config.example.toml").read_text("utf-8")


def init_config(path: Path | None = None) -> Path:
    path = path or config_path()
    if path.exists():
        raise ConfigError(f"配置文件已存在：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example_config(), encoding="utf-8")
    return path
