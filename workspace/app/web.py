"""HTTP 层：仅用标准库 http.server 实现 JSON REST API。

时间注入：请求头 ``X-Now``（ISO 8601）可覆盖当前时间，
仅当环境变量 ALLOW_TIME_OVERRIDE=1 时生效（测试使用）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import registry
from .contracts import ContractError
from .db import connect, init_db
from .errors import ApiError

ALLOW_TIME_OVERRIDE = os.environ.get("ALLOW_TIME_OVERRIDE", "0") == "1"

ROUTES = [
    ("GET", r"^/health$", "health"),
    ("GET", r"^/services$", "list_services"),
    ("POST", r"^/services$", "create_service"),
    ("GET", r"^/services/(?P<service_id>[^/]+)/versions$", "list_versions"),
    ("POST", r"^/services/(?P<service_id>[^/]+)/versions$", "submit_version"),
    ("GET", r"^/services/(?P<service_id>[^/]+)/lineage$", "lineage"),
    ("GET", r"^/services/(?P<service_id>[^/]+)/declarations$",
     "list_declarations"),
    ("POST", r"^/services/(?P<service_id>[^/]+)/declarations$",
     "register_declaration"),
    ("GET", r"^/services/(?P<service_id>[^/]+)/exemptions$",
     "list_exemptions"),
    ("POST", r"^/services/(?P<service_id>[^/]+)/exemptions$",
     "create_exemption"),
    ("GET", r"^/services/(?P<service_id>[^/]+)/publish$", "get_publish"),
    ("POST", r"^/services/(?P<service_id>[^/]+)/publish$", "publish_latest"),
    ("GET", r"^/versions/(?P<version_id>[^/]+)$", "get_version"),
    ("POST", r"^/versions/(?P<version_id>[^/]+)/admit$", "admit"),
    ("POST", r"^/versions/(?P<version_id>[^/]+)/withdraw$", "withdraw"),
    ("POST", r"^/versions/(?P<version_id>[^/]+)/publish$", "publish"),
    ("GET", r"^/versions/(?P<version_id>[^/]+)/reviews$", "reviews"),
    ("POST", r"^/exemptions/(?P<exemption_id>[^/]+)/revoke$",
     "revoke_exemption"),
]


class AppState:
    def __init__(self, db_path: str | None = None):
        self.conn = connect(db_path)
        init_db(self.conn)


class Handler(BaseHTTPRequestHandler):
    state: AppState

    # ---- 框架基础设施 -----------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        if os.environ.get("QUIET") != "1":
            super().log_message(fmt, *args)

    def _now(self):
        now = registry.utcnow()
        if ALLOW_TIME_OVERRIDE:
            override = self.headers.get("X-Now")
            if override:
                now = registry.parse_dt(override, "X-Now")
        return now

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json", f"请求体不是合法 JSON: {exc}")
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
        return body

    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _match(self, method: str, path: str):
        for m, pattern, action in ROUTES:
            if m != method:
                continue
            match = re.match(pattern + r"$", path)
            if match:
                return action, match.groupdict()
        return None, None

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        action, kwargs = self._match(method, parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if not action:
            self._send(404, {"error": {"code": "no_route",
                                       "message": f"未找到路由 {method} {parts.path}"}})
            return
        try:
            body = self._read_json() if method == "POST" else {}
            status, result = getattr(self, f"handle_{action}")(
                body, query, **kwargs)
            self._send(status, result)
        except ContractError as exc:
            self._send(400, {"error": {"code": "invalid_contract",
                                       "message": str(exc)}})
        except ApiError as exc:
            self._send(exc.status, {"error": {"code": exc.code,
                                              "message": exc.message,
                                              "details": exc.details}})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # ---- 处理器 -----------------------------------------------------------

    @property
    def conn(self):
        return self.state.conn

    def handle_health(self, body, query):
        return 200, {"status": "ok", "time": self._now().isoformat()}

    def handle_list_services(self, body, query):
        services = registry.list_services(self.conn)
        return 200, {"services": services}

    def handle_create_service(self, body, query):
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ApiError(400, "invalid_name", "name 必须是非空字符串")
        try:
            svc = registry.get_or_create_service(
                self.conn, name.strip(), self._now())
        except sqlite3.IntegrityError:
            raise conflict("服务名已存在")
        return 201, svc

    def handle_list_versions(self, body, query, service_id):
        registry.get_service(self.conn, service_id)
        return 200, {"versions": registry.list_versions(
            self.conn, service_id, query.get("status"))}

    def handle_submit_version(self, body, query, service_id):
        result = registry.submit_version(
            self.conn, service_id, body, self._now())
        return 201, result

    def handle_lineage(self, body, query, service_id):
        registry.get_service(self.conn, service_id)
        return 200, registry.get_lineage(self.conn, service_id)

    def handle_list_declarations(self, body, query, service_id):
        registry.get_service(self.conn, service_id)
        include = query.get("include_inactive") in ("1", "true", "yes")
        return 200, {"declarations": registry.list_declarations(
            self.conn, service_id, include)}

    def handle_register_declaration(self, body, query, service_id):
        decl = registry.register_declaration(
            self.conn, service_id, body, self._now())
        return 201, decl

    def handle_list_exemptions(self, body, query, service_id):
        include = query.get("include_expired") in ("1", "true", "yes")
        return 200, {"exemptions": registry.list_exemptions(
            self.conn, service_id, self._now(), include)}

    def handle_create_exemption(self, body, query, service_id):
        ex = registry.create_exemption(
            self.conn, service_id, body, self._now())
        return 201, ex

    def handle_get_publish(self, body, query, service_id):
        return 200, registry.get_publish_record(self.conn, service_id)

    def handle_publish_latest(self, body, query, service_id):
        """便捷入口：发布服务下指定版本 id（body.version_id）。"""
        version_id = body.get("version_id")
        if not isinstance(version_id, str) or not version_id:
            raise ApiError(400, "version_id_required",
                           "请在 body 中提供 version_id")
        result = registry.publish_version(
            self.conn, version_id, body, self._now())
        return 200, result

    def handle_get_version(self, body, query, version_id):
        return 200, registry.get_version(self.conn, version_id)

    def handle_admit(self, body, query, version_id):
        return 200, registry.admit_version(
            self.conn, version_id, self._now())

    def handle_withdraw(self, body, query, version_id):
        return 200, registry.withdraw_version(
            self.conn, version_id, body.get("reason"), self._now())

    def handle_publish(self, body, query, version_id):
        return 200, registry.publish_version(
            self.conn, version_id, body, self._now())

    def handle_reviews(self, body, query, version_id):
        registry.get_version(self.conn, version_id)
        return 200, {"reviews": registry.list_reviews(self.conn, version_id)}

    def handle_revoke_exemption(self, body, query, exemption_id):
        return 200, registry.revoke_exemption(
            self.conn, exemption_id, self._now())


def build_server(host: str = "0.0.0.0", port: int = 8080,
                 db_path: str | None = None) -> ThreadingHTTPServer:
    state = AppState(db_path)

    class BoundHandler(Handler):
        pass

    BoundHandler.state = state
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.state = state
    return server


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = build_server(host, port)
    print(f"接口契约登记与演进系统启动: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
