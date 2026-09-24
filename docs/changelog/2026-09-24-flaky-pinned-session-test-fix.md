# 修复 test_pinned_session_stays_on_top 在全量测试里的偶发失败（flaky）

- 日期：2026-09-24
- 对比基线：`3e780ae`（Merge pull request #9 from tjiisztiuv/claude/cross-space-task-dispatch-33385a）
- 对应里程碑：无（测试修复）

## 功能变化

- 修复：`tests/serve/test_web.py::test_pinned_session_stays_on_top` 在全量 `uv run pytest` 里偶发失败（约 5 次全量跑失败 1 次，单独跑该测试不会复现）。

原因：测试连续创建 6 个会话，`SessionMeta.updated_at`（见 `spaces/models.py` 的 `_now`）只精确到毫秒，几个会话经常落在同一毫秒内。`SpaceStore.list_sessions` 按 `updated_at` 倒序排列，`updated_at` 打平（相同）时退化为按 `*.meta.json` 文件名的稳定排序，而同一毫秒内创建的会话，文件名（会话 id）只差 `new_id` 末尾的随机 hex，与创建先后顺序无关。于是最早创建的 `old` 会话不一定被挤出「最近 5 条」，测试里 `old.id not in listed` 这条断言会随机失败。用同样的 store 调用离线复现 500 次：约一半出现同毫秒打平，其中 121 次（约 24%）`old` 仍留在前 5 条里，即整体约 24% 概率触发这条失败断言（对应「全量跑约 5 次失败 1 次」的量级）。

修法：只改测试本身，不改产品代码——每创建一个新会话前 `time.sleep(0.002)`，让毫秒级截断的时间戳在至少 1ms 后必然变化，使这 6 个会话的 `updated_at` 严格递增，从而不再依赖打平时的文件名顺序。断言意图不变：置顶前 `old` 不在最近 5 条里，置顶后 `old` 排第一。

为什么不改产品代码：曾尝试给 `SpaceStore.list_sessions` 的排序键加上 `(updated_at, created_at, id)` 这样的次级排序，同样跑 500 次复现仍有 139 次失败——因为同一毫秒内创建的会话，`created_at` 和 id 的毫秒前缀部分也相同，唯一有区分度的只剩随机 hex，次级排序键救不了这种情况。而在真实使用中，用户通过界面创建/修改会话不会在同一毫秒内连续发生多次，同一份磁盘状态下的列表顺序本身是稳定的，这个打平行为算不上产品层面的 bug，因此判断只需要修测试。

## 函数级改动

`src/` 下没有任何改动。

## 配置与依赖

无。

## 测试

- 修改：`tests/serve/test_web.py`（`test_pinned_session_stays_on_top`：每次创建会话前加 `time.sleep(0.002)`，避免多个会话的 `updated_at` 落在同一毫秒导致排序打平）
- 测试结果：
  - 用同样的 store 调用离线复现原问题 500 次：修复前约一半出现同毫秒打平，121 次（约 24%）导致断言会失败；修复后 500 次复现 0 次打平、0 次失败。
  - `test_pinned_session_stays_on_top` 单独连续跑 50 次，全部通过。
  - 全量 `uv run pytest`（在本次改动、即基线 `3e780ae` + 这一处测试修复上）连续跑 5 次，均为 `718 passed`。
  - `uv run ruff check`：All checks passed。
  - `uv run ruff format --check`：167 files already formatted。

## 相关笔记

无
