#!/usr/bin/env python3
"""团队资源更新 / OTA 构建面板（仅使用 Python 标准库）。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import secrets
import sqlite3
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse, parse_qs


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ota_tool.db"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = BASE_DIR / "jobs.json"
SESSION_COOKIE = "ota_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 310_000


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
        root = Path(os.path.expandvars(os.path.expanduser(raw["resource_root"])))
        if not root.is_absolute():
            root = (BASE_DIR / root).resolve()
        config = dict(raw)
        config["resource_root"] = str(root.resolve())
        config.setdefault("display_name", job_id.upper())
        config.setdefault("description", "资源更新与 OTA 构建")
        config.setdefault("command", [])
        config.setdefault("simulate_seconds", 4)
        config.setdefault("exclude_dirs", [".git", "node_modules", "Library", "Temp"])
        config.setdefault("max_search_depth", 8)
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
        root = Path(config["resource_root"])
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
    return {
        "id": config["id"],
        "display_name": config["display_name"],
        "description": config["description"],
        "resource_root_name": Path(config["resource_root"]).name,
    }


def build_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    try:
        result["parameters"] = json.loads(result.pop("parameters_json"))
    except (json.JSONDecodeError, TypeError):
        result["parameters"] = {}
        result.pop("parameters_json", None)
    return result


class BuildManager:
    def __init__(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        self.jobs = jobs
        self.pending: "queue.Queue[int]" = queue.Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True, name="build-worker")
        self.thread.start()

    def enqueue(self, build_id: int) -> None:
        self.pending.put(build_id)

    def _worker(self) -> None:
        while True:
            build_id = self.pending.get()
            try:
                self._run(build_id)
            except Exception:
                traceback.print_exc()
                with connect_db() as conn:
                    conn.execute(
                        """UPDATE builds SET status='failed', finished_at=?, error_message=?
                           WHERE id=?""",
                        (utc_now(), "构建服务内部错误", build_id),
                    )
            finally:
                self.pending.task_done()

    def _run(self, build_id: int) -> None:
        started_at = utc_now()
        start_clock = time.monotonic()
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            if not row or row["status"] != "queued":
                return
            conn.execute(
                "UPDATE builds SET status='running', started_at=? WHERE id=?",
                (started_at, build_id),
            )

        config = self.jobs[row["job_id"]]
        parameters = json.loads(row["parameters_json"])
        command = config.get("command") or []
        log_path = LOG_DIR / f"build-{build_id}.log"
        exit_code = 0
        error_message: Optional[str] = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"[{started_at}] 开始构建 {row['job_id']} #{row['build_number']}\n")
                log.write(json.dumps(parameters, ensure_ascii=False, indent=2) + "\n\n")
                log.flush()
                if command:
                    env = os.environ.copy()
                    env.update(
                        {
                            "OTA_JOB_ID": row["job_id"],
                            "OTA_BUILD_NUMBER": str(row["build_number"]),
                            "OTA_RESOURCE_ROOT": config["resource_root"],
                            "OTA_RESOURCE_PATHS": json.dumps(parameters["resource_paths"], ensure_ascii=False),
                        }
                    )
                    process = subprocess.run(
                        [str(part) for part in command],
                        cwd=config.get("working_directory") or config["resource_root"],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    exit_code = process.returncode
                else:
                    seconds = max(0, min(float(config.get("simulate_seconds", 4)), 30))
                    log.write("未配置 command，当前运行演示构建。\n")
                    log.flush()
                    time.sleep(seconds)
                    log.write("演示构建完成。\n")
        except Exception as exc:
            exit_code = 1
            error_message = f"{type(exc).__name__}: {exc}"

        finished_at = utc_now()
        duration = round(time.monotonic() - start_clock, 2)
        status = "success" if exit_code == 0 else "failed"
        if exit_code and not error_message:
            error_message = f"构建命令退出码：{exit_code}"
        with connect_db() as conn:
            conn.execute(
                """UPDATE builds
                   SET status=?, finished_at=?, duration_seconds=?, error_message=?
                   WHERE id=?""",
                (status, finished_at, duration, error_message, build_id),
            )


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
        raw_paths = payload.get("resource_paths")
        if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 20:
            self._error(HTTPStatus.BAD_REQUEST, "玩家资源更新路径为空")
            return
        root = Path(JOBS[job_id]["resource_root"])
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
        }
        created_at = utc_now()
        with connect_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            next_number = conn.execute(
                "SELECT COALESCE(MAX(build_number), 0) + 1 FROM builds WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                """INSERT INTO builds(job_id, build_number, user_id, status, parameters_json, created_at)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (job_id, next_number, user["id"], json.dumps(parameters, ensure_ascii=False), created_at),
            )
            build_id = int(cursor.lastrowid)
        assert BUILD_MANAGER is not None
        BUILD_MANAGER.enqueue(build_id)
        self._json(
            HTTPStatus.CREATED,
            {
                "build": {
                    "id": build_id,
                    "job_id": job_id,
                    "build_number": next_number,
                    "username": user["username"],
                    "status": "queued",
                    "parameters": parameters,
                    "created_at": created_at,
                    "started_at": None,
                    "finished_at": None,
                    "duration_seconds": None,
                    "error_message": None,
                }
            },
        )

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
    global JOBS, PATH_INDEX, BUILD_MANAGER
    init_db()
    recover_interrupted_builds()
    JOBS = load_config()
    PATH_INDEX = PathIndex(JOBS)
    BUILD_MANAGER = BuildManager(JOBS)
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
