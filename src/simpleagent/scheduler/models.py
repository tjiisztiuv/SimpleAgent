"""定时任务的定义：schedules.toml 里的一个 [jobs.<name>]，以及它下次什么时候触发。

这里只有数据和计算，不碰文件、不跑 agent：手写的 schedules.toml、以后的 `schedule_add`
工具、工作台界面都往同一份定义里写，共用这一个模型和同一套校验。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 任务名要当日志目录名和命令行参数用，只收这几种字符；中文名写进 title
JOB_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# 没写 cwd 的任务在 <home>/jobs/<name>/ 里跑：Policy 只允许写工作目录，等于自带沙箱
JOBS_DIRNAME = "jobs"

NotifyOn = Literal["always", "error", "never"]


class Job(BaseModel):
    """schedules.toml 里的一个 [jobs.<name>]。"""

    # 字段拼错直接报错：`alowed_tools` 被静默忽略的话，任务会以「什么都不许做」跑起来
    model_config = ConfigDict(extra="forbid")

    name: str  # 由表名注入，不在 toml 里写
    cron: str  # 5 段：分 时 日 月 周（本机时区），或 @daily 这类别名
    prompt: str
    title: str = ""  # 列表和通知里显示的名字；不写就用 name
    enabled: bool = True
    profile: str | None = None  # None = config.toml 的 default_profile
    cwd: str | None = None  # None = <home>/jobs/<name>/；存不存在到运行时再查
    # 无人值守时允许自动执行的工具，和 `sa run --allow` 同义；不写就只能用只读工具
    allowed_tools: list[str] = Field(default_factory=list)
    notify: list[str] | None = None  # 结论发到哪些渠道；None = 用全局默认渠道
    notify_on: NotifyOn = "always"  # always / error（只在出错时通知）/ never
    timeout: int = Field(900, ge=10, le=86_400)  # 秒，超时就取消这次运行
    max_steps: int | None = Field(None, ge=1)  # None = 用 config.toml 的 max_steps

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not JOB_NAME_RE.fullmatch(value):
            raise ValueError(
                "任务名只能用字母、数字、_ 和 -，以字母或数字开头，最长 64 个字符；中文名写进 title"
            )
        return value

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, value: str) -> str:
        value = " ".join(value.split())
        # 拒绝 6 段：第 6 段是什么各家不统一（croniter 当秒放最后，Quartz 放最前），
        # 同一行字两种理解本身就危险；个人自动化用分钟粒度也够了
        shape_ok = value.startswith("@") or len(value.split()) == 5
        if not shape_ok or not croniter.is_valid(value):
            raise ValueError(
                f"不是合法的 cron 表达式：{value!r}"
                "（5 段：分 时 日 月 周，或 @hourly / @daily / @weekly / @monthly）"
            )
        return value

    @field_validator("prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt 不能为空")
        return value

    @property
    def label(self) -> str:
        return self.title or self.name

    def next_fire(self, after: datetime) -> datetime:
        """严格晚于 after 的下一次触发时间。after 要带时区，结果和它同一个时区。"""
        return croniter(self.cron, after).get_next(datetime)

    def workdir(self, home: Path) -> Path:
        """这个任务在哪个目录里跑。"""
        return Path(self.cwd).expanduser() if self.cwd else home / JOBS_DIRNAME / self.name
