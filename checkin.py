#!/usr/bin/env python3
"""WorkBuddy 每日签到 —— 主脚本（纯标准库，零依赖）。

用法:
    python3 checkin.py              # 查状态 -> 未签到则签到
    python3 checkin.py --status     # 只查状态
    python3 checkin.py --json       # 机器可读输出（单行 JSON）

凭证优先级: 环境变量 > state/tokens.json
退出码: 0 成功或今日已签到 / 1 失败 / 2 缺少凭证
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:  # Windows 控制台中文输出兜底
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BJ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")

# 多账号支持：设置 WORKBUDDY_ACCOUNT=1 / 2 后，
# 环境变量、状态文件、GitHub Secrets 名一律带该后缀。
# 例如 WORKBUDDY_ACCESS_TOKEN -> WORKBUDDY_ACCESS_TOKEN_2
ACCOUNT = (os.environ.get("WORKBUDDY_ACCOUNT") or "").strip()
ACCOUNT_LABEL = (os.environ.get("WORKBUDDY_ACCOUNT_LABEL") or "").strip()
SUFFIX = f"_{ACCOUNT}" if ACCOUNT else ""


def set_account(account, label=""):
    """按 --account 切换账号：改全局后缀、状态文件与日志标签。

    本机定时任务通过它指定账号，避免在 crontab / 计划任务里塞中文环境变量
    （中文写入 crontab 存在编码风险）。
    """
    global ACCOUNT, ACCOUNT_LABEL, SUFFIX, TOKENS_FILE
    account = (account or "").strip()
    if not account:
        return
    ACCOUNT = account
    SUFFIX = f"_{ACCOUNT}"
    TOKENS_FILE = os.path.join(STATE_DIR, f"tokens{SUFFIX}.json")
    if (label or "").strip():
        ACCOUNT_LABEL = label.strip()
    elif not ACCOUNT_LABEL:
        ACCOUNT_LABEL = f"账号{ACCOUNT}"

TOKENS_FILE = os.path.join(STATE_DIR, f"tokens{SUFFIX}.json")
# 日志路径可用 WORKBUDDY_LOG_FILE 覆盖；Actions 里由汇总步骤读取同一份文件
LOG_FILE = (os.environ.get("WORKBUDDY_LOG_FILE") or "").strip() \
    or os.path.join(STATE_DIR, "checkin.log")

API_BASE = (os.environ.get("WORKBUDDY_API_BASE") or "https://www.codebuddy.cn").rstrip("/")
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
REFRESH_URL = os.environ.get(
    "WORKBUDDY_REFRESH_URL",
    "https://copilot.tencent.com/v2/plugin/auth/token/refresh",
)

CODE_OK = 0
CODE_ALREADY = 10001
TIMEOUT = 25


# ------------------------------------------------------------------ 工具

def now_bj():
    return datetime.now(BJ)


def env_get(name, default=""):
    """读环境变量：优先带账号后缀的，没有则回落到无后缀（单账号兼容）。"""
    for key in (f"{name}{SUFFIX}", name):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return default


def tag(text):
    """给输出行加账号标签，多账号日志才分得清。"""
    return f"[{ACCOUNT_LABEL}] {text}" if ACCOUNT_LABEL else text


def say(msg):
    print(tag(msg))


def warn(msg):
    print(tag(msg), file=sys.stderr)


def stamp():
    return now_bj().strftime("%Y-%m-%d %H:%M:%S")


def append_log(line):
    """结果追加写入日志（北京时间）。永不写入令牌。"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp()}] {tag(line)}\n")
    except Exception:  # noqa: BLE001 - 日志失败不影响主流程
        pass


def http_json(url, method="GET", headers=None, body=None, timeout=TIMEOUT):
    """返回 (http_status, dict)。网络异常时 http_status=0。"""
    data = None
    hdrs = {"User-Agent": "workbuddy-checkin/1.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception as exc:  # noqa: BLE001 - 超时/DNS/连接重置
        return 0, {"_error": f"{type(exc).__name__}: {exc}"}


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw.decode("utf-8", "replace")[:800]}


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def pick(obj, keywords, kinds=(int, float)):
    """按字段名关键字模糊取值，兼容接口字段改名。"""
    for path, value in walk(obj):
        leaf = path.split(".")[-1].split("[")[0].lower()
        if isinstance(value, bool) or not isinstance(value, kinds):
            continue
        if any(kw in leaf for kw in keywords):
            return value
    return None


def pick_bool(obj, keywords):
    for path, value in walk(obj):
        leaf = path.split(".")[-1].split("[")[0].lower()
        if isinstance(value, bool) and any(kw in leaf for kw in keywords):
            return value
    return None


def extract(payload, fallback=None):
    """从响应里抽出 积分 / 连签天数 / 累计积分。"""
    fallback = fallback or {}
    info = {
        "points": pick(payload, ("point", "score", "credit", "coin", "integral")),
        "streak": pick(payload, ("streak", "continuous", "serial", "consecutive",
                                 "daycount", "days")),
        "total": pick(payload, ("totalpoint", "totalintegral", "accumulate",
                                "totalcredit")),
    }
    for key in ("points", "streak", "total"):
        if info[key] is None:
            info[key] = fallback.get(key)
    return info


# ------------------------------------------------------------------ 凭证

def load_tokens():
    env_token = env_get("WORKBUDDY_ACCESS_TOKEN")
    env_uid = env_get("WORKBUDDY_UID")
    env_refresh = env_get("WORKBUDDY_REFRESH_TOKEN")

    disk = {}
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as fh:
                disk = json.load(fh) or {}
        except Exception:  # noqa: BLE001
            disk = {}

    tokens = {
        "accessToken": env_token or (disk.get("accessToken") or "").strip(),
        "uid": env_uid or (disk.get("uid") or "").strip(),
        "refreshToken": env_refresh or (disk.get("refreshToken") or "").strip(),
        "machineId": (disk.get("machineId") or "").strip(),
        "sessionId": (disk.get("sessionId") or "").strip(),
        "source": "env" if env_token else ("file" if disk else "none"),
    }
    return tokens


def save_tokens(tokens):
    """回写 state/tokens.json，权限收紧到 600。永不落盘到仓库目录之外。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        payload = {k: tokens[k] for k in
                   ("accessToken", "uid", "refreshToken", "machineId", "sessionId")
                   if tokens.get(k)}
        with open(TOKENS_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except Exception:  # noqa: BLE001 - Windows 上部分文件系统不支持
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        warn(f"[warn] 回写令牌失败: {exc}")
        return False


def auth_headers(tokens):
    hdrs = {
        "Authorization": f"Bearer {tokens['accessToken']}",
        "X-User-Id": tokens["uid"],
    }
    if tokens.get("machineId"):
        hdrs["X-Machine-ID"] = tokens["machineId"]
    if tokens.get("sessionId"):
        hdrs["X-Session-ID"] = tokens["sessionId"]
    return hdrs


# ------------------------------------------------------------------ 业务

def call_api(path, tokens):
    return http_json(API_BASE + path, "POST", auth_headers(tokens), {})


def is_auth_error(status, payload):
    if status in (401, 403):
        return True
    if isinstance(payload, dict) and payload.get("code") in (401, 403, 10401):
        return True
    text = json.dumps(payload, ensure_ascii=False).lower() if payload else ""
    return any(k in text for k in ("unauthorized", "token is expired",
                                   "token expired", "invalid token",
                                   "登录已过期", "未登录", "token invalid"))


def is_already(payload):
    if isinstance(payload, dict) and payload.get("code") == CODE_ALREADY:
        return True
    text = json.dumps(payload, ensure_ascii=False).lower() if payload else ""
    # 先排除否定表述：文案里出现「未签到/尚未签到」时不能算已签到，
    # 否则 "今日未签到，昨日已签到" 这类句子会被误判成今天已签，直接漏签一天。
    if any(k in text for k in ("未签到", "尚未签到", "还没有签到", "not checked",
                               "not yet check", "havent check", "haven't check")):
        return False
    return any(k in text for k in ("已签到", "已经签到", "already check"))


def refresh_tokens(tokens):
    """用 refreshToken 换新令牌。成功返回更新后的 tokens，失败返回 None。"""
    if not tokens.get("refreshToken"):
        warn("[auth] 没有 refreshToken，无法续期")
        return None
    status, data = http_json(REFRESH_URL, "POST",
                             {"X-Refresh-Token": tokens["refreshToken"]}, {})
    if status == 0:
        warn(f"[auth] 续期请求网络失败: {data.get('_error')}")
        return None
    if status != 200:
        warn(f"[auth] 续期被拒绝 http={status}")
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if code not in (None, 0):
        msg = data.get("msg") or data.get("message") or ""
        warn(f"[auth] 续期失败 code={code} {msg}")
        return None

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    new_access = next((body.get(k) for k in ("accessToken", "access_token", "token")
                       if isinstance(body.get(k), str) and body.get(k)), None)
    if not new_access:
        warn("[auth] 续期响应缺少 accessToken 字段")
        return None

    updated = dict(tokens)
    updated["accessToken"] = new_access
    rotated = False
    for key in ("refreshToken", "refresh_token"):
        val = body.get(key)
        if isinstance(val, str) and val and val != tokens.get("refreshToken"):
            updated["refreshToken"] = val
            rotated = True
            say("[auth] refreshToken 已轮换，同步更新")
            break
    save_tokens(updated)
    if not persist_to_github(updated) and rotated:
        annotate("warning", "[auth] 续期成功但新 refreshToken 未写回 Secrets；"
                            "旧值可能已作废，请尽快更新")
    return updated


def annotate(level, msg):
    """输出 GitHub Actions 注解，让问题出现在运行摘要里，而不是埋在日志第 80 行。"""
    print(f"::{level}::{tag(msg)}")


def persist_to_github(tokens):
    """把续期后的令牌写回 Actions Secrets，避免 GitHub 上凭证过期。

    仅在 GH_SECRETS_TOKEN + WB_GITHUB_REPO 都存在时启用。
    返回 True 表示全部写回成功（或本来就不需要写回），False 表示写回失败。

    为什么必须让失败可见：Keycloak 续期通常会轮换 refreshToken，
    一旦新令牌没写回，GitHub 上留的就是已作废的旧值，下次运行必然 401。
    所以这里失败要重试一次并打 ::warning::，不能静默吞掉。
    """
    repo = os.environ.get("WB_GITHUB_REPO", "").strip()
    gh = os.environ.get("GH_SECRETS_TOKEN", "").strip()
    if not repo or not gh:
        annotate("warning", "[persist] 未配置 GH_SECRETS_TOKEN / WB_GITHUB_REPO，"
                            "新令牌只在本次运行内有效，不会写回 Secrets")
        return False

    headers = {"Authorization": f"Bearer {gh}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    status, keyinfo = http_json(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        "GET", headers)
    if status != 200 or "key" not in keyinfo:
        annotate("warning", f"[persist] 获取仓库公钥失败 http={status}，"
                            "续期结果未能写回，GitHub 上的令牌可能已作废")
        return False
    try:
        import libsodium_seal
    except Exception:  # noqa: BLE001
        annotate("warning", "[persist] 缺少 libsodium_seal，跳过写回")
        return False

    pairs = [(f"WORKBUDDY_ACCESS_TOKEN{SUFFIX}", tokens["accessToken"])]
    if tokens.get("refreshToken"):
        pairs.append((f"WORKBUDDY_REFRESH_TOKEN{SUFFIX}", tokens["refreshToken"]))

    ok = True
    for name, value in pairs:
        try:
            enc = libsodium_seal.seal_b64(value.encode(), keyinfo["key"])
        except Exception as exc:  # noqa: BLE001
            warn(f"[persist] 加密失败: {exc}")
            ok = False
            continue
        put = dict(headers)
        put["Content-Type"] = "application/json"
        code = 0
        for attempt in (1, 2):  # 网络抖动重试一次
            code, _ = http_json(
                f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
                "PUT", put, {"encrypted_value": enc, "key_id": keyinfo["key_id"]})
            if code in (201, 204):
                break
            time.sleep(2 * attempt)
        if code in (201, 204):
            say(f"[persist] {name} 已更新")
        else:
            ok = False
            warn(f"[persist] {name} 写回失败 http={code}")
            annotate("warning", f"[persist] {name} 写回失败 http={code}；"
                                "请尽快更新该 Secret，否则下次续期会失败")
    return ok


# ------------------------------------------------------------------ 主流程

def finish(result, as_json, exit_code):
    line = json.dumps(result, ensure_ascii=False, default=str)
    if as_json:
        print(line)
    append_log(result.get("message", "") + (f" | {line}" if not as_json else ""))
    return exit_code


def main(argv=None):
    ap = argparse.ArgumentParser(description="WorkBuddy 每日签到")
    ap.add_argument("--status", action="store_true", help="只查询签到状态")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="输出机器可读 JSON")
    ap.add_argument("--account", default="",
                    help="账号编号（读 WORKBUDDY_*_<编号> 环境变量与 tokens_<编号>.json）")
    args = ap.parse_args(argv)

    if args.account:
        set_account(args.account)

    tokens = load_tokens()
    if not tokens.get("accessToken") or not tokens.get("uid"):
        msg = ("缺少凭证：请设置 WORKBUDDY_ACCESS_TOKEN / WORKBUDDY_UID "
               "环境变量，或先运行 extract_token.py 生成 state/tokens.json")
        if args.as_json:
            print(json.dumps({"ok": False, "reason": "missing_credentials",
                              "message": msg}, ensure_ascii=False))
        else:
            warn(f"[error] {msg}")
        return 2

    # 1) 查状态
    status, payload = call_api(STATUS_PATH, tokens)
    code = payload.get("code") if isinstance(payload, dict) else None

    # 2) 鉴权失败 -> 续期一次
    if is_auth_error(status, payload):
        say("[auth] accessToken 失效，尝试用 refreshToken 续期")
        renewed = refresh_tokens(tokens)
        if not renewed:
            msg = "凭证续期失败：需要重新登录 WorkBuddy 客户端，并更新凭证"
            if args.as_json:
                print(json.dumps({"ok": False, "reason": "auth_failed",
                                  "message": msg}, ensure_ascii=False))
            else:
                warn(f"[error] {msg}")
            append_log(f"FAIL auth_failed http={status}")
            print("::error::" + tag(msg))
            return 1
        tokens = renewed
        status, payload = call_api(STATUS_PATH, tokens)
        code = payload.get("code") if isinstance(payload, dict) else None

    if status == 0:
        msg = f"网络异常，无法访问 {API_BASE}: {payload.get('_error')}"
        if args.as_json:
            print(json.dumps({"ok": False, "reason": "network", "message": msg},
                             ensure_ascii=False))
        else:
            warn(f"[error] {msg}")
        append_log(f"FAIL network {payload.get('_error')}")
        return 1

    if is_already(payload) or pick_bool(payload, ("todaycheck", "checked",
                                                  "checkin", "signed")) is True:
        info = extract(payload)
        msg = ("今日已签到，无需重复领取" if not info["points"]
               else f"今日已签到，无需重复领取（积分 {info['points']}）")
        if args.as_json:
            print(json.dumps({"ok": True, "status": "already", "code": code,
                              **info, "message": msg}, ensure_ascii=False))
        else:
            say(f"[ok] {msg}")
            if info["streak"] is not None:
                say(f"     连续签到 {info['streak']} 天")
            if info["total"] is not None:
                say(f"     累计积分 {info['total']}")
        append_log(f"ALREADY {msg}")
        return 0

    if args.status:
        info = extract(payload)
        msg = "查询成功，今日尚未签到"
        if args.as_json:
            print(json.dumps({"ok": True, "status": "not_checked_in", "code": code,
                              **info, "message": msg}, ensure_ascii=False))
        else:
            say(f"[status] {msg}")
            say("[status] " + json.dumps(payload, ensure_ascii=False)[:600])
        return 0

    # 3) 签到
    status, res = call_api(CHECKIN_PATH, tokens)
    rcode = res.get("code") if isinstance(res, dict) else None

    if rcode == CODE_OK:
        info = extract(res, fallback=extract(payload))
        parts = ["签到成功"]
        if info["points"] is not None:
            parts.append(f"获得 {info['points']} 积分")
        if info["streak"] is not None:
            parts.append(f"连续签到第 {info['streak']} 天")
        msg = "，".join(parts)
        if args.as_json:
            print(json.dumps({"ok": True, "status": "checked_in", "code": rcode,
                              **info, "message": msg}, ensure_ascii=False))
        else:
            say(f"[ok] {msg}")
            if info["total"] is not None:
                say(f"     累计积分 {info['total']}")
        append_log(f"OK {msg}")
        return 0

    if is_already(res):
        info = extract(res, fallback=extract(payload))
        msg = "今日已签到，无需重复领取"
        if args.as_json:
            print(json.dumps({"ok": True, "status": "already", "code": rcode,
                              **info, "message": msg}, ensure_ascii=False))
        else:
            say(f"[ok] {msg}")
        append_log(f"ALREADY {msg}")
        return 0

    # 签到阶段鉴权失败，再续期重试一次（仅一次）
    if is_auth_error(status, res):
        renewed = refresh_tokens(tokens)
        if renewed:
            tokens = renewed
            status, res = call_api(CHECKIN_PATH, tokens)
            rcode = res.get("code") if isinstance(res, dict) else None
            if rcode == CODE_OK:
                info = extract(res)
                msg = (f"签到成功（续期后），获得 {info['points']} 积分"
                       if info["points"] is not None else "签到成功（续期后）")
                if args.as_json:
                    print(json.dumps({"ok": True, "status": "checked_in",
                                      "code": rcode, **info, "message": msg},
                                     ensure_ascii=False))
                else:
                    say(f"[ok] {msg}")
                append_log(f"OK {msg}")
                return 0

    detail = json.dumps(res, ensure_ascii=False)[:400]
    if args.as_json:
        print(json.dumps({"ok": False, "status": "failed", "code": rcode,
                          "message": "签到失败", "detail": detail},
                         ensure_ascii=False))
    else:
        warn(f"[error] 签到失败 code={rcode} http={status}")
        warn(f"[error] 响应: {detail}")
    append_log(f"FAIL code={rcode} {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
