#!/usr/bin/env python3
"""
bug_daemon.py
=============

飞书 Bug 反馈 Pipeline 的「事件长连接 + outbox 网关」守护进程。

== 角色 ==

这是 ADR-040 lark-bug-pipeline 三个进程之一：

    [飞书多维表 record_added]
            ↓
       bug_daemon.py    ← 本文件
            ↓
    ~/.towow/bug-queue.jsonl
            ↓
       bug_worker.py    (另一个进程)
            ↓
       Triage → Fixer → PR
            ↓
    ~/.towow/daemon-outbox/*.json
            ↓
       bug_daemon.py    ← 本文件（outbox 消费方向）
            ↓
       飞书 IM / Bitable 写回

daemon 是双向桥：
    上行：长连接订阅 bitable record_changed → 写 jsonl
    下行：扫 outbox 文件夹 → 调 IM API 发消息 / 调 bitable 改状态

worker 不直接持有飞书 token，所有飞书写操作必须经 daemon outbox。
（ADR-040 D6 决策）

== 依赖 ==

    pip3 install lark-oapi

    # ~/.towow/.env.lark 或者环境变量
    LARK_APP_ID=cli_xxx
    LARK_APP_SECRET=xxxxxxxx
    LARK_BUG_TABLE_TOKEN=W9XxbCIIcabeMRsOxhXchgzAnBg
    LARK_BUG_TABLE_ID=tblXXXXXX
    LARK_NATURE_OPEN_ID=ou_xxxxxxxx        # Nature 自己的 open_id（升级通知发她）

== 飞书开放平台需要做的事 ==

1) 创建自建应用（task #2）
2) 拿到 app_id + app_secret
3) 应用权限里开启：
   - im:message
   - im:message:send_as_bot
   - im:message.p2p_msg
   - bitable:app
   - drive:file:read
4) 「事件订阅」页面：
   - 模式选「长连接」
   - 添加事件：drive.file.bitable_record_changed_v1
5) 「版本管理与发布」创建版本，提交企业管理员审批
6) 把 bot 加为「Bug 反馈」表的协作者（编辑权限）

== 运行 ==

    # 一次性消费 outbox 后退出（调试用）
    python3 scripts/lark/bug_daemon.py --once

    # 正式运行（前台）
    python3 scripts/lark/bug_daemon.py

    # 后台运行（生产推荐）
    nohup python3 scripts/lark/bug_daemon.py > ~/.towow/lark-daemon.out 2>&1 &

    # 假数据测试（不连飞书，只跑 outbox loop）
    python3 scripts/lark/bug_daemon.py --mock
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 注意：lark_oapi 是飞书官方 Python SDK
# 如果还没装：pip3 install lark-oapi
try:
    import lark_oapi as lark
except ImportError:
    print(
        "[!] 缺少依赖 lark-oapi。请运行：pip3 install lark-oapi",
        file=sys.stderr,
    )
    # 在 mock 模式下不强制要求 lark_oapi
    lark = None  # type: ignore


# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------

TOWOW_DIR = Path.home() / ".towow"
QUEUE_FILE = TOWOW_DIR / "bug-queue.jsonl"
DAEMON_OUTBOX = TOWOW_DIR / "daemon-outbox"
OUTBOX_FAILED = TOWOW_DIR / "daemon-outbox-failed"
DAEMON_LOG = TOWOW_DIR / "lark-daemon.log"
ENV_FILE = TOWOW_DIR / ".env.lark"
# 附件（图片/文件）落盘根目录，按 message_id 分子目录
ATTACHMENTS_DIR = TOWOW_DIR / "attachments"
# flush 信号文件：用户确认"没了/开始修"时写入，worker 看到后跳过 debounce
FLUSH_SIGNAL = TOWOW_DIR / "flush-queue.signal"
# triage session 追踪目录（worker 写入，daemon 读取）
TRIAGE_SESSIONS_DIR = TOWOW_DIR / "triage-sessions"
# p2p 回复关联的 session 过期时间（秒），超过不再关联
TRIAGE_SESSION_TTL = 86400  # 24 小时

# 默认 debounce 秒数（与 worker DEFAULTS 保持一致，用于回复提示）
DEFAULTS_DEBOUNCE = 600

# 用户确认触发词（去空格后匹配）
_FLUSH_TRIGGERS = frozenset({
    "开始修", "没了", "就这些", "没有其他问题了", "没有了",
    "可以开始了", "go", "开始吧", "修吧", "没其他了",
})

# outbox 单条消息最多重试几次，超过移到 failed/
MAX_OUTBOX_RETRIES = 5
# outbox 轮询间隔（秒）
OUTBOX_POLL_INTERVAL = 2


# ---------------------------------------------------------------------------
# Triage session 追踪（p2p 回复关联）
# ---------------------------------------------------------------------------


def _find_pending_session(sender_open_id: str) -> dict | None:
    """查找该用户是否有 pending 的 triage session（worker 写入的文件）。

    返回最新的 pending session dict 或 None。
    """
    if not TRIAGE_SESSIONS_DIR.is_dir():
        return None
    now = datetime.now().astimezone()
    best: dict | None = None
    best_time = ""
    for f in TRIAGE_SESSIONS_DIR.iterdir():
        if not f.suffix == ".json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("open_id") != sender_open_id:
            continue
        if data.get("status") != "pending":
            continue
        # 检查过期
        notified = data.get("notified_at", "")
        if notified:
            from datetime import timezone
            try:
                t = datetime.fromisoformat(notified)
                if (now - t).total_seconds() > TRIAGE_SESSION_TTL:
                    continue
            except ValueError:
                pass
        if notified > best_time:
            best = data
            best_time = notified
    return best


def _close_triage_session(record_id: str) -> None:
    """标记 triage session 为 replied。"""
    session_file = TRIAGE_SESSIONS_DIR / f"{record_id}.json"
    if session_file.exists():
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            data["status"] = "replied"
            data["replied_at"] = datetime.now().astimezone().isoformat()
            session_file.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8",
            )
        except Exception:
            logging.exception("close triage session failed: %s", record_id)


# ---------------------------------------------------------------------------
# 全局信号处理
# ---------------------------------------------------------------------------

_running = True


def _install_signal_handlers() -> None:
    def _handle(signum: int, _frame: Any) -> None:
        global _running
        _running = False
        logging.info("收到信号 %s，准备退出", signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

REQUIRED_ENV = {
    "LARK_APP_ID",
    "LARK_APP_SECRET",
}

OPTIONAL_ENV = {
    # 多维表格集成（bitable 路径，ADR-040 v1 原始路径）。
    # 默认不启用 —— 2026-04-08 起默认走 IM 群聊 @bot 路径，bitable
    # 变成可选升级。两个都配了才会启用 bitable 事件订阅和状态写回。
    "LARK_BUG_TABLE_TOKEN",
    "LARK_BUG_TABLE_ID",
    "LARK_BUG_VIEW_ID",
    "LARK_NATURE_OPEN_ID",
    "LARK_LOG_LEVEL",
    # ADR-040 v2（IM 路径）：bot 自己的 open_id，用来判断一条群聊消息
    # 是不是在 @ 我们这个 bot。没配置的话 IM 路径完全不工作（因为
    # lark-oapi 的 MentionEvent 不暴露 bot_info/mentioned_type，
    # 我们只能用 open_id 比对）。
    # 可以从 sniffer 日志或第一次 IM 事件的 payload 里看到。
    "LARK_BOT_OPEN_ID",
}


def bitable_enabled(config: dict[str, str]) -> bool:
    """True 表示 bitable 路径被启用（需要 token + table_id 都非空）。"""
    return bool(config.get("LARK_BUG_TABLE_TOKEN") and config.get("LARK_BUG_TABLE_ID"))


def load_env_file(path: Path) -> None:
    """读 .env.lark 文件并 setdefault 到 os.environ"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(allow_missing: bool = False) -> dict[str, str]:
    load_env_file(ENV_FILE)
    config = {k: os.environ.get(k, "") for k in REQUIRED_ENV | OPTIONAL_ENV}
    if not allow_missing:
        missing = [k for k in REQUIRED_ENV if not config[k]]
        if missing:
            raise SystemExit(
                f"[!] 缺少环境变量：{', '.join(missing)}\n"
                f"    请检查 {ENV_FILE} 或运行环境变量"
            )
    return config


# ---------------------------------------------------------------------------
# DaemonState：把所有 client 和 config 包起来
# ---------------------------------------------------------------------------


class DaemonState:
    def __init__(self, config: dict[str, str], rest_client: Any) -> None:
        self.config = config
        self.rest = rest_client


# ---------------------------------------------------------------------------
# 上行：事件 → queue
# ---------------------------------------------------------------------------


def append_to_queue(entry: dict[str, Any]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_record(state: DaemonState, record_id: str) -> dict[str, Any] | None:
    """
    用 bitable OpenAPI 拉一条 record 的详情。
    返回 record dict（含 fields），失败返回 None。
    """
    try:
        from lark_oapi.api.bitable.v1 import GetAppTableRecordRequest

        req = (
            GetAppTableRecordRequest.builder()
            .app_token(state.config["LARK_BUG_TABLE_TOKEN"])
            .table_id(state.config["LARK_BUG_TABLE_ID"])
            .record_id(record_id)
            .build()
        )
        resp = state.rest.bitable.v1.app_table_record.get(req)
        if not resp.success():
            logging.error(
                "fetch_record 失败 record=%s code=%s msg=%s",
                record_id, resp.code, resp.msg,
            )
            return None
        # resp.data.record 是一个 Record 对象，含 record_id, fields
        record = resp.data.record
        if record is None:
            return None
        return {
            "record_id": getattr(record, "record_id", record_id),
            "fields": getattr(record, "fields", {}) or {},
        }
    except Exception:
        logging.exception("fetch_record 异常 record=%s", record_id)
        return None


def make_record_handler(state: DaemonState):
    """
    返回 bitable_record_changed_v1 的事件 handler。

    事件结构（飞书官方文档）：
        event.file_token       — 多维表 token
        event.action_list[]
            .action            — record_added / record_modified / record_deleted
            .record_id
            .table_id
    """
    def handler(data: Any) -> None:
        try:
            event = data.event
            file_token = getattr(event, "file_token", None)

            # 只处理我们关心的那张表
            if file_token != state.config["LARK_BUG_TABLE_TOKEN"]:
                logging.debug(
                    "忽略 file_token=%s（不是 bug 表）", file_token
                )
                return

            action_list = getattr(event, "action_list", None) or []
            if not action_list:
                return

            for action in action_list:
                action_name = getattr(action, "action", "")
                # 我们只关心新增（用户提了一条新 bug）
                if action_name != "record_added":
                    continue

                table_id = getattr(action, "table_id", "")
                if table_id != state.config["LARK_BUG_TABLE_ID"]:
                    continue

                record_id = getattr(action, "record_id", "")
                if not record_id:
                    continue

                record = fetch_record(state, record_id)
                if not record:
                    continue

                entry = {
                    "record_id": record_id,
                    "received_at": datetime.now().astimezone().isoformat(),
                    "source": "lark-bitable",
                    "fields": record["fields"],
                }
                append_to_queue(entry)
                logging.info(
                    "[上行] record=%s -> queue (fields=%s)",
                    record_id, list(record["fields"].keys()),
                )
        except Exception:
            logging.exception("record handler 异常")

    return handler


# ---------------------------------------------------------------------------
# 上行（IM 路径）：群聊 @bot → queue
# ---------------------------------------------------------------------------
#
# ADR-040 v2（2026-04-08）：从 bitable 事件切换到 IM 群聊事件。
# 原因：
#   1) bitable 事件需要先调"订阅云文档事件"REST API 且受文档所有权限制
#   2) IM `im.message.receive_v1` 无所有权概念，只要 bot 在群里就能收到
#   3) 用户反馈体验："群里 @bot 发一句" 比打开多维表格填表快
#
# IM 路径判定 "这条消息是发给 bot 的"：
#   mentions 数组里存在 `mentioned_type == "bot"` 且 `bot_info.app_id == 本应用`
# 只有命中的消息才入队，群里普通聊天直接忽略。
#
# 文本清洗：飞书 content 里的 @ 是以占位符 `@_user_N` 形式出现（N 是 mention
# 数组里的索引），我们把占位符 strip 掉后再入队，triage 看到的就是纯净描述。


import re as _re

_MENTION_PLACEHOLDER_RE = _re.compile(r"@_user_\d+\s*")


def _extract_im_text(content_raw: str | None) -> str:
    """从 IM message.content 里拿纯文本（strip @ 占位符）。"""
    if not content_raw:
        return ""
    try:
        parsed = json.loads(content_raw)
    except Exception:
        return content_raw.strip()
    text = parsed.get("text", "")
    if not isinstance(text, str):
        return ""
    return _MENTION_PLACEHOLDER_RE.sub("", text).strip()


def _extract_post_content(content_raw: str | None) -> tuple[str, list[dict]]:
    """解析飞书富文本 (post) 消息。

    post content 典型结构：
      {"title": "...", "content": [[{"tag":"text","text":"按钮坏了"},
                                     {"tag":"img","image_key":"img_v2_xxx"}]]}

    返回 (纯文本, 图片/文件 ref list)。ref 形如
      {"kind": "image", "key": "img_v2_xxx"}
      {"kind": "file",  "key": "file_xxx", "name": "error.log"}
    """
    if not content_raw:
        return "", []
    try:
        parsed = json.loads(content_raw)
    except Exception:
        return content_raw.strip(), []

    title = parsed.get("title", "") or ""
    texts: list[str] = []
    if title:
        texts.append(title)

    refs: list[dict] = []
    content = parsed.get("content", [])
    if not isinstance(content, list):
        return _MENTION_PLACEHOLDER_RE.sub("", " ".join(texts)).strip(), refs

    for line in content:
        if not isinstance(line, list):
            continue
        for element in line:
            if not isinstance(element, dict):
                continue
            tag = element.get("tag")
            if tag in ("text", "a", "at", "code_inline"):
                t = element.get("text") or element.get("href") or ""
                if isinstance(t, str) and t:
                    texts.append(t)
            elif tag == "img":
                key = element.get("image_key")
                if key:
                    refs.append({"kind": "image", "key": key})
            elif tag == "media":
                # 视频/文件
                key = element.get("file_key") or element.get("image_key")
                if key:
                    refs.append({"kind": "file", "key": key, "name": element.get("file_name") or ""})
        texts.append("\n")

    joined = _MENTION_PLACEHOLDER_RE.sub("", "".join(texts)).strip()
    return joined, refs


def _extract_image_key(content_raw: str | None) -> str | None:
    """纯图片消息的 content：{"image_key": "img_v2_xxx"}"""
    if not content_raw:
        return None
    try:
        parsed = json.loads(content_raw)
    except Exception:
        return None
    key = parsed.get("image_key")
    return key if isinstance(key, str) else None


def _extract_file_info(content_raw: str | None) -> tuple[str | None, str]:
    """文件消息的 content：{"file_key":"file_xxx","file_name":"xxx.log","file_size":N}"""
    if not content_raw:
        return None, ""
    try:
        parsed = json.loads(content_raw)
    except Exception:
        return None, ""
    key = parsed.get("file_key")
    name = parsed.get("file_name") or ""
    return (key if isinstance(key, str) else None), name


def _download_message_resource(
    rest_client: Any,
    message_id: str,
    file_key: str,
    resource_type: str,
    save_path: Path,
) -> bool:
    """用 lark-oapi SDK 下载某条消息的附件（image 或 file）到 save_path。

    resource_type: "image" | "file"
    成功返回 True，失败返回 False（失败不抛出，调用方决定是否降级）。

    lark-oapi 的 API 路径：
      POST 不是，是 GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}
      SDK: lark_oapi.api.im.v1.GetMessageResourceRequest
           client.im.v1.message.get(request)  →  返回 BinaryIO
    """
    if rest_client is None:
        logging.warning("[IM] rest_client 未初始化，跳过附件下载 key=%s", file_key)
        return False
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        resp = rest_client.im.v1.message_resource.get(req)
        if not resp.success():
            logging.warning(
                "[IM] 下载附件失败 key=%s code=%s msg=%s",
                file_key, getattr(resp, "code", "?"), getattr(resp, "msg", "?"),
            )
            return False
        # 返回的 file 是 BytesIO / file-like
        file_obj = getattr(resp, "file", None)
        if file_obj is None:
            logging.warning("[IM] 下载附件无 body key=%s", file_key)
            return False
        save_path.parent.mkdir(parents=True, exist_ok=True)
        data = file_obj.read() if hasattr(file_obj, "read") else bytes(file_obj)
        save_path.write_bytes(data)
        return True
    except Exception:
        logging.exception("[IM] 下载附件异常 key=%s type=%s", file_key, resource_type)
        return False


def _guess_extension(resource_type: str, original_name: str) -> str:
    """根据 resource_type 和原始文件名推测扩展名。"""
    if original_name and "." in original_name:
        return "." + original_name.rsplit(".", 1)[-1].lower()
    return ".png" if resource_type == "image" else ".bin"


def _materialize_attachments(
    rest_client: Any,
    message_id: str,
    refs: list[dict],
) -> list[dict]:
    """把 refs（来自消息 content 的 image_key/file_key 列表）下载落盘。

    返回 entry.attachments 的 list，元素形如：
      {"kind": "image", "path": "/Users/.../attachments/om_xxx/0.png",
       "original_name": "", "source_key": "img_v2_xxx"}
    """
    if not refs:
        return []
    result: list[dict] = []
    msg_dir = ATTACHMENTS_DIR / message_id
    for i, ref in enumerate(refs):
        kind = ref.get("kind", "image")
        key = ref.get("key")
        if not key:
            continue
        original_name = ref.get("name", "")
        ext = _guess_extension(kind, original_name)
        save_path = msg_dir / f"{i}{ext}"
        ok = _download_message_resource(
            rest_client, message_id, key, kind, save_path,
        )
        if not ok:
            continue
        result.append({
            "kind": kind,
            "path": str(save_path),
            "original_name": original_name,
            "source_key": key,
        })
    return result


def _is_message_for_bot(mentions: list[Any], bot_open_id: str) -> bool:
    """判断消息里是否 @ 了我们这个 bot。

    注意：lark-oapi 的 MentionEvent 模型只暴露 `key/id/name/tenant_key`
    四个字段，**不**暴露原始 JSON 里的 `mentioned_type` 或 `bot_info`。
    因此我们只能用 `id.open_id == bot_open_id` 比对——需要提前把 bot
    自己的 open_id 放到 LARK_BOT_OPEN_ID 环境变量。

    如果 LARK_BOT_OPEN_ID 没配，返回 False（IM 路径视为关闭），这样
    不会误把群里所有 @ 过 bot 的消息都吞进去；同时 daemon 启动时会
    记一条 warning 提示用户去配。
    """
    if not bot_open_id:
        return False
    if not mentions:
        return False
    for m in mentions:
        id_obj = getattr(m, "id", None)
        if id_obj is None:
            continue
        if getattr(id_obj, "open_id", None) == bot_open_id:
            return True
    return False


def make_im_handler(state: DaemonState):
    """
    返回 im.message.receive_v1 的事件 handler。

    只处理群聊里 @bot 的文本/富文本消息。其他全部忽略。
    入队格式（与 bitable 路径共享同一 queue 文件，用 source 字段区分）：

        {
          "record_id": "om_xxx",        # 直接用 message_id，前缀天然区分
          "received_at": "ISO8601",
          "source": "lark-im",
          "fields": {                   # 伪装成 bitable 同形结构，triage 复用
            "症状": "原始消息文本",
            "复现步骤": "",
            "场景": "未分类",
            "严重程度": "未指定",
            "提交人": "ou_xxx"
          },
          "im": {
            "chat_id": "oc_xxx",
            "message_id": "om_xxx",     # 回复时用它作为 reply_to
            "sender_open_id": "ou_xxx",
            "chat_type": "group",
            "raw_text": "@_user_1 登录页 404"
          }
        }
    """
    bot_open_id = state.config.get("LARK_BOT_OPEN_ID", "")
    if not bot_open_id:
        logging.warning(
            "[IM] LARK_BOT_OPEN_ID 未配置，IM 路径将忽略所有群聊事件。"
            " 请把 bot 自己的 open_id 写到 ~/.towow/.env.lark"
        )

    def handler(data: Any) -> None:
        try:
            event = data.event
            message = getattr(event, "message", None)
            sender = getattr(event, "sender", None)
            if message is None or sender is None:
                return

            chat_type = getattr(message, "chat_type", None)
            sender_id_obj = getattr(sender, "sender_id", None)
            sender_open_id = getattr(sender_id_obj, "open_id", None) if sender_id_obj else None

            # p2p 私聊：仅当发送者有 pending triage session 时接收
            if chat_type != "group":
                if not sender_open_id:
                    return
                session = _find_pending_session(sender_open_id)
                if session is None:
                    logging.debug(
                        "[IM] p2p 消息无 pending session sender=%s，忽略",
                        sender_open_id,
                    )
                    return
                logging.info(
                    "[IM] p2p 回复关联 triage session: sender=%s record=%s",
                    sender_open_id, session.get("record_id"),
                )
                # 走下面的通用消息处理流程，entry 会带 reply_to 标记
            else:
                # 群聊：必须 @bot
                mentions = getattr(message, "mentions", None) or []
                if not _is_message_for_bot(mentions, bot_open_id):
                    logging.debug("[IM] 群聊消息未 @bot，忽略")
                    return
                session = None

            message_type = getattr(message, "message_type", None)
            SUPPORTED_TYPES = {"text", "post", "image", "file"}
            if message_type not in SUPPORTED_TYPES:
                logging.info("[IM] 暂不支持的消息类型 %s，忽略", message_type)
                return

            message_id = getattr(message, "message_id", None)
            chat_id = getattr(message, "chat_id", None)
            content_raw = getattr(message, "content", None)
            # sender_open_id 已在上方 p2p/group 分支中提取

            if not message_id or not chat_id:
                logging.warning("[IM] 缺少 message_id/chat_id，跳过")
                return

            # 按消息类型抽取 text + refs
            text = ""
            refs: list[dict] = []
            if message_type == "text":
                text = _extract_im_text(content_raw)
            elif message_type == "post":
                text, refs = _extract_post_content(content_raw)
            elif message_type == "image":
                key = _extract_image_key(content_raw)
                if key:
                    refs.append({"kind": "image", "key": key})
            elif message_type == "file":
                key, name = _extract_file_info(content_raw)
                if key:
                    refs.append({"kind": "file", "key": key, "name": name})

            # 文本 + 附件都空时才跳过（纯 @ 无内容）
            if not text and not refs:
                logging.info("[IM] 消息为空（无文本无附件），跳过")
                return

            # ── flush 检测：用户说"没了/开始修"时跳过 debounce ──
            clean = text.strip().strip("。.！!~")
            if clean in _FLUSH_TRIGGERS:
                FLUSH_SIGNAL.write_text(
                    datetime.now().astimezone().isoformat(), encoding="utf-8",
                )
                logging.info("[IM] flush 信号：%r → 写入 %s", clean, FLUSH_SIGNAL)
                reply_im_message(state, {
                    "kind": "im_reply",
                    "message_id": message_id,
                    "text": "好的，马上开始处理 🔧",
                })
                return  # flush 消息不入 bug 队列

            # 下载附件到 ~/.towow/attachments/<message_id>/
            attachments = _materialize_attachments(
                state.rest, message_id, refs,
            )

            # 如果只有附件没有文字，给 triage 一个友好占位
            if not text and attachments:
                kinds = [a["kind"] for a in attachments]
                text = f"（用户只发了 {len(attachments)} 个附件，无文字说明：{', '.join(kinds)}）"

            entry = {
                "record_id": message_id,
                "received_at": datetime.now().astimezone().isoformat(),
                "source": "lark-im",
                "fields": {
                    "症状": text,
                    "复现步骤": "",
                    "场景": "未分类",
                    "严重程度": "未指定",
                    "提交人": sender_open_id or "",
                },
                "im": {
                    "chat_id": chat_id or "",
                    "message_id": message_id,
                    "sender_open_id": sender_open_id,
                    "chat_type": chat_type,
                    "message_type": message_type,
                    "raw_text": content_raw or "",
                },
                "attachments": attachments,
            }
            # p2p 回复：关联原 triage record + 关闭 session
            if session:
                entry["reply_to"] = session["record_id"]
                _close_triage_session(session["record_id"])
            append_to_queue(entry)
            logging.info(
                "[上行 IM] msg=%s type=%s chat=%s sender=%s text=%r attachments=%d",
                message_id, message_type, chat_id[:12] + "...", sender_open_id,
                text[:80], len(attachments),
            )

            # ── 自动确认回复 ──
            if session:
                # p2p 决策回复：确认收到 + 立即触发处理
                reply_im_message(state, {
                    "kind": "im_reply",
                    "message_id": message_id,
                    "text": (
                        f"收到决策！已关联到原反馈 {session['record_id'][:20]}…\n"
                        f"马上安排处理 🔧"
                    ),
                })
                # 写 flush 信号，让 worker 立即触发
                FLUSH_SIGNAL.write_text(
                    datetime.now().astimezone().isoformat(), encoding="utf-8",
                )
            else:
                debounce_min = int(DEFAULTS_DEBOUNCE / 60)
                reply_im_message(state, {
                    "kind": "im_reply",
                    "message_id": message_id,
                    "text": (
                        f"收到反馈！如果还有补充可以继续发。\n"
                        f"回复「开始修」立即处理，"
                        f"不回复则 {debounce_min} 分钟后自动开始。"
                    ),
                })
        except Exception:
            logging.exception("im handler 异常")

    return handler


# ---------------------------------------------------------------------------
# 下行：outbox → 飞书
# ---------------------------------------------------------------------------


def _read_outbox_payload(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("解析 outbox 文件失败：%s", path)
        return None


def _move_to_failed(path: Path, reason: str) -> None:
    OUTBOX_FAILED.mkdir(parents=True, exist_ok=True)
    target = OUTBOX_FAILED / path.name
    try:
        path.rename(target)
        logging.error("移到 failed/：%s 原因：%s", target, reason)
    except Exception:
        logging.exception("移动失败文件出错：%s", path)


def send_im_message(state: DaemonState, payload: dict[str, Any]) -> bool:
    """
    payload schema:
      {
        "kind": "im_message",
        "receive_id": "ou_xxxxxxxx",
        "receive_id_type": "open_id",   # 默认 open_id
        "msg_type": "text",              # 默认 text
        "text": "你好...",
        "card": {...}                    # 可选，msg_type=interactive 时用
      }
    """
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        msg_type = payload.get("msg_type", "text")
        if msg_type == "text":
            content_dict = {"text": payload.get("text", "")}
        elif msg_type == "interactive":
            content_dict = payload.get("card", {})
        else:
            content_dict = payload.get("content", {})

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(payload["receive_id"])
            .msg_type(msg_type)
            .content(json.dumps(content_dict, ensure_ascii=False))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(payload.get("receive_id_type", "open_id"))
            .request_body(body)
            .build()
        )
        resp = state.rest.im.v1.message.create(req)
        if not resp.success():
            logging.error(
                "send_im 失败 to=%s code=%s msg=%s",
                payload.get("receive_id"), resp.code, resp.msg,
            )
            return False
        logging.info(
            "[下行 IM] -> %s (%s 字)",
            payload.get("receive_id"),
            len(content_dict.get("text", "") if isinstance(content_dict, dict) else ""),
        )
        return True
    except Exception:
        logging.exception("send_im_message 异常")
        return False


def update_bitable_record(state: DaemonState, payload: dict[str, Any]) -> bool:
    """
    payload schema:
      {
        "kind": "bitable_update",
        "record_id": "rec_xxx",
        "fields": {"处理状态": "Triaging", "AI 备注": "..."}
      }

    Bitable 路径未启用时 no-op return True（把 outbox 条目消费掉），
    这样 IM-only 模式下 worker 往 outbox 扔 bitable_update 不会死循环。
    """
    if not bitable_enabled(state.config):
        logging.debug("bitable_update skipped: bitable not enabled")
        return True
    try:
        from lark_oapi.api.bitable.v1 import (
            UpdateAppTableRecordRequest,
            AppTableRecord,
        )

        record_body = AppTableRecord.builder().fields(payload["fields"]).build()
        req = (
            UpdateAppTableRecordRequest.builder()
            .app_token(state.config["LARK_BUG_TABLE_TOKEN"])
            .table_id(state.config["LARK_BUG_TABLE_ID"])
            .record_id(payload["record_id"])
            .request_body(record_body)
            .build()
        )
        resp = state.rest.bitable.v1.app_table_record.update(req)
        if not resp.success():
            logging.error(
                "update_record 失败 record=%s code=%s msg=%s",
                payload.get("record_id"), resp.code, resp.msg,
            )
            return False
        logging.info(
            "[下行 Bitable] record=%s fields=%s",
            payload.get("record_id"),
            list(payload["fields"].keys()),
        )
        return True
    except Exception:
        logging.exception("update_bitable_record 异常")
        return False


def reply_im_message(state: DaemonState, payload: dict[str, Any]) -> bool:
    """回复群聊里一条消息（作为线程回复）。

    payload schema:
      {
        "kind": "im_reply",
        "message_id": "om_xxx",   # 原消息 ID（必填）
        "text": "分诊中..."       # 回复正文
      }

    用 im.v1.message.reply 而不是 create：飞书对"reply 到 message_id"的
    消息会自动在原消息下面拉一条线程，群友能一眼看到某条 bug 的进度链。
    """
    try:
        from lark_oapi.api.im.v1 import (
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        message_id = payload.get("message_id")
        text = payload.get("text", "")
        if not message_id:
            logging.error("im_reply 缺少 message_id")
            return False

        content_dict = {"text": text}
        body = (
            ReplyMessageRequestBody.builder()
            .content(json.dumps(content_dict, ensure_ascii=False))
            .msg_type("text")
            .reply_in_thread(False)  # 普通 reply 而非 thread reply
            .build()
        )
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = state.rest.im.v1.message.reply(req)
        if not resp.success():
            logging.error(
                "im_reply 失败 msg=%s code=%s msg=%s",
                message_id, resp.code, resp.msg,
            )
            return False
        logging.info("[下行 IM reply] parent=%s (%d 字)", message_id, len(text))
        return True
    except Exception:
        logging.exception("reply_im_message 异常")
        return False


HANDLERS = {
    "im_message": send_im_message,
    "bitable_update": update_bitable_record,
    "im_reply": reply_im_message,
}


def process_outbox(state: DaemonState) -> int:
    """
    扫描 daemon-outbox/，按文件名顺序处理。
    成功 → 删文件
    失败 → 文件名带 .retry-N 后缀，超过 MAX 次移到 failed/
    """
    DAEMON_OUTBOX.mkdir(parents=True, exist_ok=True)
    processed = 0

    for path in sorted(DAEMON_OUTBOX.iterdir()):
        if not path.is_file() or path.suffix not in (".json", ""):
            continue

        payload = _read_outbox_payload(path)
        if payload is None:
            _move_to_failed(path, "解析失败")
            continue

        kind = payload.get("kind")
        handler = HANDLERS.get(kind)  # type: ignore
        if handler is None:
            _move_to_failed(path, f"未知 kind={kind}")
            continue

        ok = handler(state, payload)
        if ok:
            try:
                path.unlink()
            except Exception:
                logging.exception("删除 outbox 文件失败：%s", path)
            processed += 1
        else:
            # 失败：累加 retry 计数（写在文件 metadata 里）
            retries = int(payload.get("_retries", 0)) + 1
            if retries >= MAX_OUTBOX_RETRIES:
                _move_to_failed(path, f"重试 {retries} 次仍失败")
            else:
                payload["_retries"] = retries
                path.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                logging.warning(
                    "outbox 重试 %d/%d : %s",
                    retries, MAX_OUTBOX_RETRIES, path.name,
                )

    return processed


# ---------------------------------------------------------------------------
# 后台 outbox 线程
# ---------------------------------------------------------------------------


def outbox_loop(state: DaemonState) -> None:
    while _running:
        try:
            process_outbox(state)
        except Exception:
            logging.exception("outbox loop 异常")
        # 短间隔轮询，daemon 退出时立刻响应
        for _ in range(OUTBOX_POLL_INTERVAL * 10):
            if not _running:
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Mock 模式：不连飞书，只测 outbox 通路
# ---------------------------------------------------------------------------


def mock_send_im_message(state: DaemonState, payload: dict[str, Any]) -> bool:
    logging.info(
        "[MOCK IM] to=%s text=%r",
        payload.get("receive_id"),
        payload.get("text", "")[:80],
    )
    return True


def mock_update_bitable_record(state: DaemonState, payload: dict[str, Any]) -> bool:
    logging.info(
        "[MOCK Bitable] record=%s fields=%s",
        payload.get("record_id"),
        payload.get("fields"),
    )
    return True


def mock_reply_im_message(state: DaemonState, payload: dict[str, Any]) -> bool:
    logging.info(
        "[MOCK IM reply] parent=%s text=%r",
        payload.get("message_id"),
        (payload.get("text") or "")[:80],
    )
    return True


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logging(level: str = "INFO") -> None:
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        handlers=[
            logging.FileHandler(DAEMON_LOG, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="飞书 Bug 反馈长连接 daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只处理一次 outbox 后退出（调试用）",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Mock 模式：不连飞书，hook 用日志代替（端到端演练用）",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="日志级别 DEBUG/INFO/WARNING/ERROR（默认 INFO 或 LARK_LOG_LEVEL）",
    )
    args = parser.parse_args()

    config = load_config(allow_missing=args.mock)
    log_level = args.log_level or config.get("LARK_LOG_LEVEL") or "INFO"
    setup_logging(log_level)
    _install_signal_handlers()

    logging.info(
        "daemon 启动 mock=%s once=%s app_id=%s bitable=%s",
        args.mock, args.once,
        config.get("LARK_APP_ID", "-")[:12] + "...",
        "enabled" if bitable_enabled(config) else "disabled (IM-only)",
    )

    # ----- Mock 模式 -----
    if args.mock:
        # 用 mock handler 替换真实 handler
        HANDLERS["im_message"] = mock_send_im_message
        HANDLERS["bitable_update"] = mock_update_bitable_record
        HANDLERS["im_reply"] = mock_reply_im_message
        state = DaemonState(config=config, rest_client=None)
        if args.once:
            n = process_outbox(state)
            logging.info("[mock --once] processed=%d", n)
            return 0
        logging.info("[mock] 进入 outbox loop（Ctrl+C 退出）")
        try:
            outbox_loop(state)
        except KeyboardInterrupt:
            pass
        return 0

    # ----- 正式模式 -----
    if lark is None:
        print("[!] lark-oapi 未安装", file=sys.stderr)
        return 1

    # 把 daemon 的 --log-level 也传给 lark SDK；不然 SDK 内部
    # 的 "connected to ..."（INFO） / "receive message ..."（DEBUG）
    # 都会被压掉，长连接是否真的通无从判断。
    _lark_level_map = {
        "DEBUG": lark.LogLevel.DEBUG,
        "INFO": lark.LogLevel.INFO,
        "WARNING": lark.LogLevel.WARNING,
        "ERROR": lark.LogLevel.ERROR,
    }
    lark_log_level = _lark_level_map.get(log_level.upper(), lark.LogLevel.WARNING)

    # REST client
    rest = (
        lark.Client.builder()
        .app_id(config["LARK_APP_ID"])
        .app_secret(config["LARK_APP_SECRET"])
        .log_level(lark_log_level)
        .build()
    )
    state = DaemonState(config=config, rest_client=rest)

    if args.once:
        n = process_outbox(state)
        logging.info("--once: processed=%d", n)
        return 0

    # 后台 outbox 线程
    t = threading.Thread(
        target=outbox_loop,
        args=(state,),
        daemon=True,
        name="outbox",
    )
    t.start()

    # WS 长连接（阻塞）——注册事件 handler：
    #   - IM 路径是 v2 主推方案（ADR-040 v2，2026-04-08），默认启用
    #   - bitable 路径是 v1 原始路径，只有在 LARK_BUG_TABLE_TOKEN +
    #     LARK_BUG_TABLE_ID 都配了才启用（可选升级）
    # 两个路径共享同一个 queue 文件，worker 用 source 字段区分
    im_handler = make_im_handler(state)
    builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(im_handler)
    if bitable_enabled(config):
        bitable_handler = make_record_handler(state)
        builder = builder.register_p2_drive_file_bitable_record_changed_v1(bitable_handler)
        logging.info("Bitable 路径已启用（token + table_id 均配置）")
    else:
        logging.info("Bitable 路径未启用 —— IM-only 模式（默认）")
    event_handler = builder.build()
    ws = lark.ws.Client(
        config["LARK_APP_ID"],
        config["LARK_APP_SECRET"],
        event_handler=event_handler,
        log_level=lark_log_level,
    )

    logging.info("启动飞书 WebSocket 长连接...")
    try:
        ws.start()
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt")
    except Exception:
        logging.exception("ws.start 异常")
        return 1

    logging.info("daemon 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
