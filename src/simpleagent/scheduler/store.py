"""schedules.toml 的读取。

和 config.toml 的「写错一处整个文件都不能用」不同，这里**按任务隔离错误**：
每个 [jobs.x] 单独校验，失败的放进 errors，其余照常加载。改一个任务时手滑，
不该让明早的早报也跟着停掉。只有 TOML 语法错、顶层结构不对这类文件级问题，
才让整个文件不可用（抛 ScheduleError）。

单独一个文件、不放进 config.toml：schedules.toml 以后会被工具程序化改写，
config.toml 只由人手写——工具写坏了也碰不到 profiles。
"""

from __future__ import annotations

import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from pydantic import ValidationError

from simpleagent.config import home_dir
from simpleagent.scheduler.models import Job

SCHEDULES_FILENAME = "schedules.toml"


class ScheduleError(Exception):
    """文件级错误：整个 schedules.toml 都不可用。"""


@dataclass(frozen=True)
class Schedules:
    jobs: dict[str, Job] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # 任务名 → 为什么没加载


def schedules_path() -> Path:
    return home_dir() / SCHEDULES_FILENAME


def _brief(error: ValidationError) -> str:
    """把 pydantic 的报错压成一行。不带 input_value，和 load_config 同一个口径。"""
    parts = []
    for err in error.errors(include_url=False, include_input=False):
        where = ".".join(map(str, err["loc"]))
        message = err["msg"].removeprefix("Value error, ")
        parts.append(f"{where}：{message}" if where else message)
    return "；".join(parts)


def load_schedules(
    path: Path | None = None, *, profiles: Collection[str] | None = None
) -> Schedules:
    """读 schedules.toml。文件不存在 = 没有任务，不算错。

    给了 profiles（config.toml 里的 profile 名）时，引用了不存在的 profile 的任务也进 errors：
    否则要等到凌晨触发时才发现跑不起来。
    """
    path = path or schedules_path()
    if not path.exists():
        return Schedules()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ScheduleError(f"schedules.toml 语法错误：{path}\n{e}") from e
    except OSError as e:
        raise ScheduleError(f"读不了 schedules.toml：{path}\n{e}") from e
    # 顶层写错（比如 [job.x] 少了个 s）要大声报出来，否则所有任务都会悄悄消失
    unknown = sorted(set(data) - {"jobs"})
    if unknown:
        raise ScheduleError(
            f"schedules.toml 顶层只能有 [jobs.<名字>]，多出来：{', '.join(unknown)}\n{path}"
        )
    tables = data.get("jobs", {})
    if not isinstance(tables, dict):
        raise ScheduleError(f"schedules.toml 里的 jobs 应该写成 [jobs.<名字>] 表：{path}")

    jobs: dict[str, Job] = {}
    errors: dict[str, str] = {}
    for name, spec in tables.items():
        if not isinstance(spec, dict):
            errors[name] = f"应该是一个表 [jobs.{name}]"
            continue
        try:
            job = Job.model_validate({**spec, "name": name})
        except ValidationError as e:
            errors[name] = _brief(e)
            continue
        if profiles is not None and job.profile is not None and job.profile not in profiles:
            errors[name] = f"profile '{job.profile}' 不在 config.toml 的 profiles 里"
            continue
        jobs[name] = job
    return Schedules(jobs, errors)


def example_schedules() -> str:
    return resources.files("simpleagent").joinpath("schedules.example.toml").read_text("utf-8")


def init_schedules(path: Path | None = None) -> Path:
    """生成带注释的 schedules.toml 模板；已存在时报错，不覆盖。"""
    path = path or schedules_path()
    if path.exists():
        raise ScheduleError(f"定时任务文件已存在：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example_schedules(), encoding="utf-8")
    return path
