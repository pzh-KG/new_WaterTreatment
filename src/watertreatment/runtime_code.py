"""Startup tree identity and a conservative restart gate for web model jobs.

This is a filesystem snapshot taken at package startup, not an attestation of
every byte loaded into Python memory. A second check at Workbench construction
detects edits during subsequent imports. Stable file metadata avoids repeatedly
hashing unchanged files; the model-job boundary always refreshes that metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class RuntimeRestartRequired(ValueError):
    pass


class RuntimeDispatchDeferred(RuntimeRestartRequired):
    """Only top-level dispatch preflight may defer a still-unexecuted job."""


def _signature(root):
    paths = []
    for name in ("src", "config", "prompts"):
        folder = root / name
        if folder.exists():
            paths.extend(path for path in folder.rglob("*") if path.is_file()
                         and path.suffix in {".py", ".json", ".txt", ".toml", ".md", ".html", ".css", ".js"}
                         and "__pycache__" not in path.parts and ".local." not in path.name)
    paths.extend(root / name for name in ("pyproject.toml", "AGENTS.md", "requirements.lock.txt")
                 if (root / name).is_file())
    result = []
    for path in sorted(paths):
        if path.is_symlink() or root not in path.resolve(strict=True).parents:
            raise ValueError("代码树包含指向外部的链接，无法确定启动版本")
        stat = path.stat()
        result.append((path.relative_to(root).as_posix(), stat.st_size,
                       stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino))
    return tuple(result)


def _digest(root, signature):
    items = [{"path": row[0], "sha256": hashlib.sha256((root / row[0]).read_bytes()).hexdigest()}
             for row in signature]
    encoded = json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RuntimeCodeGuard:
    """One immutable startup identity, with a latched restart requirement."""

    def __init__(self, root, *, clock=time.monotonic, check_interval=2.0):
        self.root = Path(root).resolve(strict=True)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.pid = os.getpid()
        self._clock, self._interval = clock, check_interval
        self._lock = threading.RLock()
        self._signature = None
        self._last_check = float("-inf")
        self.startup_tree_digest = self.current_disk_digest = None
        self.restart_required = False
        self.reason = None
        self._startup_complete = False
        try:
            before = _signature(self.root)
            digest = _digest(self.root, before)
            if before != _signature(self.root):
                raise ValueError("启动快照建立期间代码树发生变化")
            self._signature = before
            self.startup_tree_digest = self.current_disk_digest = digest
            self._last_check = self._clock()
        except (OSError, RuntimeError, ValueError) as exc:
            self._block("无法稳定记录启动代码快照：" + str(exc))

    def _block(self, reason):
        self.restart_required = True
        self.reason = self.reason or reason

    def complete_startup(self):
        with self._lock:
            status = self.status(force=True, startup_check=not self._startup_complete)
            self._startup_complete = True
            return status

    def status(self, *, force=False, startup_check=False):
        with self._lock:
            now = self._clock()
            if force or now - self._last_check >= self._interval:
                try:
                    signature = _signature(self.root)
                    if signature != self._signature:
                        if startup_check:
                            self._block("应用导入期间代码树发生变化，需重启以避免混合运行版本")
                        else:
                            self._block("应用启动后代码树文件状态发生变化；即使内容已恢复，也需重启以避免混合运行版本")
                        digest = _digest(self.root, signature)
                        if signature != _signature(self.root):
                            raise ValueError("检查期间代码树持续变化")
                        self._signature = signature
                        self.current_disk_digest = digest
                    if self.current_disk_digest != self.startup_tree_digest:
                        self._block("应用启动后代码、提示词或共享配置已变化；需要重启网页后台")
                except (OSError, RuntimeError, ValueError) as exc:
                    self.current_disk_digest = None
                    self._block("当前代码树无法稳定核验，需要重启网页后台：" + str(exc))
                self._last_check = now
            return {"startup_tree_digest": self.startup_tree_digest,
                    "current_disk_digest": self.current_disk_digest,
                    "started_at": self.started_at, "pid": self.pid,
                    "restart_required": self.restart_required, "reason": self.reason,
                    "model_jobs_allowed": not self.restart_required,
                    "identity_scope": "actual_application_tree_at_package_startup",
                    "identity_algorithm": "runtime-tree-v1",
                    "memory_attestation": False,
                    "interpretation": "启动时实际应用目录的代码树快照；不是 Python 内存代码的逐字节认证。"}

    def require_model_ready(self):
        status = self.status(force=True)
        if status["restart_required"]:
            raise RuntimeRestartRequired((status["reason"] or "运行版本无法确认") +
                "。本次没有启动模型；历史任务和结果保留，请重启后继续。")
        return status


# Import this from __init__ before any application modules. Settings.project_root
# can point at synthetic fixtures or external resources; it is not proof of which
# package supplied the executing Python modules.
PROCESS_CODE_GUARD = RuntimeCodeGuard(Path(__file__).resolve().parents[2])
