"""技能（Skills）：按 SKILL.md 开放标准（agentskills.io）发现和按需加载。

    <技能目录>/
      weekly-report/
        SKILL.md            frontmatter（name、description）+ 操作说明
        scripts/collect.py  可选：脚本、参考资料、模板……

核心是**渐进式披露**，分三层，越往后越贵、越少用到：

1. 名字 + 描述：会话开始时全部放进 system prompt，每个技能一两行
2. SKILL.md 正文：模型判断任务对得上某个技能时，调用 `load_skill` 才读进上下文
3. 目录里的其他文件：`load_skill` 只列清单，模型需要时自己 `read_file` / `bash`

所以装 50 个技能，常驻 prompt 的也只多 50 行；而模型要不要用、用哪个，靠的正是那一行描述。

找技能的地方（先找到的优先，同名的后来者被忽略并在 /skills 里标出来）：

1. 个人技能：数据目录下的 `skills/`（`~/.simpleagent/skills/`）
2. `[skills] dirs` 里的目录：相对路径按项目根目录到 cwd 的每一层解析（默认 `.agents/skills`、
   `.claude/skills`，为 Claude Code 写的技能直接能用），绝对路径和 `~` 照原样

个人的排在项目的前面：clone 下来的仓库不能用同名技能顶替你自己的。

除了模型自己加载，用户也可以在 REPL 里敲 `/技能名 补充说明` 直接调用（`expand_command`）。
frontmatter 写了 `disable-model-invocation: true` 的技能不进 prompt，只能这样手动调用
（比如部署这类要人拍板的流程）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from simpleagent.knowledge.frontmatter import FrontmatterError, is_true, split_frontmatter
from simpleagent.knowledge.instructions import project_chain
from simpleagent.tools.base import Tool, ToolContext, ToolError

SKILL_FILENAME = "SKILL.md"
SKILLS_DIRNAME = "skills"
# 标准要求小写字母、数字、连字符；放宽一点，大写和下划线也收（别的工具写的技能不一定守规矩）
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
DESCRIPTION_MAX = 1024  # 标准里 description 的上限
LISTING_MAX_CHARS = 8000  # 技能列表进 system prompt 的总字数；超出的只列名字
FILE_MAX_BYTES = 512 * 1024  # SKILL.md 超过这么大就不认：多半不是技能说明
RESOURCES_MAX = 50  # load_skill 列出的附带文件个数上限
RESOURCES_SCAN_MAX = 1000  # 最多扫这么多个文件就停
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv"})
ARGUMENTS = "$ARGUMENTS"  # 正文里的占位符，/技能名 后面的文字替换进来（和 Claude Code 一致）


class SkillError(Exception):
    """技能写坏了（没有 frontmatter、缺 description……）或读不出来。"""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # SKILL.md
    root: Path  # 在哪个技能目录下找到的
    model_invocable: bool = True

    @property
    def dir(self) -> Path:
        return self.path.parent

    def body(self) -> str:
        """SKILL.md 去掉 frontmatter 的正文。每次都从磁盘读：改了技能不用重开会话。"""
        try:
            _, body = split_frontmatter(self.path.read_text(encoding="utf-8"))
        except (OSError, FrontmatterError) as e:
            raise SkillError(f"读取技能 {self.name} 失败（{self.path}）：{e}") from e
        return body

    def resources(self) -> list[str]:
        """技能目录下除 SKILL.md 以外的文件（相对路径）。跳过隐藏的和常见的依赖、缓存目录。"""
        files: list[str] = []
        for directory, subdirs, names in os.walk(self.dir):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and d not in SKIP_DIRS)
            base = Path(directory)
            files += [
                (base / name).relative_to(self.dir).as_posix()
                for name in names
                if not name.startswith(".") and base / name != self.path
            ]
            if len(files) > RESOURCES_SCAN_MAX:  # 技能目录里不该有这么多文件，不再往下找
                break
        files.sort()
        if len(files) > RESOURCES_MAX:
            return [*files[:RESOURCES_MAX], f"…还有 {len(files) - RESOURCES_MAX} 个"]
        return files

    def render(self, body: str | None = None) -> str:
        """交给模型的技能全文：正文 + 所在目录 + 附带文件清单。"""
        body = self.body() if body is None else body
        text = f'<skill name="{self.name}" dir="{self.dir}">\n{body.strip()}\n</skill>'
        if resources := self.resources():
            text += (
                "\n\n技能目录下的其他文件"
                "（路径相对上面的 dir；需要时用 read_file 读、用 bash 运行）：\n"
                + "\n".join(f"- {item}" for item in resources)
            )
        return text


@dataclass
class SkillCatalog:
    skills: dict[str, Skill] = field(default_factory=dict)
    errors: list[tuple[Path, str]] = field(default_factory=list)  # 写坏了、没加载的
    shadowed: list[Skill] = field(default_factory=list)  # 和先找到的同名，被忽略的
    roots: list[Path] = field(default_factory=list)  # 找过的目录（存在的）

    @property
    def listed(self) -> list[Skill]:
        """模型能看到、能加载的技能，按名字排序：顺序固定，system prompt 才稳定。"""
        return sorted(
            (skill for skill in self.skills.values() if skill.model_invocable),
            key=lambda skill: skill.name,
        )

    def prompt_section(self) -> str:
        """system prompt 里的「技能」一节：每个技能一行名字加描述。没有技能返回空串。"""
        skills = self.listed
        if not skills:
            return ""
        lines: list[str] = []
        used = 0
        for skill in skills:
            description = " ".join(skill.description.split())
            if len(description) > DESCRIPTION_MAX:
                description = description[:DESCRIPTION_MAX] + "…"
            line = f"- {skill.name}：{description}"
            if used + len(line) > LISTING_MAX_CHARS:
                line = f"- {skill.name}"  # 预算用完了：只列名字，模型还知道有这么个技能
            lines.append(line)
            used += len(line)
        return (
            "# 技能\n\n"
            "技能（skill）是写好的操作说明，每个对应一类任务。下面只列了名字和适用场景："
            "任务和某个技能的描述对得上时，先用 load_skill 加载它的完整说明，再照着做；"
            "对不上的不要加载。\n\n" + "\n".join(lines)
        )

    def tool(self) -> Tool | None:
        """load_skill 工具；模型一个技能都看不到时返回 None（不往请求里塞没用的工具）。"""
        if not self.listed:
            return None

        async def load_skill(args: LoadSkillArgs, ctx: ToolContext) -> str:
            skill = self.skills.get(args.name.strip())
            if skill is None or not skill.model_invocable:
                names = "、".join(item.name for item in self.listed)
                raise ToolError(f"没有名为 {args.name} 的技能。可用的技能：{names}")
            try:
                return skill.render()
            except SkillError as e:
                raise ToolError(str(e)) from e

        return Tool(
            name="load_skill",
            description=(
                "加载一个技能的完整说明（SKILL.md 正文）和它目录里的文件清单。"
                "任务和 system prompt 里技能列表的某一项对得上时，先调用它再动手。"
            ),
            args_model=LoadSkillArgs,
            fn=load_skill,
        )

    def expand_command(self, text: str) -> str | None:
        """用户输入 `/技能名 补充说明` 时，换成交给模型的完整请求；不是技能返回 None。

        第一行保留用户原话（会话标题、历史都好认），后面接技能全文。正文里有 $ARGUMENTS 的，
        把补充说明替换进去；没有的话，模型从第一行就能看到补充说明。读不出技能抛 SkillError。
        """
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped[1:].split(maxsplit=1)
        skill = self.skills.get(parts[0]) if parts else None
        if skill is None:
            return None
        arguments = parts[1].strip() if len(parts) > 1 else ""
        body = skill.body().replace(ARGUMENTS, arguments)
        return (
            f"{stripped}\n\n"
            f"（用户用 /{skill.name} 调用了技能，下面是它的完整说明，按说明完成上面的请求）\n\n"
            f"{skill.render(body)}"
        )


class LoadSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="技能名，见 system prompt 里的技能列表")


def skill_roots(dirs: list[str], home: Path, cwd: Path) -> list[Path]:
    """要找技能的目录，按优先级排好：个人的 → 配置里的（相对路径按项目根到 cwd 每层展开）。"""
    roots = [home / SKILLS_DIRNAME]
    chain = project_chain(cwd)
    for entry in dirs:
        path = Path(entry).expanduser()
        if path.is_absolute():
            roots.append(path)
        else:
            roots += [directory / path for directory in chain]
    return roots


def discover_skills(roots: Iterable[Path]) -> SkillCatalog:
    """扫描每个目录的直接子目录，有 SKILL.md 的就是一个技能。同名的先到先得。"""
    catalog = SkillCatalog()
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        key = root.resolve()
        if key in seen:  # 同一个目录经不同路径出现两次（符号链接、cwd 就是项目根）
            continue
        seen.add(key)
        catalog.roots.append(root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            path = child / SKILL_FILENAME
            if child.name.startswith(".") or not path.is_file():
                continue
            try:
                skill = load_skill_file(path, root)
            except SkillError as e:
                catalog.errors.append((path, str(e)))
                continue
            if skill.name in catalog.skills:
                catalog.shadowed.append(skill)
            else:
                catalog.skills[skill.name] = skill
    return catalog


def load_skill_file(path: Path, root: Path) -> Skill:
    """读一个 SKILL.md 的 frontmatter。正文此时不留：用到时再读。"""
    try:
        if path.stat().st_size > FILE_MAX_BYTES:
            raise SkillError(f"文件太大（超过 {FILE_MAX_BYTES // 1024} KB）")
        fields, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, FrontmatterError) as e:
        raise SkillError(str(e)) from e
    if not fields:
        raise SkillError("缺少 frontmatter：文件开头要有 --- 包起来的 name 和 description")
    name = fields.get("name", "").strip() or path.parent.name
    if not NAME_RE.fullmatch(name):
        raise SkillError(f"技能名 {name!r} 不合法：只能用字母、数字、- 和 _，最长 64 个字符")
    description = fields.get("description", "").strip()
    if not description:
        raise SkillError("缺少 description：模型靠它判断什么时候该用这个技能")
    return Skill(
        name=name,
        description=description,
        path=path,
        root=root,
        model_invocable=not is_true(fields.get("disable-model-invocation")),
    )
