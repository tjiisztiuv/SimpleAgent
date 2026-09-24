"""REPL 的斜杠命令补全：输入 / 开头的内容时按 Tab，补命令名、技能名和部分命令的第一个参数。

用标准库 readline 的补全回调，不引入 prompt_toolkit：要按 Tab 才出候选，但输入层还是 input()，
审批提示、多行输入、测试注入的 input_fn 都不用动。
macOS 上的 Python（包括 uv 装的）readline 多半是 libedit 包装的：绑定 Tab 的写法不一样，
按两下 Tab 列候选时也只显示名字，说明要看 /help。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

try:
    import readline
except ImportError:  # pragma: no cover  Windows 上没有 readline，补全就不装
    readline = None


class SlashCompleter:
    """readline 的补全回调。候选在会话开始时定下来：技能清单、profile 在会话里本来就不变。"""

    def __init__(self, names: Iterable[str], args: Mapping[str, Iterable[str]] | None = None):
        self.names = sorted(set(names))  # 不带 /；内置命令和同名技能只留一个
        self.args = {name: sorted(set(values)) for name, values in (args or {}).items()}
        self._matches: list[str] = []

    def candidates(self, line: str) -> list[str]:
        """光标前的整行 → 候选词。候选替换的是光标所在的那个词（以空格分隔）。"""
        if not line.startswith("/"):
            return []
        name, sep, rest = line[1:].partition(" ")
        if not sep:  # 还在输入命令名
            return [f"/{item}" for item in self.names if item.startswith(name)]
        if " " in rest:  # 只补第一个参数
            return []
        return [item for item in self.args.get(name, []) if item.startswith(rest)]

    def complete(self, text: str, state: int) -> str | None:
        """readline 的协议：同一次 Tab 里 state 从 0 往上调，返回 None 表示没有更多候选。

        state=0 时算好整份候选。不用 text 自己匹配：它只是光标所在的那个词，
        看不出前面是哪个命令，所以取整行。
        """
        if state == 0:
            line = readline.get_line_buffer()[: readline.get_endidx()] if readline else text
            self._matches = self.candidates(line)
            # 只有一个候选时补上空格，接着就能输入参数。Python 的 readline 模块把
            # 「补全后自动追加的字符」设成了空，GNU readline 和 libedit 都不会自己加
            if len(self._matches) == 1:
                self._matches[0] += " "
        return self._matches[state] if state < len(self._matches) else None


def install_completer(completer: SlashCompleter) -> bool:
    """接到 readline 上；没有 readline 时什么都不做，返回 False。改的是进程级的全局状态。"""
    if readline is None:
        return False
    readline.set_completer(completer.complete)
    # 默认的分隔符里有 /，不去掉的话输入 /co 时 readline 交给我们的词只有 co
    readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    return True
