#!/usr/bin/env python3
"""
bug_worker.py — Lark Bug Pipeline 串行执行器

ADR-040 §3 定义的 worker 实现。

职责：
  1. 从 ~/.towow/bug-queue.jsonl 消费用户 bug 反馈事件（由 bug-daemon.js 写入）
  2. 评估批处理触发条件（debounce / max_batch / max_wait）
  3. 调用 lark-triage skill（通过 `claude -p`）把每条反馈翻译成 issue 草稿
  4. 按 bundle_key 分组
  5. 飞书回报「30s 默认放行」（通过写消息给 daemon）
  6. 等 30s 静默后串行执行 guardian-fixer
  7. 每个 bundle 在独立 worktree 里跑（R1: 基于本地 main HEAD）
  8. finally 块强制清理 worktree（R4）
  9. 错误/blocked 通过飞书通知 Nature

非职责（明确不做的事）：
  * 不直接调 Anthropic API（Claude CLI 已经做了）
  * 不解析飞书事件格式（daemon 已经做了）
  * 不发送飞书消息（写文件给 daemon 发，避免持有飞书 token）
  * 不修改业务代码（worktree 里的子进程做）

关联：
  * ADR-040 §3 架构图 / §4 决策清单
  * .claude/skills/lark-triage/SKILL.md
  * .claude/skills/guardian-fixer/SKILL.md
  * feedback_codex_quality_issues.md (R3 git status 复检)
  * feedback_worktree_governance_gap.md (R1 基于本地 main)
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ==============================================================================
# Config
# ==============================================================================

REPO_ROOT = Path(__file__).resolve().parents[2]  # /Users/nature/个人项目/Towow
TOWOW_DIR = Path.home() / ".towow"
QUEUE_FILE = TOWOW_DIR / "bug-queue.jsonl"
TRIAGE_STATE_DIR = TOWOW_DIR / "triage-state"
WORKER_LOCK = TOWOW_DIR / "worker.lock"
WORKER_LOG = TOWOW_DIR / "worker.log"
WORKER_STATE = TOWOW_DIR / "worker.state.json"
DAEMON_OUTBOX = TOWOW_DIR / "daemon-outbox"  # worker → daemon 消息目录
PROCESSED_LOG = TOWOW_DIR / "processed-records.jsonl"
WORKTREE_BASE = Path("/tmp")

# ADR-040 D2: 批处理触发器参数
DEFAULTS = {
    "DEBOUNCE_SECONDS": 600,           # 10 min: 距上一条新 bug 静默多久
    "MIN_BATCH": 1,                    # 最少凑齐多少条
    "MAX_BATCH": 5,                    # 队列长度达到多少立刻开干
    "MAX_WAIT_SECONDS": 3600,          # 60 min: 最老一条等多久必须开干
    "DEFAULT_RELEASE_DELAY": 30,       # 30 秒默认放行
    "POLL_INTERVAL": 30,               # 主循环 poll 间隔
    "TRIAGE_BUDGET_USD": "3.00",       # 单次 Triage 预算（$0.50 实测过低，4KB issue 草稿会撞上限 → CLI exit 1）
    "FIXER_BUDGET_USD": "5.00",        # 单次 Fixer 预算
    "FIXER_TIMEOUT_SECONDS": 3600,     # 单次 Fixer 超时
}


def load_config() -> dict:
    """加载配置，热更新友好。"""
    cfg_file = TOWOW_DIR / "bug-pipeline.config.json"
    cfg = dict(DEFAULTS)
    if cfg_file.exists():
        try:
            with cfg_file.open() as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception as e:
            logging.warning("Failed to load config %s: %s", cfg_file, e)
    return cfg


# ==============================================================================
# Logging
# ==============================================================================

def setup_logging() -> None:
    TOWOW_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(WORKER_LOG, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(
        logging.Formatter("[%(levelname)s] %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream])


# ==============================================================================
# Lock (确保单实例)
# ==============================================================================

class WorkerLock:
    """Flock-based 单实例锁。worker 启动时 acquire，退出时 release。"""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        TOWOW_DIR.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False
        os.write(self.fd, f"{os.getpid()}\n".encode())
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass


# ==============================================================================
# Queue (jsonl)
# ==============================================================================

@dataclass
class QueueEntry:
    record_id: str               # 飞书 record_id 或 IM message_id（om_ 前缀）
    received_at: str             # ISO8601
    fields: dict                 # 多维表格行字段 / IM 伪装的同形字段
    raw_line: str                # 原始 jsonl 行（用于 ack/skip）
    source: str = "lark-bitable" # "lark-bitable" | "lark-im" | "dry-run-seed"
    im: Optional[dict] = None    # source=="lark-im" 时：chat_id/message_id/sender_open_id/...

    def received_dt(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.received_at)

    @property
    def is_im(self) -> bool:
        return self.source == "lark-im" and bool(self.im)


def read_queue() -> list[QueueEntry]:
    """读 jsonl 队列。daemon 写入这个文件，worker 只读。"""
    if not QUEUE_FILE.exists():
        return []
    entries: list[QueueEntry] = []
    with QUEUE_FILE.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                logging.warning("Bad jsonl line, skip: %s (%s)", raw[:120], e)
                continue
            entries.append(QueueEntry(
                record_id=obj["record_id"],
                received_at=obj["received_at"],
                fields=obj["fields"],
                raw_line=raw,
                source=obj.get("source", "lark-bitable"),
                im=obj.get("im"),
            ))
    return entries


def already_processed(record_id: str) -> bool:
    if not PROCESSED_LOG.exists():
        return False
    with PROCESSED_LOG.open(encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("record_id") == record_id:
                return True
    return False


def mark_processed(record_id: str, status: str, **extra) -> None:
    PROCESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "record_id": record_id,
        "status": status,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **extra,
    }
    with PROCESSED_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def filter_unprocessed(entries: list[QueueEntry]) -> list[QueueEntry]:
    return [e for e in entries if not already_processed(e.record_id)]


# ==============================================================================
# Dry-run helpers (排练用)
# ==============================================================================

_DRY_RUN_SAMPLES = [
    {
        "症状": "发完消息后聊天页白屏（dry-run #1）",
        "复现步骤": "1. 进入自航船 2. 发任意消息 3. 等回复结束",
        "场景": "自航船",
        "严重程度": "影响我用",
    },
    {
        "症状": "登录后头像不显示（dry-run #2）",
        "复现步骤": "1. 用账号登录 2. 看顶栏头像位",
        "场景": "通爻官网",
        "严重程度": "外观问题",
    },
    {
        "症状": "黑客松提交按钮无响应（dry-run #3）",
        "复现步骤": "1. 进入项目页 2. 点提交 3. 没反应",
        "场景": "黑客松",
        "严重程度": "阻断我用",
    },
]


def seed_fake_queue(n: int) -> list[str]:
    """往 ~/.towow/bug-queue.jsonl 追加 n 条假数据，返回 record_id 列表。

    record_id 嵌入秒级时间戳，重复演练时不会撞上之前的 processed 记录，
    所以 filter_unprocessed 永远会返回这些新条目。
    """
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    now = int(time.time())
    with QUEUE_FILE.open("a", encoding="utf-8") as f:
        for i in range(n):
            sample = _DRY_RUN_SAMPLES[i % len(_DRY_RUN_SAMPLES)]
            record_id = f"rec_dryrun_{now}_{i:03d}"
            entry = {
                "record_id": record_id,
                "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "source": "dry-run-seed",
                "fields": dict(sample),
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            written.append(record_id)
    return written


# ==============================================================================
# Trigger Evaluation (ADR-040 D2)
# ==============================================================================

def evaluate_triggers(
    entries: list[QueueEntry],
    cfg: dict,
) -> tuple[bool, str]:
    """
    返回 (should_fire, reason)。

    触发条件（OR）：
      ① len >= MIN_BATCH AND time_since_last >= DEBOUNCE
      ② len >= MAX_BATCH
      ③ now - oldest >= MAX_WAIT
    """
    if not entries:
        return False, "empty queue"

    now = dt.datetime.now(dt.timezone.utc)
    n = len(entries)
    oldest = min(e.received_dt() for e in entries)
    newest = max(e.received_dt() for e in entries)
    age_oldest = (now - oldest).total_seconds()
    silence = (now - newest).total_seconds()

    # ②: 队列爆满
    if n >= cfg["MAX_BATCH"]:
        return True, f"max_batch: queue size {n} >= {cfg['MAX_BATCH']}"

    # ③: 兜底
    if age_oldest >= cfg["MAX_WAIT_SECONDS"]:
        return True, (
            f"max_wait: oldest entry age {int(age_oldest)}s "
            f">= {cfg['MAX_WAIT_SECONDS']}s"
        )

    # ①: 常态
    if n >= cfg["MIN_BATCH"] and silence >= cfg["DEBOUNCE_SECONDS"]:
        return True, (
            f"debounce: {n} entries, silence {int(silence)}s "
            f">= {cfg['DEBOUNCE_SECONDS']}s"
        )

    return False, (
        f"waiting: n={n} silence={int(silence)}s age_oldest={int(age_oldest)}s"
    )


# ==============================================================================
# Triage (调 lark-triage skill via `claude -p`)
# ==============================================================================

@dataclass
class TriageResult:
    record_id: str
    issue_path: Optional[str]
    bundle_key: str
    scope_estimate: str
    escalation: str          # auto / needs_nature / needs_user_clarification / out_of_scope
    feishu_message_for_user: Optional[str] = None
    feishu_message_for_nature: Optional[str] = None
    error: Optional[str] = None


def synthetic_triage(entry: QueueEntry) -> TriageResult:
    """Dry-run: 不调 Claude，按 entry 字段合成一个 Triage 结果。

    bundle_key 用「scenes/<场景>」是为了让分组逻辑能验证（同场景会合到一起）。
    issue_path 是不存在的占位路径，下游 synthetic_fixer 也是 dry-run，
    永远不会真的去读它，所以不用预先创建文件。
    """
    scene = (entry.fields.get("场景") or "其他").strip() or "其他"
    bundle_key = f"scenes/{scene}"
    issue_path = f"docs/issues/lark-{entry.record_id}-dryrun.md"
    logging.info(
        "[DRY] Triage synthetic: record=%s -> bundle=%s",
        entry.record_id, bundle_key,
    )
    return TriageResult(
        record_id=entry.record_id,
        issue_path=issue_path,
        bundle_key=bundle_key,
        scope_estimate="small",
        escalation="auto",
    )


def run_triage(entry: QueueEntry, cfg: dict) -> TriageResult:
    """
    调用 lark-triage skill 处理一条飞书反馈。

    通过 `claude -p` 启动 headless Claude，让它读 SKILL.md 并执行。
    Triage 自己写 issue 文档 + 状态文件到 ~/.towow/triage-state/{record_id}.json。
    我们读状态文件返回结果。
    """
    if cfg.get("DRY_RUN"):
        return synthetic_triage(entry)

    state_file = TRIAGE_STATE_DIR / f"{entry.record_id}.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if state_file.exists():
        state_file.unlink()

    # 把 entry 的字段以 JSON 形式喂给 skill
    input_payload = json.dumps({
        "record_id": entry.record_id,
        "received_at": entry.received_at,
        "fields": entry.fields,
    }, ensure_ascii=False, indent=2)

    prompt = (
        "使用 lark-triage skill 处理下面这条飞书 Bug 反馈。\n\n"
        "输入数据：\n"
        f"```json\n{input_payload}\n```\n\n"
        f"完成后必须把状态文件写到：{state_file}\n"
        "请严格按 lark-triage SKILL.md 的执行流程跑。"
    )

    # 关键：必须传 `--setting-sources user`，否则 claude CLI 会自动加载项目
    # 根目录的 CLAUDE.md + 所有 path-scoped 规则，context 膨胀后触发某种自动
    # 模型路由到 `sonnet-4-6` 这个不可解析的 alias，claude CLI 直接 exit 1。
    # 2026-04-08 真飞书 @bot 端到端调试 1h 才定位到的坑。
    # skill 文件仍可通过 cwd 被找到，不影响 lark-triage skill 加载。
    cmd = [
        "claude",
        "-p", prompt,
        "--model", "claude-opus-4-6",
        "--max-budget-usd", str(cfg["TRIAGE_BUDGET_USD"]),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--setting-sources", "user",
        "--output-format", "text",
    ]

    logging.info("Triage start: record_id=%s", entry.record_id)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=600,  # Triage 最多 10 分钟
        )
    except subprocess.TimeoutExpired:
        logging.error("Triage timeout: record_id=%s", entry.record_id)
        return TriageResult(
            record_id=entry.record_id,
            issue_path=None,
            bundle_key="_unbundleable",
            scope_estimate="unknown",
            escalation="needs_nature",
            error="triage_timeout",
            feishu_message_for_nature=(
                f"Triage 超时（10 min）：record_id={entry.record_id}\n"
                f"症状：{entry.fields.get('症状', '?')}"
            ),
        )

    # 权威输出优先：state file > exit code。
    # 原因：claude CLI 在某些 hook / session-end 场景下会 exit != 0，
    # 但 skill 本身已经完成并写出 state file。如果只信 exit code 就会把
    # 成功的 triage 误判为失败，触发假 needs_nature 通知（2026-04-08 实测）。
    # 判定优先级：
    #   1. state file 存在且 escalation 字段合法 → 认为 triage 成功（哪怕 exit != 0）
    #   2. state file 缺失 / 无效 → fall back 到 exit code + stderr 报错
    state: dict | None = None
    if state_file.exists():
        try:
            with state_file.open(encoding="utf-8") as f:
                candidate = json.load(f)
            if candidate.get("escalation") in {
                "auto", "needs_user_clarification", "out_of_scope", "needs_nature",
            }:
                state = candidate
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning(
                "Triage state file unreadable: %s (%s)", state_file, exc,
            )

    if state is not None:
        if proc.returncode != 0:
            logging.warning(
                "Triage exit %d but state file valid; honoring state file. "
                "stderr head: %s",
                proc.returncode, proc.stderr[:200],
            )
        return TriageResult(
            record_id=entry.record_id,
            issue_path=state.get("issue_path"),
            bundle_key=state.get("bundle_key", "_unbundleable"),
            scope_estimate=state.get("scope_estimate", "unknown"),
            escalation=state.get("escalation", "needs_nature"),
            feishu_message_for_user=state.get("feishu_message_for_user"),
            feishu_message_for_nature=state.get("feishu_message_for_nature"),
        )

    # state file 缺失或无效 → 真失败
    if proc.returncode != 0:
        logging.error(
            "Triage exit %d + no valid state file: stderr=%s",
            proc.returncode, proc.stderr[:500],
        )
        return TriageResult(
            record_id=entry.record_id,
            issue_path=None,
            bundle_key="_unbundleable",
            scope_estimate="unknown",
            escalation="needs_nature",
            error=f"triage_exit_{proc.returncode}",
            feishu_message_for_nature=(
                f"Triage 失败（exit {proc.returncode}，无 state file）："
                f"record_id={entry.record_id}\n"
                f"stderr 前 500 字：{proc.stderr[:500]}"
            ),
        )

    logging.error(
        "Triage exit 0 but didn't write state file: %s", state_file,
    )
    return TriageResult(
        record_id=entry.record_id,
        issue_path=None,
        bundle_key="_unbundleable",
        scope_estimate="unknown",
        escalation="needs_nature",
        error="triage_no_state_file",
        feishu_message_for_nature=(
            f"Triage 异常：record_id={entry.record_id} exit 0 但没写 state 文件\n"
            f"stdout 前 500 字：{proc.stdout[:500]}"
        ),
    )


# ==============================================================================
# Bundle Grouping (ADR-040 D3)
# ==============================================================================

def group_by_bundle(results: list[TriageResult]) -> dict[str, list[TriageResult]]:
    """
    按 bundle_key 分组。
    例外：scope_estimate == 'large' 的拆出来单独跑（避免拖死小 bug 组）。
    """
    bundles: dict[str, list[TriageResult]] = {}
    for r in results:
        if r.scope_estimate == "large":
            key = f"_solo_{r.record_id}"
        else:
            key = r.bundle_key or "_unbundleable"
        bundles.setdefault(key, []).append(r)
    return bundles


# ==============================================================================
# Worktree Manager (ADR-040 D5)
# ==============================================================================

class WorktreeManager:
    """
    R1: 基于本地 main HEAD 创建（不是 origin/main）
    R4: finally 强制清理
    """
    def __init__(self, bundle_key: str) -> None:
        slug = bundle_key.replace("/", "-").replace("_", "")
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.branch = f"lark/bug-{slug}-{ts}"
        self.path = WORKTREE_BASE / f"towow-bug-{slug}-{ts}"

    def __enter__(self) -> "WorktreeManager":
        # R1: 显式 main（本地 HEAD），不是 origin/main
        # 教训来源：feedback_worktree_governance_gap.md
        cmd = [
            "git", "worktree", "add",
            str(self.path),
            "-b", self.branch,
            "main",  # ← 本地 main HEAD，不是 origin/main
        ]
        logging.info("Worktree create: %s", " ".join(cmd))
        result = subprocess.run(
            cmd, cwd=str(REPO_ROOT),
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {result.stderr}"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # R4: finally 强制清理，不管中间发生了什么
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(self.path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                check=False,
            )
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
            # 删除 branch（worktree 已 remove）
            subprocess.run(
                ["git", "branch", "-D", self.branch],
                cwd=str(REPO_ROOT),
                capture_output=True,
                check=False,
            )
            logging.info("Worktree cleaned: %s", self.path)
        except Exception as e:
            logging.error("Worktree cleanup failed: %s", e)


# ==============================================================================
# Fixer (调 guardian-fixer skill via `claude -p`)
# ==============================================================================

@dataclass
class FixerResult:
    bundle_key: str
    pr_url: Optional[str]
    status: str              # pr_ready / blocked / failed
    error: Optional[str] = None


def synthetic_fixer(bundle_key: str) -> FixerResult:
    """Dry-run: 不创建 worktree，不调 Claude，假装一个 PR ready。

    PR url 嵌入时间戳让重复演练能区分批次。
    """
    pr_url = (
        f"https://github.com/towow/towow/pull/dryrun-{int(time.time())}"
    )
    logging.info(
        "[DRY] Fixer synthetic: bundle=%s -> %s", bundle_key, pr_url,
    )
    return FixerResult(
        bundle_key=bundle_key,
        pr_url=pr_url,
        status="pr_ready",
    )


def run_fixer_for_bundle(
    bundle_key: str,
    bundle: list[TriageResult],
    cfg: dict,
) -> FixerResult:
    """
    在独立 worktree 里运行 guardian-fixer 处理一组 issue。
    """
    issue_paths = [r.issue_path for r in bundle if r.issue_path]
    if not issue_paths:
        return FixerResult(
            bundle_key=bundle_key,
            pr_url=None,
            status="failed",
            error="no_issue_paths",
        )

    if cfg.get("DRY_RUN"):
        return synthetic_fixer(bundle_key)

    with WorktreeManager(bundle_key) as wt:
        # Stage issue files into worktree.
        # 原因：triage 把 issue 草稿写在主仓 working dir 的 `docs/issues/lark-*.md`，
        # 还没 commit。git worktree 是独立 checkout，看不到主仓的 uncommitted 文件。
        # fixer 在 worktree 里找不到这些 issue 会立刻 BLOCKED。
        # 2026-04-08 第三次端到端跑实测到。
        staged_issues: list[str] = []
        missing_issues: list[str] = []
        for ip in issue_paths:
            src = REPO_ROOT / ip
            dst = wt.path / ip
            if not src.exists():
                missing_issues.append(ip)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            staged_issues.append(ip)
            logging.info("Staged issue into worktree: %s", ip)

        if missing_issues:
            logging.error("Issues missing in main working dir: %s", missing_issues)
            return FixerResult(
                bundle_key=bundle_key,
                pr_url=None,
                status="failed",
                error=f"issues_missing_in_repo: {missing_issues}",
            )

        prompt = (
            "使用 guardian-fixer skill 处理下面这一组 issue（按列出顺序）。\n"
            "你已经在一个干净的 worktree 里：\n"
            f"  worktree: {wt.path}\n"
            f"  branch:   {wt.branch}\n\n"
            "Issue 列表（这些文件已经从主仓 stage 进来，但还没 commit）：\n"
            + "\n".join(f"  - {p}" for p in staged_issues)
            + "\n\n"
            "严格按 guardian-fixer SKILL.md 的 8 Gate 流程跑。\n"
            "Gate 5 实现阶段优先用 Codex MCP（mechanical 写代码省 token）。\n"
            "完成后输出 PR url 到 stdout 最后一行，格式：PR_URL=https://github.com/...\n"
            "如果 blocked，最后一行输出：BLOCKED=<原因>"
        )

        # 同 triage：必须 `--setting-sources user`，不加载项目 CLAUDE.md，
        # 否则 claude CLI 会因 context 膨胀触发 sonnet-4-6 自动路由 → exit 1。
        cmd = [
            "claude",
            "-p", prompt,
            "--model", "claude-opus-4-6",  # fixer 用 opus（含审查环节）
            "--max-budget-usd", str(cfg["FIXER_BUDGET_USD"]),
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--setting-sources", "user",
            "--output-format", "text",
        ]

        logging.info(
            "Fixer start: bundle=%s issues=%d worktree=%s",
            bundle_key, len(issue_paths), wt.path,
        )

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(wt.path),  # ← 在 worktree 目录里跑
                capture_output=True,
                text=True,
                timeout=cfg["FIXER_TIMEOUT_SECONDS"],
            )
        except subprocess.TimeoutExpired:
            logging.error("Fixer timeout: bundle=%s", bundle_key)
            return FixerResult(
                bundle_key=bundle_key,
                pr_url=None,
                status="failed",
                error="fixer_timeout",
            )

        # 解析 stdout 最后几行找 PR_URL 或 BLOCKED 标记
        lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
        pr_url = None
        blocked_reason = None
        for line in reversed(lines[-20:]):  # 只看最后 20 行
            if line.startswith("PR_URL="):
                pr_url = line.split("=", 1)[1]
                break
            if line.startswith("BLOCKED="):
                blocked_reason = line.split("=", 1)[1]
                break

        if pr_url:
            return FixerResult(
                bundle_key=bundle_key,
                pr_url=pr_url,
                status="pr_ready",
            )
        if blocked_reason:
            return FixerResult(
                bundle_key=bundle_key,
                pr_url=None,
                status="blocked",
                error=blocked_reason,
            )
        return FixerResult(
            bundle_key=bundle_key,
            pr_url=None,
            status="failed",
            error=(
                f"fixer_exit_{proc.returncode}: "
                f"no PR_URL or BLOCKED marker in last 20 lines"
            ),
        )


# ==============================================================================
# Daemon Outbox (worker → daemon → 飞书)
# ==============================================================================
#
# Wire format 与 bug_daemon.py 的 HANDLERS 严格对齐：
#   - 顶层 "kind" ∈ {"bitable_update", "im_message"}
#   - 其他字段平铺在顶层（不嵌套 payload），daemon 直接 payload[key] 取
#
# 三个意图明确的 helper 覆盖三种场景，调用方不再手写 dict：
#   - update_bitable           : 状态/AI 备注/PR 链接写回多维表
#   - reply_to_user_in_bitable : 回复提交人（写到「AI 备注」列）
#   - notify_nature            : 私聊 Nature（需要 LARK_NATURE_OPEN_ID）
#
# ⚠️ 飞书表字段名（按 CSV 导入路径建的表）：
#     症状 / 复现步骤 / 截图录屏 / 场景 / 严重程度 / 处理状态 / AI 备注 / 关联 PR
# 不要再写 "AI 状态" / "PR 链接" — 那是早期版本，跟实际表结构对不上。


def _load_lark_env() -> None:
    """读 ~/.towow/.env.lark 进 os.environ，跟 daemon 共享同一份配置。

    worker 自己不连飞书，但需要 LARK_NATURE_OPEN_ID 来决定 notify_nature
    是真发还是只 log。daemon 已经会读这个文件，worker 跟着读一遍即可。
    """
    env_file = TOWOW_DIR / ".env.lark"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _emit_outbox(kind: str, **fields) -> None:
    """统一写 outbox 文件。fields 平铺到 JSON 顶层，与 daemon HANDLERS 对齐。

    文件名用 `time.time_ns()` 而不是 `int(time.time())`，是为了让
    lex sort == 时间序：daemon 用 sorted(iterdir()) 处理，必须保证
    同一 record 的 Queued → Fixing → PR Ready 严格按时间顺序到达飞书。
    秒级时间戳 + uuid hash 会乱序，daemon 重启时堆积消费会让飞书表
    卡在中间态——这是真实的状态机一致性 bug，所以这里特意保证单调。
    """
    DAEMON_OUTBOX.mkdir(parents=True, exist_ok=True)
    msg_id = uuid.uuid4().hex[:12]
    out_file = DAEMON_OUTBOX / f"{time.time_ns()}-{kind}-{msg_id}.json"
    payload = {
        "kind": kind,
        "_created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **fields,
    }
    out_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logging.info("Outbox queued: %s -> %s", kind, out_file.name)


def update_bitable(record_id: str, fields: dict) -> None:
    """改飞书 bug 表里某条 record 的字段（处理状态 / AI 备注 / 关联 PR）。"""
    _emit_outbox("bitable_update", record_id=record_id, fields=fields)


def reply_to_user_in_bitable(record_id: str, message: str) -> None:
    """回复提交 bug 的用户：写到「AI 备注」列。

    选这条路径而不是私聊，是因为用户原本就在多维表里看 bug 进度，
    AI 备注列是最自然的反馈通道，也省去从 record fields 反查 open_id。
    """
    update_bitable(record_id, {"AI 备注": message})


def notify_nature(text: str) -> None:
    """私聊 Nature。需要 LARK_NATURE_OPEN_ID 已配置。

    没配的时候不报错——只 log 一行 warning，让 worker 在 daemon 还没启用
    真飞书前（dry-run / 用户尚未完成 task #2）也能正常跑完整批处理流程。
    """
    nature_open_id = os.environ.get("LARK_NATURE_OPEN_ID", "").strip()
    if not nature_open_id:
        logging.warning(
            "LARK_NATURE_OPEN_ID 未配置，notify_nature 跳过：%s",
            text[:120].replace("\n", " "),
        )
        return
    _emit_outbox(
        "im_message",
        receive_id=nature_open_id,
        receive_id_type="open_id",
        msg_type="text",
        text=text,
    )


# ------------------------------------------------------------------------------
# IM 路径（ADR-040 v2）：按 entry.source 分发状态回写
# ------------------------------------------------------------------------------
#
# 设计约束：process_batch 原本全量调 update_bitable()。v2 加入 IM 路径后，
# 我们不想把 process_batch 拆成两套流程——两条路径除了"状态写到哪里"之外
# 完全一致。因此引入两个 thin dispatcher：
#
#   report_status(entry, fields)   ← 替换原 update_bitable(entry.record_id, fields)
#   reply_to_user(entry, message)  ← 替换原 reply_to_user_in_bitable(r.record_id, msg)
#
# dispatcher 按 entry.source 分流：
#   - lark-bitable  → 原 update_bitable 路径（写多维表字段）
#   - lark-im       → 新 im_reply 路径（用 message_id 回复到原消息下）
#
# IM 路径的 "状态字段" 必须翻译成人类可读文本。fields_to_im_text 做这件事，
# 把 fields 字典渲染成一段对群里用户友好的中文消息。
#
# 为什么不直接把所有状态写成同一段文字？——每次状态推进只带变化的字段，
# 渲染器按"状态机快照"解读 fields，保证语义一致（比如 "处理状态=PR 就绪" 优先于
# "AI 备注"）。这样 process_batch 的原调用点完全不用改语义。


_IM_STATUS_LABEL = {
    "分诊中": "[分诊] 收到了，正在看是什么问题",
    "排队中": "[排队] 已识别，等待修复队列",
    "修复中": "[修复] guardian-fixer 接管，正在改代码",
    "PR 就绪": "[就绪] PR 已开",
    "阻塞": "[阻塞] 自动修复失败",
    "已关闭": "[已关闭]",
}


def fields_to_im_text(fields: dict) -> str:
    """把 bitable 风格的 fields 字典渲染成一段群聊用户可读的文本。

    渲染规则（按优先级）：
      1. 有「关联 PR」→ PR 就绪消息 + 链接（优先级最高，盖过其他字段）
      2. 有「处理状态」→ 该状态对应的中文标签
      3. 如果带「AI 备注」→ 作为原因/说明附加
      4. 都没有 → 原 dict 的 JSON 兜底（不应该发生，留作防御）
    """
    pr_url = (fields.get("关联 PR") or "").strip()
    status = (fields.get("处理状态") or "").strip()
    notes = (fields.get("AI 备注") or "").strip()

    lines: list[str] = []
    if status:
        label = _IM_STATUS_LABEL.get(status, f"[{status}]")
        lines.append(label)
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if notes:
        # 把备注截断，避免把长 stack trace 贴到群里
        snippet = notes if len(notes) <= 300 else notes[:300] + "..."
        lines.append(f"原因: {snippet}")

    if not lines:
        # 兜底：不应触发
        lines.append(json.dumps(fields, ensure_ascii=False))
    return "\n".join(lines)


def _emit_im_reply(message_id: str, text: str) -> None:
    """写 im_reply 出库条目。daemon 会调 im.v1.message.reply。"""
    _emit_outbox(
        "im_reply",
        message_id=message_id,
        text=text,
    )


def report_status(entry: QueueEntry, fields: dict) -> None:
    """按 entry.source 把状态更新写回去。

    - bitable：直接调 update_bitable（等价原路径）
    - im：把 fields 翻译成文本 + im_reply 到原消息
    """
    if entry.is_im:
        parent_id = entry.im.get("message_id") if entry.im else None
        if not parent_id:
            logging.warning(
                "report_status: IM entry 缺 message_id (record=%s)",
                entry.record_id,
            )
            return
        text = fields_to_im_text(fields)
        _emit_im_reply(parent_id, text)
    else:
        update_bitable(entry.record_id, fields)


def _default_user_reply_for_escalation(escalation: str, entry: QueueEntry) -> str:
    """当 triage skill 没写 feishu_message_for_user 时的兜底话术。

    目的：保证凡是非 auto 的 escalation，都会在群里给用户留一段可理解的回复，
    不让用户看到 "[分诊中]" 后就没下文。lark-triage SKILL.md 应当**总是**写
    feishu_message_for_user，这里只是代码层兜底——防 skill prompt 飘。
    """
    symptom = (entry.fields.get("症状") or "").strip() or "（空）"
    if escalation == "out_of_scope":
        return (
            "这条反馈我看了一下，像是信息不够（没有复现步骤 / 场景 / 严重程度），"
            f"或者内容太宽泛（症状：{symptom}）。\n\n"
            "如果是真问题，麻烦帮我补一下：\n"
            "1. 哪个页面 / 产品？\n"
            "2. 点了什么、看到了什么、期待看到什么？\n"
            "3. 怎么复现（按顺序）？\n\n"
            "如果是通道测试——收到了 ✓ 通道通的。"
        )
    if escalation == "needs_user_clarification":
        return (
            f"收到你的反馈（{symptom}）。\n\n"
            "但我还差点信息才能定位问题，能帮我补一下吗：\n"
            "1. 具体在哪个页面 / 哪个按钮 / 哪个场景？\n"
            "2. 操作步骤能按顺序写一下吗？\n"
            "3. 你期待的表现是什么、实际看到了什么？"
        )
    if escalation == "needs_nature":
        return (
            "这条反馈超出我自动处理的范围，我已经通知 Nature，她会亲自看一下。"
            "后续进展会在这里继续同步。"
        )
    # 其他未知 escalation
    return f"收到反馈（{symptom}），目前状态：{escalation}。有进展会继续同步。"


def reply_to_user(entry: QueueEntry, message: str) -> None:
    """直接回复提交人一段自由文本（triage 要求澄清场景用）。"""
    if entry.is_im:
        parent_id = entry.im.get("message_id") if entry.im else None
        if not parent_id:
            logging.warning(
                "reply_to_user: IM entry 缺 message_id (record=%s)",
                entry.record_id,
            )
            return
        _emit_im_reply(parent_id, message)
    else:
        reply_to_user_in_bitable(entry.record_id, message)


# ==============================================================================
# Stop Signal (用户在飞书里说 STOP)
# ==============================================================================

def check_stop_signal(bundle_key: str) -> bool:
    """
    检查 ~/.towow/stop-signals/ 是否有针对这个 bundle 的 STOP。
    daemon 接收到飞书消息时写入这个目录。
    """
    stop_file = TOWOW_DIR / "stop-signals" / f"{bundle_key}.stop"
    if stop_file.exists():
        stop_file.unlink()  # 消费掉
        return True
    return False


# ==============================================================================
# Process One Batch
# ==============================================================================

def process_batch(entries: list[QueueEntry], cfg: dict) -> None:
    logging.info("=== Process batch: %d entries ===", len(entries))

    # record_id → entry 映射：triage/fixer 结果阶段只拿到 record_id，
    # 需要反查原 entry 才能决定状态写回到 bitable 还是 IM reply
    entries_by_id: dict[str, QueueEntry] = {e.record_id: e for e in entries}

    def _entry_for(record_id: str) -> Optional[QueueEntry]:
        e = entries_by_id.get(record_id)
        if e is None:
            logging.warning(
                "entries_by_id 缺 record=%s（可能 triage 产生了未知 id）",
                record_id,
            )
        return e

    # Step 1: Triage every entry
    #
    # 沟通原则（Nature 指示 2026-04-08）：
    #   "bot 可以经常去跟用户沟通，反正就是发信息的形式"
    # 具体落地：
    #   - triage 前：先 reply 一条「分诊中」告诉用户"在看了"
    #   - triage 后：按 escalation 结果**必然**再 reply 一条定状态的话
    #     （out_of_scope 也要回群，不能只私聊 Nature——那让用户觉得没人理）
    triage_results: list[TriageResult] = []
    for entry in entries:
        # 先告诉用户"在看"
        report_status(entry, {"处理状态": "分诊中"})

        result = run_triage(entry, cfg)
        triage_results.append(result)

    # Step 2: 处理 escalation
    auto_results = []
    for r in triage_results:
        entry = _entry_for(r.record_id)

        if r.escalation == "auto":
            # 进 fixer 队列——给群里回一条"排队中"
            if entry is not None:
                report_status(entry, {"处理状态": "排队中"})
            auto_results.append(r)
            continue

        # 非 auto 路径：必然给群里回一条对状态的说明 + 按需私聊 Nature
        if entry is not None:
            user_msg = r.feishu_message_for_user or _default_user_reply_for_escalation(
                r.escalation, entry
            )
            reply_to_user(entry, user_msg)

        if r.escalation == "needs_nature":
            notify_nature(
                r.feishu_message_for_nature
                or f"Triage 标 needs_nature：{r.record_id}\n"
                f"症状：{(entry.fields.get('症状', '?') if entry else '?')}"
            )

        mark_processed(
            r.record_id,
            status=f"escalated_{r.escalation}",
            issue_path=r.issue_path,
        )

    if not auto_results:
        logging.info("No auto-releasable triage results in this batch")
        return

    # Step 3: 按 bundle 分组
    bundles = group_by_bundle(auto_results)
    logging.info("Bundles: %s", {k: len(v) for k, v in bundles.items()})

    # Step 4: 飞书回报「默认放行」
    bundle_summary = "\n".join(
        f"  - {k}: {len(v)} 条"
        for k, v in bundles.items()
    )
    notify_nature(
        f"[Lark Pipeline] 一批 bug 准备处理"
        f"（{cfg['DEFAULT_RELEASE_DELAY']}s 后默认开干）：\n"
        f"{bundle_summary}\n\n"
        f"想中止某组：在飞书发 STOP <bundle_key>"
    )

    # Step 5: 等 30 秒，期间允许用户 STOP
    logging.info("Default release delay %d s", cfg["DEFAULT_RELEASE_DELAY"])
    time.sleep(cfg["DEFAULT_RELEASE_DELAY"])

    # Step 6: 串行执行每个 bundle
    for bundle_key, bundle in bundles.items():
        if check_stop_signal(bundle_key):
            logging.info("Bundle stopped by user: %s", bundle_key)
            for r in bundle:
                entry = _entry_for(r.record_id)
                if entry is None:
                    continue
                # 飞书表选项里没有「用户中止」，落到「已关闭」+ 备注里写明原因
                report_status(entry, {
                    "处理状态": "已关闭",
                    "AI 备注": "用户手动中止该组",
                })
                mark_processed(r.record_id, status="user_stopped")
            continue

        # 排队中 → 修复中
        for r in bundle:
            entry = _entry_for(r.record_id)
            if entry is None:
                continue
            report_status(entry, {"处理状态": "修复中"})

        try:
            result = run_fixer_for_bundle(bundle_key, bundle, cfg)
        except Exception as e:
            logging.exception("Fixer crashed: bundle=%s", bundle_key)
            result = FixerResult(
                bundle_key=bundle_key,
                pr_url=None,
                status="failed",
                error=f"crash: {e}",
            )

        # Step 7: 回报结果
        for r in bundle:
            entry = _entry_for(r.record_id)
            if entry is None:
                continue
            if result.status == "pr_ready":
                report_status(entry, {
                    # 「PR 就绪」中间有空格，必须严格匹配飞书选项名
                    "处理状态": "PR 就绪",
                    "关联 PR": result.pr_url or "",
                })
            else:
                # 「阻塞」是飞书选项原名，详细原因写到 AI 备注
                report_status(entry, {
                    "处理状态": "阻塞",
                    "AI 备注": f"原因：{result.error or result.status}",
                })
            mark_processed(
                r.record_id,
                status=result.status,
                pr_url=result.pr_url,
                error=result.error,
            )

        notify_nature(
            f"[Lark Pipeline] Bundle {bundle_key} 处理完成：{result.status}\n"
            + (f"PR: {result.pr_url}\n" if result.pr_url else "")
            + (f"Error: {result.error}\n" if result.error else "")
        )


# ==============================================================================
# Main Loop
# ==============================================================================

_running = True


def _shutdown_handler(signum, frame):
    global _running
    logging.info("Received signal %d, shutting down gracefully", signum)
    _running = False


def _apply_dry_run(cfg: dict) -> None:
    """把 cfg 改成 dry-run 状态：开关 + 跳过 30s 默认放行延迟。"""
    cfg["DRY_RUN"] = True
    cfg["DEFAULT_RELEASE_DELAY"] = 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lark Bug Pipeline 串行执行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="处理当前队列一次后立即退出（不进入轮询循环）。"
             " 跳过 evaluate_triggers——无论队列长度都会立刻处理。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：跳过 Claude headless 调用 + worktree 创建，"
             "用合成结果代替。配合 --once 做端到端 mock 排练。",
    )
    parser.add_argument(
        "--seed-queue",
        type=int,
        metavar="N",
        default=0,
        help="启动前往队列写 N 条假数据（仅当 --dry-run 时允许）。",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="跳过单实例锁（让排练 worker 跟真 worker 共存测试）。",
    )
    args = parser.parse_args()

    setup_logging()
    _load_lark_env()

    if args.seed_queue and not args.dry_run:
        logging.error("--seed-queue 必须配合 --dry-run 使用，避免污染真队列")
        return 2

    if args.seed_queue:
        seeded = seed_fake_queue(args.seed_queue)
        logging.info(
            "Seeded %d fake bugs into queue: %s",
            len(seeded), seeded,
        )

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    lock: Optional[WorkerLock]
    if args.no_lock:
        lock = None
    else:
        lock = WorkerLock(WORKER_LOCK)
        if not lock.acquire():
            logging.warning("Another worker is already running, exit")
            return 1

    try:
        logging.info(
            "=== bug-worker started, pid=%d (once=%s dry_run=%s no_lock=%s) ===",
            os.getpid(), args.once, args.dry_run, args.no_lock,
        )

        # ----- 单次处理路径（演练 / 调试用）-----
        if args.once:
            cfg = load_config()
            if args.dry_run:
                _apply_dry_run(cfg)
            entries = filter_unprocessed(read_queue())
            logging.info("--once: %d unprocessed entries", len(entries))
            if entries:
                batch = entries[: cfg["MAX_BATCH"]]
                try:
                    process_batch(batch, cfg)
                except Exception:
                    logging.exception("process_batch crashed")
                    notify_nature(
                        "⚠️ bug-worker process_batch 崩了（--once），看 worker.log"
                    )
            else:
                logging.info("Queue empty, nothing to do")
            logging.info("=== bug-worker --once exiting ===")
            return 0

        # ----- 常驻轮询路径 -----
        while _running:
            cfg = load_config()  # 每轮重载，支持热更新
            if args.dry_run:
                _apply_dry_run(cfg)
            entries = filter_unprocessed(read_queue())
            should_fire, reason = evaluate_triggers(entries, cfg)
            logging.info(
                "Tick: %d entries unprocessed, fire=%s (%s)",
                len(entries), should_fire, reason,
            )
            if should_fire:
                # Claim 一批：取最多 MAX_BATCH 条
                batch = entries[: cfg["MAX_BATCH"]]
                try:
                    process_batch(batch, cfg)
                except Exception:
                    logging.exception("process_batch crashed")
                    notify_nature(
                        "⚠️ bug-worker process_batch 崩了，看 worker.log"
                    )
            # Sleep before next tick (interruptible)
            for _ in range(int(cfg["POLL_INTERVAL"])):
                if not _running:
                    break
                time.sleep(1)
        logging.info("=== bug-worker exiting cleanly ===")
        return 0
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
