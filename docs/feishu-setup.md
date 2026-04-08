# 飞书侧配置（默认 IM 群聊 @bot 路径）

> 默认路径只需要「自建应用 + 群机器人」。多维表格是可选升级，见文末附录 A。

## 1. 创建自建应用

1. 访问 <https://open.feishu.cn/app> → 「创建自建应用」
2. 填名字（比如「通爻 Bug Bot」）、头像
3. 进入应用后在「凭证与基础信息」记下：
   - `App ID` → 填到 `LARK_APP_ID`
   - `App Secret` → 填到 `LARK_APP_SECRET`

## 2. 申请权限 scope

「权限管理」→ 开通以下 scope：

| scope | 用途 |
|---|---|
| `im:message` | 收/发消息 |
| `im:message.group_at_msg` | 收群里 @bot 的消息 |
| `im:message:send_as_bot` | 以 bot 身份回消息 |

改完要点「发布版本」→ 审核通过后 scope 才生效。

> 如果之后想升级到多维表格路径，再额外开 `bitable:app`，见附录 A。

## 3. 开启事件订阅

「事件与回调」→ 「事件订阅」：

1. 订阅方式选「长连接」（**关键**，不要选 webhook，webhook 需要公网回调地址）
2. 添加事件：`im.message.receive_v1`

长连接模式下 `lark-oapi` 会自己用 WebSocket 订阅，不需要你搞内网穿透。

## 4. 把 Bot 加到群里

1. 在目标群里 「设置」→「群机器人」→「添加机器人」→ 选你刚建的自建应用
2. 发一条 `@你的bot` 测试消息
3. 等 3 秒后看 `~/.towow/logs/lark-daemon.log`，应该能看到事件
4. **关键**：日志里会打印 `sender_open_id` 和 `mentioned[].id`——其中 mentioned 的那个 id 就是 bot 自己的 `open_id`，抄到 `LARK_BOT_OPEN_ID`
5. 同样把你自己（管理员）的 `sender_open_id` 抄到 `LARK_NATURE_OPEN_ID`
6. 重启 daemon：`launchctl kickstart -k gui/$(id -u)/net.towow.lark-daemon`

## 5. 验证链路

在飞书群 @bot 发：

```
@通爻 Bug Bot 登录按钮点了没反应
```

预期反应：
- daemon 日志：`IM event received: msg_id=... chat_id=... sender=...`
- daemon 启动行：`daemon 启动 ... bitable=disabled (IM-only)` —— 确认走的是默认路径
- worker 日志：`Triage start: ...` 然后 `Triage complete: escalation=...`
- 飞书群：bot 在 15-30 秒内回复一条「收到，已分诊中…」
- GitHub：6-10 分钟后出现新 PR

如果任意一步没发生，查对应进程的 `.err` 日志。

---

## 附录 A：可选升级 —— 多维表格集成

只有在你想把每个 bug 也沉淀成一张结构化表格记录（而不仅是 GitHub issue/PR）时才需要这一步。纯 IM 路径已经能走完 "反馈 → triage → PR" 全链路。

### A.1 额外权限

「权限管理」补开：

| scope | 用途 |
|---|---|
| `bitable:app` | 读写多维表格 |

发布新版本。

### A.2 建表

1. 飞书 App 内新建「多维表格」
2. 字段：

| 字段名 | 类型 | 说明 |
|---|---|---|
| Bug 描述 | 多行文本 | 用户原话 |
| 严重程度 | 单选：P0/P1/P2/P3 | triage 会更新 |
| 处理状态 | 单选 | `待处理 / Triaging / Fixing / Needs User Clarification / Needs Nature / Out of Scope / Fixed` |
| 分类 | 单选：backend / frontend / scene / docs / other | triage 会更新 |
| 报告人 | 人员 | 自动从 @bot 事件回填 |
| 报告时间 | 创建时间 | 自动 |
| 关联 PR | URL | fixer 成功后回写 |
| Issue 文档路径 | 文本 | triage 写入 `docs/issues/lark-*.md` 路径 |
| bundle_key | 文本 | triage 决定的修复 bundle |

### A.3 配置变量

从表格 URL 里抠 `APP_TOKEN` / `TABLE_ID` / `VIEW_ID`：

```
https://<org>.feishu.cn/base/<APP_TOKEN>?table=<TABLE_ID>&view=<VIEW_ID>
```

在 `~/.towow/.env.lark` 中取消注释并填入：

```
LARK_BUG_TABLE_TOKEN=...
LARK_BUG_TABLE_ID=...
LARK_BUG_VIEW_ID=...   # 可选
```

重启 daemon，日志里应该看到 `daemon 启动 ... bitable=enabled`。

### A.4 降级

想关掉 bitable：把这三行注释回去或删除，重启 daemon。代码会自动退回 IM-only 模式，不影响存量 IM 路径。
