#!/usr/bin/env python3
"""团队资源更新 / OTA 构建面板（仅使用 Python 标准库）。"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import getpass
import hashlib
import hmac
import http.cookiejar
import json
import mimetypes
import os
import secrets
import signal
import shlex
import sqlite3
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RES_DIR = BASE_DIR / "res"
CONFIG_DIR = BASE_DIR / "config"
ACCOUNTS_PATH = CONFIG_DIR / "accounts.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ota_tool.db"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = BASE_DIR / "jobs.json"
SESSION_COOKIE = "ota_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
GIT_TIMEOUT_SECONDS = 300
COMPILE_TIMEOUT_SECONDS = 60 * 60
JENKINS_TIMEOUT_SECONDS = 30
JENKINS_POLL_INTERVAL_SECONDS = 3
JENKINS_BUILD_TIMEOUT_SECONDS = 6 * 60 * 60
JENKINS_STATUS_RETRY_ATTEMPTS = 3
JENKINS_CANCEL_TIMEOUT_SECONDS = 60
LOCAL_CANCEL_GRACE_SECONDS = 3
GIT_ABANDONED_INDEX_LOCK_SECONDS = 24 * 60 * 60
GIT_INDEX_LOCK_RETRY_ATTEMPTS = 5
GIT_INDEX_LOCK_RETRY_DELAY_SECONDS = 2
GIT_COMMIT_AUTHOR_EMAIL = "relay_ota@local"
NODE_STDOUT_COMPAT_PATH = BASE_DIR / "node_stdout_compat.js"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                account_revision TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                build_number INTEGER NOT NULL,
                builder_account TEXT NOT NULL,
                builder_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','cancelled','success','failed')),
                parameters_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                duration_seconds REAL,
                error_message TEXT,
                UNIQUE(job_id, build_number)
            );

            CREATE INDEX IF NOT EXISTS idx_builds_job_id ON builds(job_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account);
            PRAGMA user_version = 3;
            """
        )
        build_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='builds'"
        ).fetchone()
        if build_schema and "'cancelled'" not in str(build_schema["sql"]):
            conn.executescript(
                """
                DROP INDEX IF EXISTS idx_builds_job_id;
                ALTER TABLE builds RENAME TO builds_before_cancel_status;
                CREATE TABLE builds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    build_number INTEGER NOT NULL,
                    builder_account TEXT NOT NULL,
                    builder_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','cancelled','success','failed')),
                    parameters_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_seconds REAL,
                    error_message TEXT,
                    UNIQUE(job_id, build_number)
                );
                INSERT INTO builds(
                    id, job_id, build_number, builder_account, builder_name, status,
                    parameters_json, created_at, started_at, finished_at,
                    duration_seconds, error_message
                )
                SELECT
                    id, job_id, build_number, builder_account, builder_name, status,
                    parameters_json, created_at, started_at, finished_at,
                    duration_seconds, error_message
                FROM builds_before_cancel_status;
                DROP TABLE builds_before_cancel_status;
                CREATE INDEX idx_builds_job_id ON builds(job_id, id DESC);
                PRAGMA user_version = 3;
                """
            )


def recover_interrupted_builds() -> None:
    """启动服务时结束上次进程遗留的任务，账号管理命令不会触碰构建状态。"""
    with connect_db() as conn:
        conn.execute(
            """UPDATE builds
               SET status='failed', finished_at=?, error_message='服务器重启，构建已中断'
               WHERE status IN ('queued', 'running', 'cancelling')""",
            (utc_now(),),
        )


class AccountConfigError(RuntimeError):
    """账号配置文件不存在或内容无效。"""


def normalize_account(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("账号必须是字符串")
    account = unicodedata.normalize("NFKC", value).strip()
    if not 2 <= len(account) <= 32:
        raise ValueError("账号长度需为 2～32 个字符")
    if not all(ch.isalnum() or ch in "._-" for ch in account):
        raise ValueError("账号只能包含中文、字母、数字、点、下划线和短横线")
    return account


def normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("显示名称必须是字符串")
    name = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(name) <= 32:
        raise ValueError("显示名称长度需为 1～32 个字符")
    if any(ch in "<>" or unicodedata.category(ch).startswith("C") for ch in name):
        raise ValueError("显示名称不能包含尖括号或控制字符")
    return name


def validate_password(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("密码必须是字符串")
    if len(value) < 8:
        raise ValueError("密码至少需要 8 个字符")
    return value


def save_accounts(accounts: List[Dict[str, Any]]) -> None:
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = ACCOUNTS_PATH.with_name(
        f".{ACCOUNTS_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    payload = {"accounts": accounts}
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, ACCOUNTS_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_accounts_file() -> None:
    if not ACCOUNTS_PATH.exists():
        save_accounts([])


def load_accounts() -> List[Dict[str, Any]]:
    ensure_accounts_file()
    try:
        payload = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountConfigError(f"无法读取 {ACCOUNTS_PATH}：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        raise AccountConfigError("账号配置的顶层必须包含 accounts 数组")

    accounts: List[Dict[str, Any]] = []
    account_keys = set()
    for index, raw in enumerate(payload["accounts"], 1):
        if not isinstance(raw, dict):
            raise AccountConfigError(f"第 {index} 个账号必须是对象")
        try:
            account = normalize_account(raw.get("account"))
            name = normalize_name(raw.get("name"))
            password = validate_password(raw.get("password"))
        except ValueError as exc:
            raise AccountConfigError(f"第 {index} 个账号无效：{exc}") from exc
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AccountConfigError(f"第 {index} 个账号的 enabled 必须是布尔值")
        account_key = account.casefold()
        if account_key in account_keys:
            raise AccountConfigError(f"账号重复：{account}")
        account_keys.add(account_key)
        accounts.append(
            {
                "account": account,
                "name": name,
                "password": password,
                "enabled": enabled,
            }
        )
    return accounts


def find_account(value: Any, include_disabled: bool = False) -> Optional[Dict[str, Any]]:
    try:
        account_key = normalize_account(value).casefold()
    except ValueError:
        return None
    for account in load_accounts():
        if account["account"].casefold() == account_key:
            if account["enabled"] or include_disabled:
                return account
            return None
    return None


def account_revision(account: Dict[str, Any]) -> str:
    identity = json.dumps(
        {
            "account": account["account"],
            "name": account["name"],
            "password": account["password"],
            "enabled": account["enabled"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def password_matches(password: str, account: Optional[Dict[str, Any]]) -> bool:
    expected = account["password"] if account else ""
    actual_digest = hashlib.sha256(password.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return bool(account) and hmac.compare_digest(actual_digest, expected_digest)


def add_user(account: str, password: str, name: Optional[str] = None) -> None:
    account = normalize_account(account)
    display_name = normalize_name(name if name is not None else account)
    password = validate_password(password)
    accounts = load_accounts()
    if any(item["account"].casefold() == account.casefold() for item in accounts):
        raise ValueError("账号已存在")
    accounts.append(
        {
            "account": account,
            "name": display_name,
            "password": password,
            "enabled": True,
        }
    )
    save_accounts(accounts)


def delete_user(account: str) -> None:
    """从账号文件删除账号并清除其会话，构建记录保留身份快照。"""
    account_key = normalize_account(account).casefold()
    accounts = load_accounts()
    removed = next(
        (item for item in accounts if item["account"].casefold() == account_key),
        None,
    )
    if not removed:
        raise ValueError("账号不存在")
    save_accounts(
        [item for item in accounts if item["account"].casefold() != account_key]
    )
    with connect_db() as conn:
        conn.execute("DELETE FROM sessions WHERE account=?", (removed["account"],))


def load_config() -> Dict[str, Dict[str, Any]]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"配置文件不存在：{CONFIG_PATH}")
    project_root_value = os.environ.get("TP_CLIENT_ROOT", "").strip()
    resource_root_value = os.environ.get("TP_RES_ROOT", "").strip()
    if not project_root_value:
        raise RuntimeError("环境变量 TP_CLIENT_ROOT 未设置")
    project_root = Path(os.path.expanduser(project_root_value)).resolve()
    resource_base_root = (
        Path(os.path.expanduser(resource_root_value)).resolve()
        if resource_root_value
        else None
    )
    if not project_root.is_dir():
        raise RuntimeError(f"TP_CLIENT_ROOT 目录不存在：{project_root}")
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    jobs: Dict[str, Dict[str, Any]] = {}
    for raw in payload.get("jobs", []):
        job_id = str(raw.get("id", "")).strip()
        if (
            not job_id
            or job_id in jobs
            or not all(ch.isalnum() or ch in "_-" for ch in job_id)
        ):
            raise RuntimeError(f"Job id 无效或重复：{job_id!r}")
        config = dict(raw)
        resource_subpath = Path(str(config.get("resource_subpath", "")).strip())
        if not resource_subpath.parts or resource_subpath.is_absolute() or ".." in resource_subpath.parts:
            raise RuntimeError(f"Job {job_id} 的资源子路径无效")
        config["project_root"] = str(project_root)
        config["resource_repo_root"] = (
            str(resource_base_root) if resource_base_root else ""
        )
        config["resource_root"] = (
            str((resource_base_root / resource_subpath).resolve())
            if resource_base_root
            else ""
        )
        config.setdefault("resource_branch", "master")
        config.setdefault("display_name", job_id.upper())
        config.setdefault("description", "资源更新与 OTA 构建")
        config.setdefault("exclude_dirs", [".git", "node_modules", "Library", "Temp"])
        config.setdefault("max_search_depth", 8)
        if not str(config.get("branch", "")).strip():
            raise RuntimeError(f"Job {job_id} 未配置主分支")
        if not str(config.get("resource_branch", "")).strip():
            raise RuntimeError(f"Job {job_id} 未配置资源仓库分支")
        if not str(config.get("jenkins_job_url", "")).strip():
            raise RuntimeError(f"Job {job_id} 未配置 Jenkins Job URL")
        jobs[job_id] = config
    return jobs


class PathIndex:
    """按 Job 懒加载资源目录索引，避免每次输入都遍历磁盘。"""

    def __init__(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        self.jobs = jobs
        self._cache: Dict[str, Tuple[float, List[str]]] = {}
        self._lock = threading.Lock()

    def _scan(self, job_id: str) -> List[str]:
        config = self.jobs[job_id]
        root_value = str(config.get("resource_root", "")).strip()
        if not root_value:
            return []
        root = Path(root_value)
        if not root.is_dir():
            return []
        excludes = set(config["exclude_dirs"])
        max_depth = int(config["max_search_depth"])
        result: List[str] = []
        for current, dirs, _files in os.walk(str(root)):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            dirs[:] = [
                name
                for name in dirs
                if name not in excludes and not name.startswith(".") and depth < max_depth
            ]
            if depth > 0:
                result.append(current_path.relative_to(root).as_posix())
            if len(result) >= 50_000:
                break
        return result

    def paths(self, job_id: str) -> List[str]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(job_id)
            if cached and now - cached[0] < 300:
                return cached[1]
            paths = self._scan(job_id)
            self._cache[job_id] = (now, paths)
            return paths

    def search(self, job_id: str, query: str, limit: int = 40) -> List[str]:
        query = query.strip().replace("\\", "/").strip("/").lower()
        paths = self.paths(job_id)
        if not query:
            return sorted(paths, key=lambda p: (p.count("/"), p.lower()))[:limit]

        tokens = [token for token in query.replace("/", " ").split() if token]

        def score(path: str) -> Optional[Tuple[int, int, int, str]]:
            value = path.lower()
            if not all(token in value for token in tokens):
                return None
            basename = value.rsplit("/", 1)[-1]
            exact = 0 if value == query else 1
            base_start = 0 if basename.startswith(query) else 1
            return exact, base_start, len(path), value

        scored = [(current_score, path) for path in paths if (current_score := score(path))]
        scored.sort(key=lambda item: item[0])
        return [path for _score, path in scored[:limit]]


def public_job(config: Dict[str, Any]) -> Dict[str, Any]:
    resource_root = str(config.get("resource_root", "")).strip()
    return {
        "id": config["id"],
        "display_name": config["display_name"],
        "description": config["description"],
        "resource_root_name": Path(resource_root).name if resource_root else "未配置",
    }


def build_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    try:
        result["parameters"] = json.loads(result.pop("parameters_json"))
    except (json.JSONDecodeError, TypeError):
        result["parameters"] = {}
        result.pop("parameters_json", None)
    return result


class PipelineError(RuntimeError):
    def __init__(self, message: str, status: int = HTTPStatus.INTERNAL_SERVER_ERROR) -> None:
        super().__init__(message)
        self.status = status


class BuildCancelled(RuntimeError):
    """当前构建已收到用户终止请求。"""


class BuildControl:
    """保存单次构建的取消信号、当前子进程和 Jenkins 精确地址。"""

    def __init__(self, build_id: int) -> None:
        self.build_id = build_id
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._finished = False
        self.requested_by_account = ""
        self.requested_by_name = ""
        self.jenkins_queue_url = ""
        self.jenkins_build_url = ""

    def request_cancel(self, user: Dict[str, Any]) -> Optional[bool]:
        with self._lock:
            if self._finished:
                return None
            first_request = not self.cancel_event.is_set()
            if first_request:
                self.requested_by_account = str(user["account"])
                self.requested_by_name = str(user["name"])
                self.cancel_event.set()
            process = self._process
        if process is not None:
            _terminate_process_group(process)
        return first_request

    def attach_process(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._process = process
            cancel_requested = self.cancel_event.is_set()
        if cancel_requested:
            _terminate_process_group(process)

    def detach_process(self, process: subprocess.Popen) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def set_jenkins_queue_url(self, url: str) -> None:
        with self._lock:
            self.jenkins_queue_url = url

    def set_jenkins_build_url(self, url: str) -> None:
        with self._lock:
            self.jenkins_build_url = url

    def jenkins_urls(self) -> Tuple[str, str]:
        with self._lock:
            return self.jenkins_queue_url, self.jenkins_build_url

    def is_cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise BuildCancelled("构建已由用户终止")

    def wait(self, seconds: float) -> None:
        if self.cancel_event.wait(seconds):
            raise BuildCancelled("构建已由用户终止")

    def mark_finished(self) -> bool:
        with self._lock:
            self._finished = True
            self._process = None
            return self.cancel_event.is_set()


def _terminate_process_group(process: subprocess.Popen, force: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass


class NoRedirectHandler(HTTPRedirectHandler):
    """保留 Jenkins 构建响应中的 Location，避免自动跳转后丢失队列地址。"""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _log_tail(log: TextIO, limit: int = 1600) -> str:
    log.flush()
    try:
        content = Path(log.name).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return ""
    lines = [line.strip() for line in content[-limit:].splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _command_output_since(log: TextIO, start_offset: int, limit: int = 16_000) -> str:
    log.flush()
    try:
        log_path = Path(log.name)
        end_offset = log_path.stat().st_size
        read_offset = max(start_offset, end_offset - limit)
        with log_path.open("rb") as file:
            file.seek(read_offset)
            return file.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return ""


def run_command(
    command: List[str],
    cwd: Path,
    log: TextIO,
    step: str,
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    cancellation: Optional[BuildControl] = None,
) -> None:
    display = shlex.join(command)
    is_git_command = bool(command) and command[0] == "git"
    max_attempts = 1 + (GIT_INDEX_LOCK_RETRY_ATTEMPTS if is_git_command else 0)
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log.write(
                f"[Git] 自动重试 {attempt - 1}/{GIT_INDEX_LOCK_RETRY_ATTEMPTS}\n"
            )
        log.write(f"\n[{step}] $ {display}\n")
        log.flush()
        try:
            output_offset = Path(log.name).stat().st_size
        except (OSError, TypeError):
            output_offset = 0
        try:
            if cancellation is None:
                result = subprocess.run(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=timeout,
                )
            else:
                cancellation.raise_if_cancelled()
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
                cancellation.attach_process(process)
                deadline = time.monotonic() + timeout
                try:
                    while True:
                        try:
                            return_code = process.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            cancellation.raise_if_cancelled()
                            if time.monotonic() >= deadline:
                                _terminate_process_group(process)
                                try:
                                    process.wait(timeout=LOCAL_CANCEL_GRACE_SECONDS)
                                except subprocess.TimeoutExpired:
                                    _terminate_process_group(process, force=True)
                                    process.wait()
                                raise subprocess.TimeoutExpired(command, timeout)
                    cancellation.raise_if_cancelled()
                    result = subprocess.CompletedProcess(command, return_code)
                except BuildCancelled:
                    log.write(f"[终止] 正在停止当前命令：{display}\n")
                    log.flush()
                    _terminate_process_group(process)
                    try:
                        process.wait(timeout=LOCAL_CANCEL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        _terminate_process_group(process, force=True)
                        process.wait()
                    raise
                finally:
                    cancellation.detach_process(process)
        except FileNotFoundError as exc:
            raise PipelineError(f"{step}失败：找不到命令 {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(f"{step}超时（{timeout} 秒）") from exc
        except OSError as exc:
            raise PipelineError(f"{step}失败：{exc}") from exc
        if result.returncode == 0:
            return

        command_output = _command_output_since(log, output_offset)
        lock_paths = [path for path in _git_index_lock_paths(cwd) if path.exists()]
        lock_message = command_output.lower()
        has_lock_conflict = bool(lock_paths) or (
            "index.lock" in lock_message and "file exists" in lock_message
        )
        detail = _log_tail(log)
        suffix = f"：{detail}" if detail else ""
        error = PipelineError(f"{step}失败（退出码 {result.returncode}）{suffix}")
        if not is_git_command or not has_lock_conflict:
            raise error

        log.write(
            f"[Git] 检测到 index.lock 冲突，正在自动处理"
            f"（{attempt}/{max_attempts}）\n"
        )
        clear_stale_git_index_locks(cwd, log)
        remaining_locks = [
            path for path in _git_index_lock_paths(cwd) if path.exists()
        ]
        if attempt >= max_attempts:
            log.write("[Git] index.lock 冲突自动重试次数已用尽\n")
            log.flush()
            raise error
        if remaining_locks:
            log.write(
                f"[Git] 锁仍被占用，{GIT_INDEX_LOCK_RETRY_DELAY_SECONDS} 秒后重试\n"
            )
            log.flush()
            time.sleep(GIT_INDEX_LOCK_RETRY_DELAY_SECONDS)


def _git_index_lock_paths(project_root: Path) -> List[Path]:
    git_dir = project_root / ".git"
    lock_paths = [git_dir / "index.lock"]
    if git_dir.is_dir():
        lock_paths.extend(git_dir.glob("modules/**/index.lock"))
    return sorted(set(lock_paths))


def _git_lock_in_use(lock_path: Path) -> Optional[bool]:
    """通过 lsof 判断锁文件是否仍被进程持有；无法判断时返回 None。"""
    try:
        result = subprocess.run(
            ["lsof", "-t", "--", str(lock_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def clear_stale_git_index_locks(project_root: Path, log: TextIO) -> None:
    """清理主仓库和递归子模块中已确认无人占用的 index.lock。"""
    now = time.time()
    for lock_path in _git_index_lock_paths(project_root):
        try:
            lock_stat = lock_path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PipelineError(f"检查 Git 锁文件失败：{lock_path}：{exc}") from exc

        age_seconds = max(0.0, now - lock_stat.st_mtime)
        in_use = _git_lock_in_use(lock_path)
        if in_use is True:
            log.write(f"[Git] 锁文件仍被进程使用，暂不清理：{lock_path}\n")
            continue
        if in_use is None and age_seconds < GIT_ABANDONED_INDEX_LOCK_SECONDS:
            log.write(f"[Git] 无法确认锁文件是否仍在使用，暂不清理：{lock_path}\n")
            continue

        try:
            current_stat = lock_path.stat()
            if (
                current_stat.st_ino != lock_stat.st_ino
                or current_stat.st_mtime_ns != lock_stat.st_mtime_ns
            ):
                log.write(f"[Git] 锁文件已发生变化，暂不清理：{lock_path}\n")
                continue
            lock_path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PipelineError(f"清理残留 Git 锁文件失败：{lock_path}：{exc}") from exc
        log.write(
            f"[Git] 已自动清理残留锁文件：{lock_path}"
            f"（{round(age_seconds)} 秒）\n"
        )
    log.flush()


def _jenkins_headers() -> Dict[str, str]:
    user = os.environ.get("JENKINS_USER", "").strip()
    api_token = os.environ.get("JENKINS_API_TOKEN", "").strip()
    if bool(user) != bool(api_token):
        raise PipelineError("Jenkins 鉴权配置不完整，请同时设置 JENKINS_USER 和 JENKINS_API_TOKEN")
    headers = {"Accept": "application/json"}
    if user and api_token:
        encoded = base64.b64encode(f"{user}:{api_token}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def _jenkins_error(exc: HTTPError) -> str:
    try:
        body = exc.read(2000).decode("utf-8", errors="replace")
    except OSError:
        body = ""
    body = " ".join(body.split())
    return body[:500] or str(exc.reason)


def trigger_jenkins(config: Dict[str, Any], log: TextIO) -> str:
    job_url = str(config["jenkins_job_url"]).rstrip("/")
    headers = _jenkins_headers()
    cookies = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies), NoRedirectHandler())

    tree = "actions[parameterDefinitions[name,type,_class]]"
    api_url = f"{job_url}/api/json?{urlencode({'tree': tree})}"
    try:
        with opener.open(Request(api_url, headers=headers), timeout=JENKINS_TIMEOUT_SECONDS) as response:
            job_info = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except HTTPError as exc:
        raise PipelineError(
            f"读取 Jenkins Job 参数失败（HTTP {exc.code}）：{_jenkins_error(exc)}",
            HTTPStatus.BAD_GATEWAY,
        ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PipelineError(f"读取 Jenkins Job 参数失败：{exc}", HTTPStatus.BAD_GATEWAY) from exc

    parameter_definitions: List[Dict[str, Any]] = []
    for action in job_info.get("actions", []):
        if isinstance(action, dict):
            definitions = action.get("parameterDefinitions", [])
            if isinstance(definitions, list):
                parameter_definitions.extend(item for item in definitions if isinstance(item, dict))

    parameters: Dict[str, str] = {}
    for definition in parameter_definitions:
        name = str(definition.get("name", "")).strip()
        if not name:
            continue
        parameter_type = str(definition.get("type") or definition.get("_class") or "")
        parameters[name] = "false" if "Boolean" in parameter_type else ""
    parameters["branch"] = str(config.get("jenkins_branch", "beta"))
    parameters["alert"] = "true"

    parsed = urlparse(job_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    first_job_part = next(
        (index for index, part in enumerate(path_parts) if part in ("job", "view")),
        len(path_parts),
    )
    context_path = "/" + "/".join(path_parts[:first_job_part]) if first_job_part else ""
    jenkins_root = f"{parsed.scheme}://{parsed.netloc}{context_path}"
    crumb_url = f"{jenkins_root}/crumbIssuer/api/json"
    try:
        with opener.open(Request(crumb_url, headers=headers), timeout=JENKINS_TIMEOUT_SECONDS) as response:
            crumb = json.loads(response.read(64 * 1024).decode("utf-8"))
            field = str(crumb.get("crumbRequestField", "")).strip()
            value = str(crumb.get("crumb", "")).strip()
            if field and value:
                headers[field] = value
    except HTTPError as exc:
        if exc.code != HTTPStatus.NOT_FOUND:
            raise PipelineError(
                f"获取 Jenkins CSRF Token 失败（HTTP {exc.code}）：{_jenkins_error(exc)}",
                HTTPStatus.BAD_GATEWAY,
            ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PipelineError(f"获取 Jenkins CSRF Token 失败：{exc}", HTTPStatus.BAD_GATEWAY) from exc

    build_url = f"{job_url}/buildWithParameters"
    request_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    request = Request(
        build_url,
        data=urlencode(parameters).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    queue_url = ""
    try:
        with opener.open(request, timeout=JENKINS_TIMEOUT_SECONDS) as response:
            queue_url = response.headers.get("Location", "")
            if not queue_url and "/queue/item/" in response.geturl():
                queue_url = response.geturl()
    except HTTPError as exc:
        location = exc.headers.get("Location", "")
        is_legacy_success = exc.code in (301, 302, 303, 307, 308) and "login" not in location.lower()
        if not is_legacy_success:
            raise PipelineError(
                f"触发 Jenkins OTA 失败（HTTP {exc.code}）：{_jenkins_error(exc)}",
                HTTPStatus.BAD_GATEWAY,
            ) from exc
        queue_url = location
    except (URLError, TimeoutError) as exc:
        raise PipelineError(f"触发 Jenkins OTA 失败：{exc}", HTTPStatus.BAD_GATEWAY) from exc

    queue_url = urljoin(job_url + "/", queue_url) if queue_url else ""
    log.write("\n[Jenkins] OTA 构建请求已受理\n")
    log.write(f"参数：{json.dumps(parameters, ensure_ascii=False)}\n")
    if queue_url:
        log.write(f"队列地址：{queue_url}\n")
    log.flush()
    return queue_url


def jenkins_get_json(url: str) -> Dict[str, Any]:
    try:
        with build_opener().open(
            Request(url, headers=_jenkins_headers()), timeout=JENKINS_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except HTTPError as exc:
        raise PipelineError(
            f"查询 Jenkins 构建状态失败（HTTP {exc.code}）：{_jenkins_error(exc)}",
            HTTPStatus.BAD_GATEWAY,
        ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PipelineError(f"查询 Jenkins 构建状态失败：{exc}", HTTPStatus.BAD_GATEWAY) from exc
    if not isinstance(payload, dict):
        raise PipelineError("Jenkins 状态响应格式无效", HTTPStatus.BAD_GATEWAY)
    return payload


def jenkins_status_json(
    url: str,
    log: TextIO,
    cancellation: Optional[BuildControl] = None,
) -> Dict[str, Any]:
    for attempt in range(1, JENKINS_STATUS_RETRY_ATTEMPTS + 1):
        if cancellation:
            cancellation.raise_if_cancelled()
        try:
            return jenkins_get_json(url)
        except PipelineError as exc:
            if attempt >= JENKINS_STATUS_RETRY_ATTEMPTS:
                raise
            log.write(
                f"[Jenkins] 状态查询失败，{JENKINS_POLL_INTERVAL_SECONDS} 秒后重试"
                f"（{attempt}/{JENKINS_STATUS_RETRY_ATTEMPTS}）：{exc}\n"
            )
            log.flush()
            if cancellation:
                cancellation.wait(JENKINS_POLL_INTERVAL_SECONDS)
            else:
                time.sleep(JENKINS_POLL_INTERVAL_SECONDS)
    raise PipelineError("查询 Jenkins 构建状态失败")


def jenkins_instance_url(instance_url: str, returned_url: str) -> str:
    """Jenkins 可能返回 localhost URL，统一改用最初请求的实例地址。"""
    instance = urlparse(instance_url)
    candidate = urlparse(urljoin(instance_url, returned_url))
    return candidate._replace(scheme=instance.scheme, netloc=instance.netloc).geturl()


def _jenkins_root_url(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    marker = next(
        (index for index, part in enumerate(path_parts) if part in ("job", "view", "queue")),
        len(path_parts),
    )
    context_path = "/" + "/".join(path_parts[:marker]) if marker else ""
    return f"{parsed.scheme}://{parsed.netloc}{context_path}"


def jenkins_post_action(url: str, log: TextIO, action: str) -> None:
    headers = _jenkins_headers()
    cookies = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies), NoRedirectHandler())
    crumb_url = f"{_jenkins_root_url(url)}/crumbIssuer/api/json"
    try:
        with opener.open(Request(crumb_url, headers=headers), timeout=JENKINS_TIMEOUT_SECONDS) as response:
            crumb = json.loads(response.read(64 * 1024).decode("utf-8"))
            field = str(crumb.get("crumbRequestField", "")).strip()
            value = str(crumb.get("crumb", "")).strip()
            if field and value:
                headers[field] = value
    except HTTPError as exc:
        if exc.code != HTTPStatus.NOT_FOUND:
            raise PipelineError(
                f"{action}失败，获取 Jenkins CSRF Token 时返回 HTTP {exc.code}：{_jenkins_error(exc)}",
                HTTPStatus.BAD_GATEWAY,
            ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PipelineError(f"{action}失败，无法获取 Jenkins CSRF Token：{exc}") from exc

    request = Request(
        url,
        data=b"",
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=JENKINS_TIMEOUT_SECONDS):
            pass
    except HTTPError as exc:
        location = exc.headers.get("Location", "")
        redirected = exc.code in (301, 302, 303, 307, 308) and "login" not in location.lower()
        if not redirected:
            raise PipelineError(
                f"{action}失败（HTTP {exc.code}）：{_jenkins_error(exc)}",
                HTTPStatus.BAD_GATEWAY,
            ) from exc
    except (URLError, TimeoutError) as exc:
        raise PipelineError(f"{action}失败：{exc}", HTTPStatus.BAD_GATEWAY) from exc


def stop_jenkins_build(build_url: str, log: TextIO) -> None:
    stop_url = f"{build_url.rstrip('/')}/stop"
    log.write(f"[终止] 请求 Jenkins 停止构建：{build_url}\n")
    log.flush()
    jenkins_post_action(stop_url, log, "停止 Jenkins 构建")

    deadline = time.monotonic() + JENKINS_CANCEL_TIMEOUT_SECONDS
    build_api = f"{build_url.rstrip('/')}/api/json?{urlencode({'tree': 'building,result'})}"
    while time.monotonic() < deadline:
        build_info = jenkins_status_json(build_api, log)
        if not build_info.get("building"):
            result = str(build_info.get("result") or "已停止").upper()
            log.write(f"[终止] Jenkins 构建已结束，结果：{result}\n")
            log.flush()
            return
        time.sleep(JENKINS_POLL_INTERVAL_SECONDS)
    raise PipelineError(
        f"已请求停止 Jenkins 构建，但 {JENKINS_CANCEL_TIMEOUT_SECONDS} 秒内未确认结束"
    )


def cancel_jenkins_queue(queue_url: str, log: TextIO) -> None:
    queue_parts = [part for part in urlparse(queue_url).path.split("/") if part]
    try:
        queue_index = queue_parts.index("queue")
        queue_id = queue_parts[queue_index + 2]
    except (ValueError, IndexError):
        raise PipelineError(f"无法从 Jenkins 队列地址中识别队列编号：{queue_url}")

    queue_api = f"{queue_url.rstrip('/')}/api/json?{urlencode({'tree': 'cancelled,executable[number,url]'})}"
    queue_info = jenkins_get_json(queue_api)
    executable = queue_info.get("executable")
    if isinstance(executable, dict) and executable.get("url"):
        stop_jenkins_build(
            jenkins_instance_url(queue_url, str(executable["url"])), log
        )
        return
    if queue_info.get("cancelled"):
        log.write("[终止] Jenkins 队列任务已经取消\n")
        log.flush()
        return

    cancel_url = f"{_jenkins_root_url(queue_url)}/queue/cancelItem?{urlencode({'id': queue_id})}"
    log.write(f"[终止] 请求 Jenkins 取消队列任务：{queue_url}\n")
    log.flush()
    try:
        jenkins_post_action(cancel_url, log, "取消 Jenkins 队列任务")
    except PipelineError:
        latest = jenkins_get_json(queue_api)
        executable = latest.get("executable")
        if isinstance(executable, dict) and executable.get("url"):
            stop_jenkins_build(
                jenkins_instance_url(queue_url, str(executable["url"])), log
            )
            return
        if latest.get("cancelled"):
            return
        raise
    log.write("[终止] Jenkins 队列取消请求已受理\n")
    log.flush()


def cancel_remote_jenkins(control: BuildControl, log: TextIO) -> None:
    queue_url, build_url = control.jenkins_urls()
    if build_url:
        stop_jenkins_build(build_url, log)
    elif queue_url:
        cancel_jenkins_queue(queue_url, log)


def wait_for_jenkins(
    queue_url: str,
    log: TextIO,
    on_started: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancellation: Optional[BuildControl] = None,
) -> Dict[str, Any]:
    if not queue_url:
        raise PipelineError("Jenkins 未返回队列地址，无法跟踪构建结果", HTTPStatus.BAD_GATEWAY)

    deadline = time.monotonic() + JENKINS_BUILD_TIMEOUT_SECONDS
    queue_api = f"{queue_url.rstrip('/')}/api/json?{urlencode({'tree': 'cancelled,why,executable[number,url]'})}"
    build_url = ""
    build_number: Optional[int] = None
    last_wait_reason = ""
    log.write("[Jenkins] 等待任务离开队列…\n")
    log.flush()
    while time.monotonic() < deadline:
        queue_info = jenkins_status_json(queue_api, log, cancellation)
        if queue_info.get("cancelled"):
            raise PipelineError("Jenkins 队列任务已取消")
        executable = queue_info.get("executable")
        if isinstance(executable, dict) and executable.get("url"):
            build_url = jenkins_instance_url(queue_url, str(executable["url"]))
            try:
                build_number = int(executable.get("number"))
            except (TypeError, ValueError):
                build_number = None
            if on_started:
                on_started(
                    {
                        "jenkins_build_url": build_url,
                        "jenkins_build_number": build_number,
                    }
                )
            break
        wait_reason = str(queue_info.get("why") or "等待 Jenkins 分配执行器")
        if wait_reason != last_wait_reason:
            log.write(f"[Jenkins] {wait_reason}\n")
            log.flush()
            last_wait_reason = wait_reason
        if cancellation:
            cancellation.wait(JENKINS_POLL_INTERVAL_SECONDS)
        else:
            time.sleep(JENKINS_POLL_INTERVAL_SECONDS)
    if not build_url:
        raise PipelineError(f"等待 Jenkins 任务进入构建阶段超时（{JENKINS_BUILD_TIMEOUT_SECONDS} 秒）")

    log.write(f"[Jenkins] 开始执行构建：{build_url}\n")
    log.flush()
    build_api = f"{build_url.rstrip('/')}/api/json?{urlencode({'tree': 'number,url,building,result,duration'})}"
    while time.monotonic() < deadline:
        build_info = jenkins_status_json(build_api, log, cancellation)
        result = str(build_info.get("result") or "").upper()
        if not build_info.get("building") and result:
            duration_ms = build_info.get("duration")
            try:
                jenkins_duration = round(float(duration_ms) / 1000, 2)
            except (TypeError, ValueError):
                jenkins_duration = None
            log.write(f"[Jenkins] 构建结束，结果：{result}\n")
            log.flush()
            return {
                "jenkins_build_url": jenkins_instance_url(
                    queue_url, str(build_info.get("url") or build_url)
                ),
                "jenkins_build_number": build_info.get("number", build_number),
                "jenkins_result": result,
                "jenkins_duration_seconds": jenkins_duration,
            }
        if cancellation:
            cancellation.wait(JENKINS_POLL_INTERVAL_SECONDS)
        else:
            time.sleep(JENKINS_POLL_INTERVAL_SECONDS)
    raise PipelineError(f"等待 Jenkins 构建结束超时（{JENKINS_BUILD_TIMEOUT_SECONDS} 秒）")


def execute_build_pipeline(
    config: Dict[str, Any],
    resource_paths: List[str],
    note: str,
    builder_name: str,
    log: TextIO,
    cancellation: Optional[BuildControl] = None,
) -> str:
    project_root = Path(config["project_root"])
    branch = str(config["branch"])
    resource_repo_root_value = str(config.get("resource_repo_root", "")).strip()
    if not resource_repo_root_value:
        raise PipelineError("资源仓库根路径未配置，请设置环境变量 TP_RES_ROOT")
    resource_repo_root = Path(resource_repo_root_value)
    resource_branch = str(config.get("resource_branch", "master")).strip()
    compile_file = project_root / "compile.coffee"
    git_dir = project_root / ".git"
    resource_git_dir = resource_repo_root / ".git"
    if not git_dir.exists():
        raise PipelineError(f"项目根路径不是 Git 仓库：{project_root}")
    if not resource_repo_root.is_dir():
        raise PipelineError(f"资源仓库根路径不存在：{resource_repo_root}")
    if not resource_git_dir.exists():
        raise PipelineError(f"资源仓库根路径不是 Git 仓库：{resource_repo_root}")
    if not compile_file.is_file():
        raise PipelineError(f"找不到资源编译脚本：{compile_file}")

    command_env = os.environ.copy()
    command_env["GIT_TERMINAL_PROMPT"] = "0"
    def run_pipeline_command(
        command: List[str],
        cwd: Path,
        command_log: TextIO,
        step: str,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        run_command(
            command, cwd, command_log, step, timeout, env,
            cancellation=cancellation,
        )

    if cancellation:
        cancellation.raise_if_cancelled()
    log.write(f"[资源仓库] 清理并更新 {resource_branch} 分支\n")
    log.flush()
    clear_stale_git_index_locks(resource_repo_root, log)
    run_pipeline_command(
        ["git", "reset", "--hard"],
        resource_repo_root,
        log,
        "清理资源仓库本地修改",
        GIT_TIMEOUT_SECONDS,
        command_env,
    )
    run_pipeline_command(
        ["git", "clean", "-fd"],
        resource_repo_root,
        log,
        "清理资源仓库未跟踪文件",
        GIT_TIMEOUT_SECONDS,
        command_env,
    )
    run_pipeline_command(
        ["git", "fetch", "origin", resource_branch],
        resource_repo_root,
        log,
        "拉取资源仓库远端分支",
        GIT_TIMEOUT_SECONDS,
        command_env,
    )
    run_pipeline_command(
        ["git", "checkout", "-B", resource_branch, f"origin/{resource_branch}"],
        resource_repo_root,
        log,
        "切换资源仓库主分支",
        GIT_TIMEOUT_SECONDS,
        command_env,
    )
    run_pipeline_command(
        ["git", "pull", "--ff-only", "origin", resource_branch],
        resource_repo_root,
        log,
        "更新资源仓库主分支",
        GIT_TIMEOUT_SECONDS,
        command_env,
    )

    log.write(f"[客户端仓库] 清理并更新 {branch} 分支\n")
    log.flush()
    clear_stale_git_index_locks(project_root, log)
    run_pipeline_command(["git", "reset", "--hard"], project_root, log, "清理本地修改", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "clean", "-fd"], project_root, log, "清理未跟踪文件", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "submodule", "foreach", "--recursive", "git reset --hard"], project_root, log, "清理子模块本地修改", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "submodule", "foreach", "--recursive", "git clean -fd"], project_root, log, "清理子模块未跟踪文件", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "fetch", "origin", branch], project_root, log, "拉取远端分支", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "checkout", "-B", branch, f"origin/{branch}"], project_root, log, "切换主分支", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "pull", "--ff-only", "origin", branch], project_root, log, "更新主分支", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "submodule", "sync", "--recursive"], project_root, log, "同步子模块配置", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "submodule", "update", "--init", "--recursive", "--force"], project_root, log, "更新子模块", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "submodule", "foreach", "--recursive", "git reset --hard"], project_root, log, "确认子模块无本地修改", GIT_TIMEOUT_SECONDS, command_env)
    run_pipeline_command(["git", "submodule", "foreach", "--recursive", "git clean -fd"], project_root, log, "确认子模块无未跟踪文件", GIT_TIMEOUT_SECONDS, command_env)

    compile_command = ["coffee", "compile.coffee", "res"]
    for resource_path in resource_paths:
        compile_command.extend(["-d", resource_path])
    compile_env = command_env.copy()
    node_options = compile_env.get("NODE_OPTIONS", "").strip()
    compat_option = f"--require={NODE_STDOUT_COMPAT_PATH}"
    compile_env["NODE_OPTIONS"] = " ".join(filter(None, (node_options, compat_option)))
    run_pipeline_command(compile_command, project_root, log, "资源编译", COMPILE_TIMEOUT_SECONDS, compile_env)

    run_pipeline_command(["git", "add", "-A"], project_root, log, "暂存资源修改", GIT_TIMEOUT_SECONDS, command_env)
    commit_message = f"res:{note}" if note else "res"
    commit_author = f"relay_ota({builder_name}) <{GIT_COMMIT_AUTHOR_EMAIL}>"
    run_pipeline_command(
        ["git", "commit", "--author", commit_author, "-m", commit_message],
        project_root,
        log,
        "提交资源修改",
        GIT_TIMEOUT_SECONDS,
        command_env,
    )
    run_pipeline_command(["git", "push", "origin", f"HEAD:{branch}"], project_root, log, "推送资源修改", GIT_TIMEOUT_SECONDS, command_env)
    if cancellation:
        cancellation.raise_if_cancelled()
    return trigger_jenkins(config, log)


def get_build_record(build_id: int) -> Optional[Dict[str, Any]]:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
    return build_to_dict(row) if row else None


def create_build_record(
    job_id: str,
    user: Dict[str, Any],
    parameters: Dict[str, Any],
) -> Dict[str, Any]:
    created_at = utc_now()
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        build_number = conn.execute(
            "SELECT COALESCE(MAX(build_number), 0) + 1 FROM builds WHERE job_id=?",
            (job_id,),
        ).fetchone()[0]
        cursor = conn.execute(
            """INSERT INTO builds(
                   job_id, build_number, builder_account, builder_name, status,
                   parameters_json, created_at, started_at, finished_at,
                   duration_seconds, error_message
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                build_number,
                user["account"],
                user["name"],
                "queued",
                json.dumps(parameters, ensure_ascii=False),
                created_at,
                None,
                None,
                None,
                None,
            ),
        )
        build_id = int(cursor.lastrowid)
    build = get_build_record(build_id)
    assert build is not None
    return build


def mark_build_running(build_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            """UPDATE builds SET status='running', started_at=COALESCE(started_at, ?)
               WHERE id=? AND status='queued'""",
            (utc_now(), build_id),
        )


def update_build_parameters(build_id: int, parameters: Dict[str, Any]) -> None:
    with connect_db() as conn:
        conn.execute(
            "UPDATE builds SET parameters_json=? WHERE id=?",
            (json.dumps(parameters, ensure_ascii=False), build_id),
        )


def finish_build(build_id: int, status: str, duration_seconds: float, error_message: Optional[str]) -> None:
    with connect_db() as conn:
        conn.execute(
            """UPDATE builds
               SET status=?, finished_at=?, duration_seconds=?, error_message=?
               WHERE id=?""",
            (status, utc_now(), duration_seconds, error_message, build_id),
        )


def mark_build_cancelling(build_id: int) -> bool:
    with connect_db() as conn:
        cursor = conn.execute(
            """UPDATE builds SET status='cancelling'
               WHERE id=? AND status IN ('queued', 'running')""",
            (build_id,),
        )
    return cursor.rowcount > 0


class BuildManager:
    """串行执行共享工作区上的构建，并在后台跟踪 Jenkins 最终结果。"""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ota-build")
        self._lock = threading.Lock()
        self._pending: List[int] = []
        self._controls: Dict[int, BuildControl] = {}
        self._futures: Dict[int, Any] = {}

    def enqueue(self, build_id: int) -> Dict[str, Any]:
        control = BuildControl(build_id)
        with self._lock:
            starts_immediately = not self._pending
            self._pending.append(build_id)
            self._controls[build_id] = control
            if starts_immediately:
                mark_build_running(build_id)
            try:
                self._futures[build_id] = self._executor.submit(
                    self._run, build_id, control
                )
            except Exception:
                self._pending.remove(build_id)
                self._controls.pop(build_id, None)
                raise
        build = get_build_record(build_id)
        assert build is not None
        return build

    def cancel(self, build_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
        build = get_build_record(build_id)
        if not build:
            raise PipelineError("构建记录不存在", HTTPStatus.NOT_FOUND)
        if build["status"] == "cancelled":
            return build
        if build["status"] in ("success", "failed"):
            raise PipelineError("构建已经结束，无法终止", HTTPStatus.CONFLICT)

        with self._lock:
            control = self._controls.get(build_id)
            future = self._futures.get(build_id)
            if not control or not future:
                raise PipelineError("构建任务已不在运行队列中", HTTPStatus.CONFLICT)
            cancellation_state = control.request_cancel(user)
            if cancellation_state is None:
                raise PipelineError("构建正在结束，无法再终止", HTTPStatus.CONFLICT)
            cancelled_before_start = future.cancel()

        mark_build_cancelling(build_id)
        if cancelled_before_start:
            log_path = LOG_DIR / f"build-{build_id}.log"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"[{utc_now()}] [终止] {user['name']}（{user['account']}）"
                    "终止了尚未开始执行的构建\n"
                )
            finish_build(build_id, "cancelled", 0, None)
            control.mark_finished()
            with self._lock:
                if build_id in self._pending:
                    self._pending.remove(build_id)
                self._controls.pop(build_id, None)
                self._futures.pop(build_id, None)

        result = get_build_record(build_id)
        assert result is not None
        return result

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, build_id: int, control: BuildControl) -> None:
        start_clock = time.monotonic()
        error_message: Optional[str] = None
        status = "failed"
        cancelled = False
        log_path = LOG_DIR / f"build-{build_id}.log"
        try:
            mark_build_running(build_id)
            control.raise_if_cancelled()
            build = get_build_record(build_id)
            if not build:
                raise PipelineError("本地构建记录不存在")
            parameters = dict(build["parameters"])
            resource_paths = list(parameters.get("resource_paths") or [])
            note = str(parameters.get("note") or "")
            config = JOBS[build["job_id"]]
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"[{utc_now()}] 开始执行 {build['job_id']} 资源更新流水线\n")
                log.write(json.dumps(parameters, ensure_ascii=False, indent=2) + "\n")
                log.flush()
                queue_url = execute_build_pipeline(
                    config,
                    resource_paths,
                    note,
                    str(build["builder_name"]),
                    log,
                    cancellation=control,
                )
                control.set_jenkins_queue_url(queue_url)
                parameters["jenkins_queue_url"] = queue_url
                update_build_parameters(build_id, parameters)
                control.raise_if_cancelled()

                def record_jenkins_start(details: Dict[str, Any]) -> None:
                    control.set_jenkins_build_url(str(details.get("jenkins_build_url") or ""))
                    parameters.update(details)
                    update_build_parameters(build_id, parameters)

                jenkins_details = wait_for_jenkins(
                    queue_url,
                    log,
                    record_jenkins_start,
                    cancellation=control,
                )
                control.raise_if_cancelled()
                parameters.update(jenkins_details)
                update_build_parameters(build_id, parameters)
                if jenkins_details["jenkins_result"] != "SUCCESS":
                    raise PipelineError(
                        f"Jenkins OTA 构建失败，结果：{jenkins_details['jenkins_result']}"
                    )
                log.write("\n[完成] 资源已提交，Jenkins OTA 构建成功\n")
                log.flush()
                status = "success"
        except BuildCancelled:
            cancelled = True
        except PipelineError as exc:
            if control.is_cancel_requested():
                cancelled = True
            else:
                error_message = str(exc)
        except Exception as exc:
            if control.is_cancel_requested():
                cancelled = True
            else:
                error_message = f"构建服务内部错误：{type(exc).__name__}: {exc}"
        finally:
            cancelled = control.mark_finished() or cancelled
            if cancelled:
                try:
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(
                            f"\n[{utc_now()}] [终止] "
                            f"{control.requested_by_name}（{control.requested_by_account}）"
                            "请求终止构建\n"
                        )
                        log.write("[终止] 已执行的 Git 操作将原样保留，不执行回滚或清理\n")
                        log.flush()
                        cancel_remote_jenkins(control, log)
                        log.write("[终止] 构建已终止\n")
                    status = "cancelled"
                    error_message = None
                except PipelineError as exc:
                    error_message = f"终止构建失败：{exc}"
                    status = "failed"
            if error_message:
                try:
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(f"\n[失败] {error_message}\n")
                except OSError:
                    pass
            finish_build(
                build_id,
                status,
                round(time.monotonic() - start_clock, 2),
                error_message,
            )
            with self._lock:
                if build_id in self._pending:
                    self._pending.remove(build_id)
                self._controls.pop(build_id, None)
                self._futures.pop(build_id, None)


JOBS: Dict[str, Dict[str, Any]] = {}
PATH_INDEX: Optional[PathIndex] = None
BUILD_MANAGER: Optional[BuildManager] = None


class AppHandler(BaseHTTPRequestHandler):
    server_version = "OTABuildServer/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format_string % args}")

    def _json(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length 无效")
        if length <= 0 or length > 64 * 1024:
            raise ValueError("请求内容为空或过大")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("请求必须使用 application/json")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("JSON 格式无效")
        if not isinstance(payload, dict):
            raise ValueError("JSON 顶层必须是对象")
        return payload

    def _session_token(self) -> Optional[str]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_user(self) -> Optional[Dict[str, Any]]:
        token = self._session_token()
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        now = int(time.time())
        with connect_db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            row = conn.execute(
                """SELECT account, account_revision FROM sessions
                   WHERE token_hash=? AND expires_at>=?""",
                (token_hash, now),
            ).fetchone()
        if not row:
            return None
        account = find_account(row["account"])
        if not account or account_revision(account) != row["account_revision"]:
            with connect_db() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            return None
        return {"account": account["account"], "name": account["name"]}

    def _require_user(self) -> Optional[Dict[str, Any]]:
        user = self._current_user()
        if not user:
            self._error(HTTPStatus.UNAUTHORIZED, "请先登录")
        return user

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.netloc == self.headers.get("Host") and parsed.scheme in ("http", "https")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path.startswith("/api/"):
                self._handle_api_get(path, parse_qs(parsed.query))
            else:
                self._serve_static(path)
        except AccountConfigError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"账号配置错误：{exc}")

    def do_POST(self) -> None:
        if not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "请求来源无效")
            return
        path = unquote(urlparse(self.path).path)
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        try:
            if path == "/api/login":
                self._login(payload)
            elif path == "/api/logout":
                self._logout()
            elif path.startswith("/api/builds/") and path.endswith("/cancel"):
                user = self._require_user()
                parts = path.strip("/").split("/")
                if user and len(parts) == 4 and parts[2].isdigit():
                    self._cancel_build(int(parts[2]), user)
                elif user:
                    self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            elif path.startswith("/api/jobs/") and path.endswith("/builds"):
                user = self._require_user()
                if user:
                    self._create_build(path.split("/")[3], payload, user)
            else:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        except AccountConfigError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"账号配置错误：{exc}")

    def _handle_api_get(self, path: str, query: Dict[str, List[str]]) -> None:
        user = self._require_user()
        if not user:
            return
        if path == "/api/me":
            self._json(HTTPStatus.OK, {"user": user})
            return
        if path == "/api/jobs":
            self._list_jobs()
            return
        if path.startswith("/api/builds/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[2].isdigit():
                self._get_build(int(parts[2]))
                return
            if len(parts) == 4 and parts[2].isdigit() and parts[3] == "log":
                self._get_build_log(int(parts[2]))
                return
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job_id = parts[2] if len(parts) >= 3 else ""
            if job_id not in JOBS:
                self._error(HTTPStatus.NOT_FOUND, "Job 不存在")
                return
            if len(parts) == 3:
                self._json(HTTPStatus.OK, {"job": public_job(JOBS[job_id])})
                return
            if len(parts) == 4 and parts[3] == "builds":
                self._list_builds(job_id, query)
                return
            if len(parts) == 4 and parts[3] == "paths":
                search_query = query.get("q", [""])[0][:200]
                assert PATH_INDEX is not None
                self._json(HTTPStatus.OK, {"paths": PATH_INDEX.search(job_id, search_query)})
                return
        self._error(HTTPStatus.NOT_FOUND, "接口不存在")

    def _login(self, payload: Dict[str, Any]) -> None:
        account_value = payload.get("account", "")
        password = str(payload.get("password", ""))
        account = find_account(account_value)
        if not password_matches(password, account):
            time.sleep(0.25)
            self._error(HTTPStatus.UNAUTHORIZED, "账号或密码错误")
            return
        assert account is not None
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with connect_db() as conn:
            conn.execute(
                """INSERT INTO sessions(
                       token_hash, account, account_revision, expires_at, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    token_hash,
                    account["account"],
                    account_revision(account),
                    int(time.time()) + SESSION_TTL_SECONDS,
                    utc_now(),
                ),
            )
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}"
        )
        self._json(
            HTTPStatus.OK,
            {"user": {"account": account["account"], "name": account["name"]}},
            {"Set-Cookie": cookie},
        )

    def _logout(self) -> None:
        token = self._session_token()
        if token:
            token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
            with connect_db() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        self._json(
            HTTPStatus.OK,
            {"ok": True},
            {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
        )

    def _list_jobs(self) -> None:
        with connect_db() as conn:
            last_rows = conn.execute(
                """SELECT b.* FROM builds b
                   JOIN (SELECT job_id, MAX(id) id FROM builds GROUP BY job_id) latest ON latest.id=b.id"""
            ).fetchall()
        last_by_job = {row["job_id"]: build_to_dict(row) for row in last_rows}
        jobs = []
        for job_id, config in JOBS.items():
            item = public_job(config)
            item["last_build"] = last_by_job.get(job_id)
            jobs.append(item)
        self._json(HTTPStatus.OK, {"jobs": jobs})

    def _list_builds(self, job_id: str, query: Dict[str, List[str]]) -> None:
        try:
            limit = max(1, min(int(query.get("limit", ["50"])[0]), 100))
        except ValueError:
            limit = 50
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT * FROM builds WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        self._json(HTTPStatus.OK, {"builds": [build_to_dict(row) for row in rows]})

    def _get_build(self, build_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
        if not row:
            self._error(HTTPStatus.NOT_FOUND, "构建记录不存在")
            return
        self._json(HTTPStatus.OK, {"build": build_to_dict(row)})

    def _get_build_log(self, build_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT id FROM builds WHERE id=?", (build_id,)).fetchone()
        if not row:
            self._error(HTTPStatus.NOT_FOUND, "构建记录不存在")
            return
        log_path = LOG_DIR / f"build-{build_id}.log"
        content = log_path.read_text(encoding="utf-8", errors="replace")[-100_000:] if log_path.exists() else "暂无日志"
        self._json(HTTPStatus.OK, {"log": content})

    def _cancel_build(self, build_id: int, user: Dict[str, Any]) -> None:
        assert BUILD_MANAGER is not None
        try:
            build = BUILD_MANAGER.cancel(build_id, user)
        except PipelineError as exc:
            self._error(exc.status, str(exc))
            return
        self._json(HTTPStatus.ACCEPTED, {"build": build})

    def _create_build(self, job_id: str, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        if job_id not in JOBS:
            self._error(HTTPStatus.NOT_FOUND, "Job 不存在")
            return
        assert PATH_INDEX is not None
        raw_paths = payload.get("resource_paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            self._error(HTTPStatus.BAD_REQUEST, "玩家资源更新路径为空")
            return
        if len(raw_paths) > 20:
            self._error(HTTPStatus.BAD_REQUEST, "最多选择 20 个资源更新路径")
            return
        root_value = str(JOBS[job_id].get("resource_root", "")).strip()
        if not root_value:
            self._error(HTTPStatus.BAD_REQUEST, "资源根路径未配置")
            return
        root = Path(root_value)
        selectable_paths = set(PATH_INDEX.paths(job_id))
        clean_paths: List[str] = []
        for raw_path in raw_paths:
            relative = str(raw_path).strip().replace("\\", "/").strip("/")
            if not relative or relative in clean_paths:
                continue
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, f"资源路径越界：{relative}")
                return
            if not candidate.is_dir():
                self._error(HTTPStatus.BAD_REQUEST, f"资源目录不存在：{relative}")
                return
            if relative not in selectable_paths:
                self._error(HTTPStatus.BAD_REQUEST, f"资源路径必须从搜索结果中选择：{relative}")
                return
            clean_paths.append(relative)
        if not clean_paths:
            self._error(HTTPStatus.BAD_REQUEST, "玩家资源更新路径为空")
            return

        note = str(payload.get("note", "")).strip()[:500]
        parameters = {
            "resource_paths": clean_paths,
            "note": note,
            "jenkins_job_url": JOBS[job_id]["jenkins_job_url"],
        }
        assert BUILD_MANAGER is not None
        build = create_build_record(job_id, user, parameters)
        try:
            build = BUILD_MANAGER.enqueue(build["id"])
        except Exception as exc:
            error_message = f"构建任务入队失败：{type(exc).__name__}: {exc}"
            finish_build(build["id"], "failed", 0, error_message)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": error_message, "build": get_build_record(build["id"])},
            )
            return
        self._json(HTTPStatus.CREATED, {"build": build})

    def _serve_static(self, request_path: str) -> None:
        if request_path == "/":
            request_path = "/index.html"
        is_resource = request_path.startswith("/res/")
        asset_root = RES_DIR if is_resource else STATIC_DIR
        relative = request_path[len("/res/"):] if is_resource else request_path.lstrip("/")
        candidate = (asset_root / relative).resolve()
        try:
            candidate.relative_to(asset_root.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # SPA 路由统一回退到 index.html。
        if not is_resource and not candidate.is_file() and "." not in Path(relative).name:
            candidate = STATIC_DIR / "index.html"
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        mime_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") or mime_type == "application/javascript" else mime_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int) -> None:
    global JOBS, PATH_INDEX, BUILD_MANAGER
    init_db()
    accounts = load_accounts()
    recover_interrupted_builds()
    JOBS = load_config()
    PATH_INDEX = PathIndex(JOBS)
    BUILD_MANAGER = BuildManager()
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"OTA 构建工具已启动：http://{host}:{port}")
    print(f"已加载 Job：{', '.join(JOBS)}")
    print(f"已加载账号：{len(accounts)} 个")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
        BUILD_MANAGER.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="团队资源更新 / OTA 构建工具")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve", help="启动 HTTP 服务")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    user_parser = subparsers.add_parser("add-user", help="添加本地账号")
    user_parser.add_argument("account", help="唯一登录账号")
    user_parser.add_argument("--name", help="显示名称，默认与登录账号相同")
    delete_user_parser = subparsers.add_parser(
        "delete-user", help="删除本地账号（保留历史构建记录）"
    )
    delete_user_parser.add_argument("account", help="唯一登录账号")
    delete_user_parser.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    subparsers.add_parser("validate-accounts", help="检查账号配置文件")
    args = parser.parse_args()
    init_db()
    if args.command == "add-user":
        password = getpass.getpass("密码：")
        confirm = getpass.getpass("再次输入密码：")
        if password != confirm:
            raise SystemExit("两次输入的密码不一致")
        try:
            add_user(args.account, password, args.name)
        except (ValueError, AccountConfigError) as exc:
            raise SystemExit(f"添加账号失败：{exc}")
        print(f"账号 {args.account} 已添加")
    elif args.command == "delete-user":
        if not args.yes:
            answer = input(
                f"确认删除账号 {args.account}？该账号将立即退出登录 [y/N]："
            ).strip().lower()
            if answer not in ("y", "yes"):
                print("已取消删除")
                return
        try:
            delete_user(args.account)
        except (ValueError, AccountConfigError) as exc:
            raise SystemExit(f"删除账号失败：{exc}")
        print(f"账号 {args.account} 已删除，历史构建记录已保留")
    elif args.command == "validate-accounts":
        try:
            accounts = load_accounts()
        except AccountConfigError as exc:
            raise SystemExit(f"账号配置检查失败：{exc}")
        print(f"账号配置有效，共 {len(accounts)} 个账号")
    else:
        serve(getattr(args, "host", "0.0.0.0"), getattr(args, "port", 8765))


if __name__ == "__main__":
    main()
