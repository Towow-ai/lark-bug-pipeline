# 架构细节

## 数据流

```
飞书事件                队列                       修复产物
─────────              ──────                    ────────
im.message              ~/.towow/queue/           git worktree
.receive_v1             <ts>-lark-im-*.json       + branch
   │                         ▲                        │
   │ lark-oapi WS             │ JSONL append           ▼
   ▼                         │                     docs/issues/
bug_daemon.py ──atomic write─┘                     <slug>.md
   │                                                  ▲
   │ (只管接消息 + 入队)                                │ claude -p
   ▼                                                  │ --skill
[LaunchAgent #1]                                      │   lark-triage
                                                       │
                                                       │
~/.towow/queue/ ───30s poll──▶ bug_worker.py ─────────┘
                                  │
                                  │ 同一个进程继续
                                  ▼
                               claude -p
                               --skill guardian-fixer
                                  │
                                  ▼
                               gh pr create
                                  │
                                  ▼
                               GitHub PR
                                  │
                                  ▼
                              回写多维表格
```

## 进程边界

- **daemon**（LaunchAgent #1）：单进程，纯 I/O，内存可忽略。崩了 KeepAlive 30 秒重启。状态只有 WebSocket 连接，重连即恢复。
- **worker**（LaunchAgent #2）：主进程循环 poll + spawn 子进程。子进程是 `claude -p` 或 `git worktree` 或 `gh`。主进程崩了 30 秒重启；子进程崩了主进程记 FixerResult(failed) 继续下一条。

## 队列格式

`~/.towow/queue/` 下每个文件是一条待处理事件，文件名：

```
<timestamp_ns>-<source>-<8char_hash>.json
```

内容示例（IM 路径）：

```json
{
  "source": "lark-im",
  "message_id": "om_xxx",
  "chat_id": "oc_xxx",
  "sender_open_id": "ou_xxx",
  "text": "登录按钮点了没反应",
  "mentioned": ["ou_botxxx"],
  "ts": "2026-04-08T12:30:00Z"
}
```

worker 处理成功 → 文件移到 `~/.towow/queue/processed/`；失败 → 移到 `~/.towow/queue/failed/` 并留 error 记录。

## Triage 状态机

`lark-triage` skill 读队列条目，输出一个 JSON state file：

```json
{
  "escalation": "auto" | "needs_user_clarification" | "out_of_scope" | "needs_nature",
  "bundle_key": "website/components" | "backend/api" | ...,
  "issue_path": "docs/issues/lark-<ts>-<slug>.md",
  "severity": "P0" | "P1" | "P2" | "P3",
  "category": "frontend" | "backend" | ...,
  "summary": "一句话根因",
  "reporter_display_name": "..."
}
```

路由：

| escalation | 下一步 |
|---|---|
| `auto` | worker 调 fixer 修 + 开 PR |
| `needs_user_clarification` | 回帖问用户细节，不进 fixer |
| `out_of_scope` | 回帖说这不是 bug，关闭 |
| `needs_nature` | @管理员，等人工裁决 |

## 为什么 state file 权威 > exit code

`claude -p` headless 模式在 budget 边界、网络抖动、工具错误时会吐 exit 1，但此时 state file 可能已经写完。worker 先查 state file，只有 state 缺失才回退到 exit code。

顺序也很重要：triage skill 必须**先**写 state file（几十字的结构化契约），**再**写 issue draft（几 KB 的叙述）。这是 crash-only software 的落盘排序——前者落盘后即使进程立刻被 SIGKILL，pipeline 也不会回到 needs_nature。

## Bundle 与 worktree

triage 产出的 `bundle_key`（比如 `website/components`）决定两件事：

1. **修复分组**：多个关联 bug 可以共享一个 bundle，fixer 跑一次改多条
2. **worktree 隔离**：每个 bundle 对应一个独立的 `/tmp/towow-bug-<bundle_slug>-<ts>/` worktree + `lark/bug-<bundle_slug>-<ts>` 分支

worktree 从 `main` HEAD 创建，所以主工作区的 uncommitted 文件**看不见**。worker 用 `shutil.copy2` 把 triage 写好的 issue draft 从主仓 stage 进 worktree，fixer 才能读到。

## 扩展点

1. **非 Mac 部署**：把 LaunchAgent 换 systemd unit 即可。daemon/worker Python 代码 100% 跨平台。
2. **非飞书源**：daemon 里 `im.message.receive_v1` 的 handler 换成 Slack Events API / Telegram webhook / GitHub Issues polling，队列格式不变，worker 不用改。
3. **多 bundle 并行**：worker `process_batch()` 目前串行跑 bundle，改成 asyncio.gather 或 multiprocessing.Pool 即可并行。注意 git worktree 名字要加随机后缀避免撞车（已加）。
4. **非 Claude Code harness**：worker 里 `claude -p` 调用换成 `codex exec` 或任何其他 headless coding agent CLI。state file 契约不变。
5. **非 guardian-fixer 流程**：如果你的修复流程不是 8 Gate，直接改 worker 里的 fixer prompt 模板。triage 侧不用动。

## 费用模型

| 组件 | 付谁 | 模型 |
|---|---|---|
| 飞书 WebSocket | 飞书免费 | 自建应用免费额度够用 |
| `claude -p` triage | Claude Max 订阅 | 每次 ~2 min opus-4-6 |
| `claude -p` fixer | Claude Max 订阅 | 每次 ~6-10 min opus-4-6 |
| `gh pr create` | GitHub 免费 | 个人账号或组织 |
| API billing | **$0** | 全程不经过 Anthropic API |

worker 顶部的 `TRIAGE_BUDGET_USD=3.00` 和 `FIXER_BUDGET_USD=30.00` 是 `claude -p --max-budget-usd` 的 **circuit breaker**，按 API 价格估算；实际不扣钱，只是超估算时 CLI 会硬中断。

## 重要常量（在 bug_worker.py 顶部）

```python
POLL_INTERVAL = 30          # 队列 poll 周期（秒）
BATCH_SIZE = 10             # 每次处理最多几条
TRIAGE_BUDGET_USD = 3.00    # triage 子进程 budget circuit breaker
FIXER_BUDGET_USD = 30.00    # fixer 子进程 budget
DEFAULT_RELEASE_DELAY = 30  # triage → fixer 之间的缓冲
```

改这些要重启 worker：`launchctl kickstart -k gui/$(id -u)/net.towow.lark-worker`
