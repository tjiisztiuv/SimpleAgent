"""配置加载：~/.simpleagent/config.toml → pydantic 模型。

数据目录可用环境变量 SIMPLEAGENT_HOME 覆盖（测试时指向临时目录）。
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


def home_dir() -> Path:
    return Path(os.environ.get("SIMPLEAGENT_HOME") or Path.home() / ".simpleagent").expanduser()


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


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str
    profiles: dict[str, Profile]
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    show_reasoning: bool = True
    # 一轮对话里最多请求模型几次，防止模型无限调用工具
    max_steps: int = Field(20, ge=1)
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    panel: PanelConfig = Field(default_factory=PanelConfig)

    @model_validator(mode="after")
    def _check_default_profile(self) -> Config:
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile '{self.default_profile}' 不在 profiles 中")
        return self

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
