import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cases import CaseStore
from .catalog import Catalog
from .domain import overview
from .patterns import build_report, resolve_parameters
from .profile import numeric_profiles


class Application:
    def __init__(self, data, state):
        self.catalog = Catalog(data)
        self.cases = CaseStore(state)
        self.pattern_reports = CaseStore(Path(state) / "pattern-reports")

    def dispatch(self, method, target, body=None):
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return 200, {"status": "ok", "source_fingerprint": self.catalog.fingerprint}
            if method == "GET" and path == "/tables":
                return 200, {"tables": self.catalog.describe()}
            if method == "GET" and path.startswith("/tables/"):
                name = path.split("/")[-1]
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                if offset < 0 or not 1 <= limit <= 500:
                    raise ValueError("分页范围无效")
                records = self.catalog.rows(name)
                return 200, {"total": len(records), "offset": offset, "rows": records[offset:offset + limit]}
            if method == "GET" and path == "/view":
                return 200, {"source_fingerprint": self.catalog.fingerprint, "view": overview(self.catalog)}
            if method == "GET" and path == "/profiles":
                return 200, {"source_fingerprint": self.catalog.fingerprint, "profiles": numeric_profiles(self.catalog)}
            if method == "GET" and path == "/pattern-report":
                parameters = {key: values[0] for key, values in query.items()}
                include_matches = parameters.pop("matches", "1")
                if include_matches not in ("0", "1"):
                    raise ValueError("matches 仅支持零或一")
                report = build_report(self.catalog, parameters)
                if include_matches == "0":
                    for entry in report["patterns"]:
                        entry["matches"] = {"discovery": [], "validation": []}
                return 200, report
            if method == "GET" and path == "/pattern-reports":
                return 200, {"pattern_reports": self.pattern_reports.list()}
            if method == "GET" and path.startswith("/pattern-reports/"):
                return 200, self.pattern_reports.read(path.split("/")[-1])
            if method == "POST" and path == "/pattern-reports":
                if not isinstance(body, dict):
                    raise ValueError("请求内容应为对象")
                if set(body) - {"label", "params", "notes"}:
                    raise ValueError("请求包含未知字段")
                parameters = resolve_parameters(body.get("params"))
                report = build_report(self.catalog, parameters)
                record = self.pattern_reports.create(
                    self.catalog, body.get("label"),
                    options={"parameters": parameters}, notes=body.get("notes", ""),
                    extra={"report": report})
                return 201, record
            if method == "GET" and path == "/cases":
                return 200, {"cases": self.cases.list()}
            if method == "GET" and path.startswith("/cases/"):
                return 200, self.cases.read(path.split("/")[-1])
            if method == "POST" and path == "/cases":
                if not isinstance(body, dict):
                    raise ValueError("请求内容应为对象")
                if set(body) - {"label", "options", "notes"}:
                    raise ValueError("请求包含未知字段")
                return 201, self.cases.create(self.catalog, body.get("label"), body.get("options"), body.get("notes", ""))
        except KeyError:
            return 404, {"error": "记录不存在"}
        except (TypeError, ValueError) as error:
            return 400, {"error": str(error)}
        return 404, {"error": "接口不存在"}


def make_server(application, host="127.0.0.1", port=8080):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond("GET")

        def do_POST(self):
            self.respond("POST")

        def respond(self, method):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 262144:
                    raise ValueError("请求体过大")
                body = json.loads(self.rfile.read(length)) if length else None
                status, content = application.dispatch(method, self.path, body)
            except (ValueError, UnicodeDecodeError):
                status, content = 400, {"error": "请求格式无效"}
            encoded = json.dumps(content, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
