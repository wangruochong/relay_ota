#!/usr/bin/env python3
"""团队资源更新 / OTA 构建面板（仅使用 Python 标准库）。"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import http.cookiejar
import json
import mimetypes
import os
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple
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
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ota_tool.db"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = BASE_DIR / "jobs.json"
SESSION_COOKIE = "ota_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 310_000
GIT_TIMEOUT_SECONDS = 300
COMPILE_TIMEOUT_SECONDS = 60 * 60
JENKINS_TIMEOUT_SECONDS = 30


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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                build_number INTEGER NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL CHECK(status IN ('queued','running','success','failed')),
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
            """
        )


def recover_interrupted_builds() -> None:
    """启动服务时结束上次进程遗留的任务，账号管理命令不会触碰构建状态。"""
    with connect_db() as conn:
        conn.execute(
            """UPDATE builds
               SET status='failed', finished_at=?, error_message='服务器重启，构建已中断'
               WHERE status IN ('queued', 'running')""",
            (utc_now(),),
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def validate_username(username: str) -> str:
    username = username.strip()
    if not 2 <= len(username) <= 32:
        raise ValueError("用户名长度需为 2～32 个字符")
    if not all(ch.isalnum() or ch in "._-" for ch in username):
        raise ValueError("用户名只能包含字母、数字、点、下划线和短横线")
    return username


def add_user(username: str, password: str) -> None:
    username = validate_username(username)
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), utc_now()),
        )


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
        config["resource_root"] = (
            str((resource_base_root / resource_subpath).resolve())
            if resource_base_root
            else ""
        )
        config.setdefault("display_name", job_id.upper())
        config.setdefault("description", "资源更新与 OTA 构建")
        config.setdefault("exclude_dirs", [".git", "node_modules", "Library", "Temp"])
        config.setdefault("max_search_depth", 8)
        if not str(config.get("branch", "")).strip():
            raise RuntimeError(f"Job {job_id} 未配置主分支")
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


def run_command(
    command: List[str],
    cwd: Path,
    log: TextIO,
    step: str,
    timeout: int,
    env: Optional[Dict[str, str]] = None,
) -> None:
    display = shlex.join(command)
    log.write(f"\n[{step}] $ {display}\n")
    log.flush()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PipelineError(f"{step}失败：找不到命令 {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"{step}超时（{timeout} 秒）") from exc
    except OSError as exc:
        raise PipelineError(f"{step}失败：{exc}") from exc
    if result.returncode != 0:
        detail = _log_tail(log)
        suffix = f"：{detail}" if detail else ""
        raise PipelineError(f"{step}失败（退出码 {result.returncode}）{suffix}")


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


def execute_build_pipeline(
    config: Dict[str, Any], resource_paths: List[str], log: TextIO
) -> str:
    project_root = Path(config["project_root"])
    branch = str(config["branch"])
    compile_file = project_root / "compile.coffee"
    git_dir = project_root / ".git"
    if not git_dir.exists():
        raise PipelineError(f"项目根路径不是 Git 仓库：{project_root}")
    if not compile_file.is_file():
        raise PipelineError(f"找不到资源编译脚本：{compile_file}")

    command_env = os.environ.copy()
    command_env["GIT_TERMINAL_PROMPT"] = "0"
    run_command(["git", "reset", "--hard"], project_root, log, "清理本地修改", GIT_TIMEOUT_SECONDS, command_env)
    run_command(["git", "clean", "-fd"], project_root, log, "清理未跟踪文件", GIT_TIMEOUT_SECONDS, command_env)
    run_command(["git", "fetch", "origin", branch], project_root, log, "拉取远端分支", GIT_TIMEOUT_SECONDS, command_env)
    run_command(["git", "checkout", "-B", branch, f"origin/{branch}"], project_root, log, "切换主分支", GIT_TIMEOUT_SECONDS, command_env)
    run_command(["git", "pull", "--ff-only", "origin", branch], project_root, log, "更新主分支", GIT_TIMEOUT_SECONDS, command_env)

    compile_command = ["coffee", "compile.coffee", "res"]
    for resource_path in resource_paths:
        compile_command.extend(["-d", resource_path])
    run_command(compile_command, project_root, log, "资源编译", COMPILE_TIMEOUT_SECONDS, command_env)

    run_command(["git", "add", "-A"], project_root, log, "暂存资源修改", GIT_TIMEOUT_SECONDS, command_env)
    run_command(["git", "commit", "-m", "res"], project_root, log, "提交资源修改", GIT_TIMEOUT_SECONDS, command_env)
    run_command(["git", "push", "origin", f"HEAD:{branch}"], project_root, log, "推送资源修改", GIT_TIMEOUT_SECONDS, command_env)
    return trigger_jenkins(config, log)


def save_build_record(
    job_id: str,
    user: Dict[str, Any],
    parameters: Dict[str, Any],
    status: str,
    created_at: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    error_message: Optional[str],
    temporary_log_path: Path,
) -> Dict[str, Any]:
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        build_number = conn.execute(
            "SELECT COALESCE(MAX(build_number), 0) + 1 FROM builds WHERE job_id=?",
            (job_id,),
        ).fetchone()[0]
        cursor = conn.execute(
            """INSERT INTO builds(
                   job_id, build_number, user_id, status, parameters_json, created_at,
                   started_at, finished_at, duration_seconds, error_message
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                build_number,
                user["id"],
                status,
                json.dumps(parameters, ensure_ascii=False),
                created_at,
                started_at,
                finished_at,
                duration_seconds,
                error_message,
            ),
        )
        build_id = int(cursor.lastrowid)
    temporary_log_path.replace(LOG_DIR / f"build-{build_id}.log")
    return {
        "id": build_id,
        "job_id": job_id,
        "build_number": build_number,
        "username": user["username"],
        "status": status,
        "parameters": parameters,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "error_message": error_message,
    }


JOBS: Dict[str, Dict[str, Any]] = {}
PATH_INDEX: Optional[PathIndex] = None
BUILD_LOCK = threading.Lock()


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
                """SELECT users.id, users.username
                   FROM sessions JOIN users ON users.id=sessions.user_id
                   WHERE sessions.token_hash=? AND sessions.expires_at>=? AND users.disabled=0""",
                (token_hash, now),
            ).fetchone()
        return dict(row) if row else None

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
        if path.startswith("/api/"):
            self._handle_api_get(path, parse_qs(parsed.query))
        else:
            self._serve_static(path)

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

        if path == "/api/login":
            self._login(payload)
        elif path == "/api/logout":
            self._logout()
        elif path.startswith("/api/jobs/") and path.endswith("/builds"):
            user = self._require_user()
            if user:
                self._create_build(path.split("/")[3], payload, user)
        else:
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")

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
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        with connect_db() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash FROM users WHERE username=? AND disabled=0",
                (username,),
            ).fetchone()
        # 即便用户不存在也做一次等成本哈希，降低用户名探测风险。
        valid = verify_password(password, row["password_hash"]) if row else verify_password(
            password, hash_password("invalid-password")
        )
        if not row or not valid:
            time.sleep(0.25)
            self._error(HTTPStatus.UNAUTHORIZED, "用户名或密码错误")
            return
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token_hash, row["id"], int(time.time()) + SESSION_TTL_SECONDS, utc_now()),
            )
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}"
        )
        self._json(HTTPStatus.OK, {"user": {"id": row["id"], "username": row["username"]}}, {"Set-Cookie": cookie})

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
                """SELECT builds.*, users.username
                   FROM builds JOIN users ON users.id=builds.user_id
                   WHERE job_id=? ORDER BY builds.id DESC LIMIT ?""",
                (job_id, limit),
            ).fetchall()
        self._json(HTTPStatus.OK, {"builds": [build_to_dict(row) for row in rows]})

    def _get_build(self, build_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute(
                """SELECT builds.*, users.username
                   FROM builds JOIN users ON users.id=builds.user_id WHERE builds.id=?""",
                (build_id,),
            ).fetchone()
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
        response_status = HTTPStatus.CREATED
        error_message: Optional[str] = None
        with BUILD_LOCK:
            created_at = utc_now()
            started_at = created_at
            start_clock = time.monotonic()
            temporary_log_path = LOG_DIR / f".pipeline-{secrets.token_hex(12)}.log"
            try:
                with temporary_log_path.open("w", encoding="utf-8") as log:
                    log.write(f"[{started_at}] 开始执行 {job_id} 资源更新流水线\n")
                    log.write(json.dumps(parameters, ensure_ascii=False, indent=2) + "\n")
                    queue_url = execute_build_pipeline(JOBS[job_id], clean_paths, log)
                    if queue_url:
                        parameters["jenkins_queue_url"] = queue_url
            except PipelineError as exc:
                response_status = exc.status
                error_message = str(exc)
            except Exception as exc:
                response_status = HTTPStatus.INTERNAL_SERVER_ERROR
                error_message = f"构建服务内部错误：{type(exc).__name__}: {exc}"

            finished_at = utc_now()
            duration_seconds = round(time.monotonic() - start_clock, 2)
            status = "failed" if error_message else "success"
            with temporary_log_path.open("a", encoding="utf-8") as log:
                if error_message:
                    log.write(f"\n[失败] {error_message}\n")
                else:
                    log.write("\n[完成] 资源已提交并成功触发 Jenkins OTA\n")
            build = save_build_record(
                job_id,
                user,
                parameters,
                status,
                created_at,
                started_at,
                finished_at,
                duration_seconds,
                error_message,
                temporary_log_path,
            )

        if error_message:
            self._json(response_status, {"error": error_message, "build": build})
        else:
            self._json(HTTPStatus.CREATED, {"build": build})

    def _serve_static(self, request_path: str) -> None:
        if request_path == "/":
            request_path = "/index.html"
        relative = request_path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # SPA 路由统一回退到 index.html。
        if not candidate.is_file() and "." not in Path(relative).name:
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
    global JOBS, PATH_INDEX
    init_db()
    recover_interrupted_builds()
    JOBS = load_config()
    PATH_INDEX = PathIndex(JOBS)
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"OTA 构建工具已启动：http://{host}:{port}")
    print(f"已加载 Job：{', '.join(JOBS)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="团队资源更新 / OTA 构建工具")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve", help="启动 HTTP 服务")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    user_parser = subparsers.add_parser("add-user", help="添加本地用户")
    user_parser.add_argument("username")
    args = parser.parse_args()
    init_db()
    if args.command == "add-user":
        password = getpass.getpass("密码：")
        confirm = getpass.getpass("再次输入密码：")
        if password != confirm:
            raise SystemExit("两次输入的密码不一致")
        try:
            add_user(args.username, password)
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise SystemExit(f"添加用户失败：{exc}")
        print(f"用户 {args.username} 已添加")
    else:
        serve(getattr(args, "host", "0.0.0.0"), getattr(args, "port", 8765))


if __name__ == "__main__":
    main()
