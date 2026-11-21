#!/usr/bin/env python3
"""本地 mock 服务：模拟 WorkBuddy 签到接口，用于离线验证 checkin.py。

单独运行（手动调试）：
    python3 tests/mock_server.py --port 8765

接口路径与真实环境一致，通过 URL 前缀区分场景：
    /a    未签到 -> 签到成功
    /b    今日已签到（code=10001）
    /c    接口业务错误
    /d    首次 401 -> 续期成功 -> 签到成功
"""

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"

STATE = {}  # scenario -> {"checked": bool, "calls": [...]}


def reset():
    STATE.clear()
    for name in ("a", "b", "c", "d", "e"):
        STATE[name] = {"checked": name == "b", "calls": [], "refreshed": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    # -------------------------------------------------- helpers
    def _send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _split(self):
        path = self.path.split("?")[0]
        parts = [p for p in path.split("/") if p]
        scenario = parts[0] if parts else ""
        rest = "/" + "/".join(parts[1:])
        return scenario, rest

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    # -------------------------------------------------- POST
    def do_POST(self):
        scenario, path = self._split()
        st = STATE.get(scenario)
        if st is None:
            return self._send(404, {"code": 404, "msg": "unknown scenario"})

        auth = self.headers.get("Authorization", "")
        uid = self.headers.get("X-User-Id", "")
        st["calls"].append({"path": path, "auth": bool(auth), "uid": uid})

        if path == REFRESH_PATH:
            if not self.headers.get("X-Refresh-Token"):
                return self._send(400, {"code": 400, "msg": "missing refresh token"})
            st["refreshed"] = True
            return self._send(200, {
                "code": 0, "msg": "ok",
                "data": {"accessToken": "new-access-token-after-refresh",
                         "refreshToken": "new-refresh-token"}})

        # /d 场景：未续期前一律 401
        if scenario == "d" and not st["refreshed"]:
            return self._send(401, {"code": 401, "msg": "token expired"})

        if not auth or not uid:
            return self._send(401, {"code": 401, "msg": "unauthorized"})

        if path == STATUS_PATH:
            # /e 场景：文案里同时出现「未签到」和「已签到」，用来验证否定词优先。
            msg = "今日未签到，上次已签到" if scenario == "e" else "ok"
            return self._send(200, {
                "code": 0, "msg": msg,
                "data": {
                    "todayCheckedIn": st["checked"],
                    "continuousDays": 3 if not st["checked"] else 4,
                    "points": 0 if not st["checked"] else 100,
                    "totalPoints": 1240,
                }})

        if path == CHECKIN_PATH:
            if scenario == "c":
                return self._send(200, {"code": 50000, "msg": "sign failed, try later"})
            if st["checked"]:
                return self._send(200, {"code": 10001, "msg": "already checked in"})
            st["checked"] = True
            return self._send(200, {
                "code": 0, "msg": "ok",
                "data": {"points": 100, "continuousDays": 4, "totalPoints": 1340}})

        return self._send(404, {"code": 404, "msg": "not found"})

    def do_GET(self):
        self._send(405, {"code": 405})

    def do_PUT(self):
        self._send(405, {"code": 405})


class MockServer:
    """上下文管理器：with MockServer() as srv: srv.base_url('a')"""

    def __init__(self, port=0):
        reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    @property
    def port(self):
        return self.httpd.server_address[1]

    def base_url(self, scenario):
        return f"http://127.0.0.1:{self.port}/{scenario}"

    def calls(self, scenario):
        return STATE[scenario]["calls"]

    def state(self, scenario):
        return STATE[scenario]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = MockServer(args.port)
    print(f"mock 服务已启动: http://127.0.0.1:{args.port}/a  (Ctrl+C 停止)")
    try:
        srv.__enter__()
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.__exit__()
        sys.exit(0)
