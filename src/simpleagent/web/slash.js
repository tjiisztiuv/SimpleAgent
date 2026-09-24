/* 输入框的 / 补全：看光标前的文字，决定弹不弹菜单、列哪些候选。
   只算候选，不碰 DOM（渲染和按键在 app.js），node 里能直接 require 来测（tests/serve/test_slash.py）。 */
(function (root) {
  "use strict";

  /* 前缀匹配的排在前面，其次是名字里包含输入的；同一档里保持传进来的顺序（内置命令在技能前）。 */
  function rank(entries, q) {
    const prefix = [];
    const inside = [];
    for (const e of entries) {
      const name = e.name.toLowerCase();
      if (name.startsWith(q)) prefix.push(e);
      else if (q && name.includes(q)) inside.push(e);
    }
    return [...prefix, ...inside];
  }

  /**
   * before：光标之前的全部文字。只认第一行开头的 /，和发送时的判断（text.startsWith("/")）一致。
   * commands：[{ name, args, desc }]，args 为空表示不带参数、选中就执行
   * skills：[{ name, description }]；profiles：profile 名字的列表；current：当前 profile
   * 返回 null 表示不弹；否则 items 里每项：
   *   label / hint / desc 显示用；value 是选中后输入框里的内容（替换光标前的全部文字）；
   *   submit：按 Enter 选中后是否直接发送（不需要再补参数的）
   */
  function slashMenu(before, { commands = [], skills = [], profiles = [], current = "" } = {}) {
    let m = /^\/(\S*)$/.exec(before);
    if (m) {
      const q = m[1].toLowerCase();
      const builtin = new Set(commands.map((c) => c.name));
      const entries = [
        ...commands.map((c) => ({
          name: c.name,
          item: {
            label: `/${c.name}`,
            hint: c.args || "",
            desc: c.desc || "",
            tag: "",
            value: c.args ? `/${c.name} ` : `/${c.name}`,
            submit: !c.args,
          },
        })),
        // 和内置命令同名的技能调不到（发送时内置命令先处理），不列
        ...skills.filter((s) => !builtin.has(s.name)).map((s) => ({
          name: s.name,
          item: {
            label: `/${s.name}`,
            hint: "[补充说明]",
            desc: s.description || "",
            tag: "技能",
            value: `/${s.name} `,
            submit: false,
          },
        })),
      ];
      const items = rank(entries, q).map((e) => e.item);
      return items.length ? { items } : null;
    }
    m = /^\/model +(\S*)$/.exec(before);
    if (m) {
      const q = m[1].toLowerCase();
      const items = rank(profiles.map((p) => ({ name: p })), q).map((e) => ({
        label: e.name,
        hint: "",
        desc: e.name === current ? "当前" : "",
        tag: "",
        value: `/model ${e.name}`,
        submit: true,
      }));
      return items.length ? { items } : null;
    }
    return null;
  }

  root.slashMenu = slashMenu;
  if (typeof module !== "undefined" && module.exports) module.exports = { slashMenu };
})(typeof window !== "undefined" ? window : globalThis);
