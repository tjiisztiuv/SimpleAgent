"""定时任务定义：schedules.toml 的解析、按任务隔离错误、下次触发时间、sa schedule list。"""

from datetime import datetime
from pathlib import Path

import pytest

from simpleagent.cli import main
from simpleagent.config import init_config
from simpleagent.scheduler import ScheduleError, init_schedules, load_schedules, schedules_path
from simpleagent.scheduler.models import Job

# 2026-09-25 是周五
FRIDAY_0930 = datetime(2026, 9, 25, 9, 30).astimezone()


def _write(sa_home: Path, content: str) -> Path:
    path = sa_home / "schedules.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _job(**fields) -> Job:
    return Job.model_validate({"name": "j", "cron": "0 9 * * *", "prompt": "hi", **fields})


# ----------------------------------------------------------------- 解析


def test_valid_job_with_defaults(sa_home: Path):
    _write(
        sa_home,
        """
[jobs.hn-digest]
cron = "0   9 * *  1-5"
prompt = \"\"\"
总结 HN 首页
\"\"\"
""",
    )
    schedules = load_schedules()
    assert schedules.errors == {}
    job = schedules.jobs["hn-digest"]
    assert job.name == "hn-digest"  # 表名注入成 name
    assert job.cron == "0 9 * * 1-5"  # 多余空白规整掉
    assert job.prompt == "总结 HN 首页"  # 多行字符串首尾的换行去掉
    assert job.label == "hn-digest"  # 没有 title 就用 name
    assert job.enabled and job.profile is None and job.cwd is None
    assert job.allowed_tools == [] and job.notify is None
    assert job.notify_on == "always" and job.timeout == 900 and job.max_steps is None


def test_all_fields(sa_home: Path):
    _write(
        sa_home,
        """
[jobs.nightly]
title = "夜间测试"
cron = "@daily"
prompt = "跑测试"
enabled = false
profile = "b"
cwd = "~/proj"
allowed_tools = ["bash"]
notify = ["macos", "inbox"]
notify_on = "error"
timeout = 60
max_steps = 5
""",
    )
    job = load_schedules(profiles=["a", "b"]).jobs["nightly"]
    assert job.label == "夜间测试"
    assert not job.enabled
    assert job.allowed_tools == ["bash"] and job.notify == ["macos", "inbox"]
    assert job.notify_on == "error" and job.timeout == 60 and job.max_steps == 5


def test_missing_file_means_no_jobs(sa_home: Path):
    schedules = load_schedules()
    assert schedules.jobs == {} and schedules.errors == {}


def test_toml_syntax_error_breaks_whole_file(sa_home: Path):
    path = _write(sa_home, "[jobs.a\ncron = 1\n")
    with pytest.raises(ScheduleError, match="语法错误") as info:
        load_schedules()
    assert str(path) in str(info.value)


def test_unknown_top_level_key_is_loud(sa_home: Path):
    # [job.x] 少了个 s：不报错的话所有任务都会悄悄消失
    _write(sa_home, '[job.a]\ncron = "0 9 * * *"\nprompt = "hi"\n')
    with pytest.raises(ScheduleError, match="job"):
        load_schedules()


def test_jobs_must_be_tables(sa_home: Path):
    _write(sa_home, "jobs = 1\n")
    with pytest.raises(ScheduleError, match=r"\[jobs\.<名字>\]"):
        load_schedules()


def test_one_bad_job_does_not_take_down_the_others(sa_home: Path):
    _write(
        sa_home,
        """
[jobs.good]
cron = "0 9 * * *"
prompt = "ok"

[jobs.bad-cron]
cron = "61 * * * *"
prompt = "x"

[jobs.typo]
cron = "0 9 * * *"
prompt = "x"
alowed_tools = ["bash"]

[jobs.empty-prompt]
cron = "0 9 * * *"
prompt = "   "

[jobs."早报"]
cron = "0 9 * * *"
prompt = "x"

[jobs.too-many]
cron = "0 9 * * *"
prompt = "x"
timeout = 1

[jobs]
scalar = 3
""",
    )
    schedules = load_schedules()
    assert list(schedules.jobs) == ["good"]
    errors = schedules.errors
    assert set(errors) == {"bad-cron", "typo", "empty-prompt", "早报", "too-many", "scalar"}
    assert "cron" in errors["bad-cron"] and "61 * * * *" in errors["bad-cron"]
    assert "alowed_tools" in errors["typo"]
    assert "prompt 不能为空" in errors["empty-prompt"]
    assert "title" in errors["早报"]  # 提示中文名写进 title
    assert "timeout" in errors["too-many"]
    assert "表" in errors["scalar"]
    assert "Value error" not in errors["bad-cron"]  # pydantic 的前缀去掉了


def test_unknown_profile_goes_to_errors(sa_home: Path):
    _write(
        sa_home,
        '[jobs.a]\ncron = "0 9 * * *"\nprompt = "x"\nprofile = "nope"\n'
        '[jobs.b]\ncron = "0 9 * * *"\nprompt = "x"\nprofile = "b"\n',
    )
    # 不给 profiles 时不做交叉校验
    assert set(load_schedules().jobs) == {"a", "b"}
    schedules = load_schedules(profiles={"a": 1, "b": 2})
    assert list(schedules.jobs) == ["b"]
    assert "nope" in schedules.errors["a"]


def test_example_template_loads_without_jobs(sa_home: Path):
    path = init_schedules()
    assert path == schedules_path()
    schedules = load_schedules()
    assert schedules.jobs == {} and schedules.errors == {}
    with pytest.raises(ScheduleError, match="已存在"):
        init_schedules()


def test_example_template_job_is_valid(sa_home: Path):
    """模板里注释掉的示例任务，去掉注释之后必须能直接用。"""
    init_schedules()
    text = schedules_path().read_text(encoding="utf-8")
    start = text.index("# [jobs.")
    uncommented = "\n".join(line.removeprefix("# ") for line in text[start:].splitlines())
    _write(sa_home, uncommented)
    schedules = load_schedules(profiles=["deepseek"])
    assert schedules.errors == {}
    assert list(schedules.jobs) == ["downloads-digest"]


# ----------------------------------------------------------------- cron


@pytest.mark.parametrize(
    "cron",
    ["0 9 * * 1-5 30", "* * * * * * *", "0 9 * *", "@reboot", "@nope", "", "H * * * *"],
)
def test_rejected_cron(cron: str):
    with pytest.raises(ValueError, match="cron"):
        _job(cron=cron)


@pytest.mark.parametrize("cron", ["*/5 * * * *", "0 9 * * mon-fri", "@hourly", "@weekly"])
def test_accepted_cron(cron: str):
    assert _job(cron=cron).cron == cron


def test_next_fire_skips_the_weekend():
    job = _job(cron="0 9 * * 1-5")
    assert job.next_fire(FRIDAY_0930) == datetime(2026, 9, 28, 9, 0).astimezone()


def test_next_fire_is_strictly_after():
    job = _job(cron="0 9 * * *")
    at_nine = datetime(2026, 9, 25, 9, 0).astimezone()
    assert job.next_fire(at_nine) == datetime(2026, 9, 26, 9, 0).astimezone()


def test_next_fire_alias():
    job = _job(cron="@daily")
    assert job.next_fire(FRIDAY_0930) == datetime(2026, 9, 26, 0, 0).astimezone()


def test_workdir(sa_home: Path):
    assert _job().workdir(sa_home) == sa_home / "jobs" / "j"
    assert _job(cwd="~/proj").workdir(sa_home) == Path.home() / "proj"


# ----------------------------------------------------------------- CLI


def test_cli_schedule_list(sa_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch):
    init_config()
    _write(
        sa_home,
        """
[jobs.weekday]
title = "工作日早报"
cron = "0 9 * * 1-5"
prompt = "x"
allowed_tools = ["web_fetch", "bash"]
profile = "glm"

[jobs.paused]
cron = "0 8 * * *"
prompt = "x"
enabled = false

[jobs.broken]
cron = "61 * * * *"
prompt = "x"
""",
    )
    import simpleagent.cli as cli

    real = cli.list_schedules
    monkeypatch.setattr(cli, "list_schedules", lambda: real(now=FRIDAY_0930))
    assert main(["schedule", "list"]) == 1  # 有任务加载失败
    out = capsys.readouterr().out
    assert "weekday   工作日早报" in out
    assert "0 9 * * 1-5 · 下次 09-28 周一 09:00 · 工具 web_fetch, bash · glm" in out
    assert "0 8 * * * · 已停用" in out
    assert "broken\n    ✗ cron：" in out


def test_cli_schedule_list_readonly_default(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    init_config()
    _write(sa_home, '[jobs.a]\ncron = "0 9 * * *"\nprompt = "x"\n')
    assert main(["schedule", "list"]) == 0
    assert "工具 只读工具" in capsys.readouterr().out


def test_cli_schedule_list_without_file(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["schedule", "list"]) == 0
    assert "sa init" in capsys.readouterr().out
    init_config()
    init_schedules()
    assert main(["schedule", "list"]) == 0
    assert "还没有任务" in capsys.readouterr().out


def test_cli_schedule_list_syntax_error(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    init_config()
    _write(sa_home, "[jobs.a\n")
    assert main(["schedule", "list"]) == 1
    assert "定时任务配置错误" in capsys.readouterr().err


def test_cli_init_fills_in_missing_files(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    init_config()  # 老用户：已经有 config.toml
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "配置已存在，跳过" in out
    assert "已生成定时任务模板" in out
    assert (sa_home / "schedules.toml").exists()
    # 两个都在了：和以前一样报错，不覆盖
    assert main(["init"]) == 1
    assert "都已存在" in capsys.readouterr().err
