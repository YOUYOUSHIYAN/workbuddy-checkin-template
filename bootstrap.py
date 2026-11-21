#!/usr/bin/env python3
"""一键把 WorkBuddy 自动签到部署到 GitHub Actions。

凭据来源（全部通过环境变量传入，脚本不写入任何代码文件）:
    GITHUB_TOKEN                 GitHub classic PAT，需勾选 repo + workflow
    WORKBUDDY_ACCESS_TOKEN(_1)   WorkBuddy 登录态 accessToken（账号 1）
    WORKBUDDY_UID(_1)            WorkBuddy uid（账号 1）
    WORKBUDDY_REFRESH_TOKEN(_1)  WorkBuddy 登录态 refreshToken（账号 1）
    WORKBUDDY_*_2                账号 2 的同名变量（可选，配了才会部署）

用法:
    # 单账号
    GITHUB_TOKEN=ghp_xxx WORKBUDDY_ACCESS_TOKEN=xxx WORKBUDDY_UID=xxx \
      WORKBUDDY_REFRESH_TOKEN=xxx python3 bootstrap.py

    # 双账号：再加一组带 _2 后缀的同名变量即可，无需改 workflow
    WORKBUDDY_ACCESS_TOKEN_2=xxx WORKBUDDY_UID_2=xxx \
      WORKBUDDY_REFRESH_TOKEN_2=xxx python3 bootstrap.py

    python3 bootstrap.py --self-test     # 只做本地自检，不联网

依赖优先级: requests + pynacl（若已安装）> 标准库 + 内置 libsodium_seal。
"""

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_NAME = "workbuddy-checkin"
GH_API = "https://api.github.com"

SKIP_DIRS = {".git", "state", "__pycache__", ".pytest_cache", ".idea", ".vscode"}
SKIP_FILES = {"tokens.json", "checkin.log", ".env"}

_REDACT = []  # 需要从输出中抹掉的敏感串


def mask(value):
    if not value:
        return "(空)"
    if len(value) <= 10:
        return value[:2] + "*" * max(len(value) - 2, 1)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def redact(text):
    out = str(text)
    for secret in _REDACT:
        if secret and len(secret) > 6:
            out = out.replace(secret, mask(secret))
    return out


def say(msg=""):
    print(redact(msg), flush=True)


def warn(msg):
    print(redact("[warn] " + str(msg)), file=sys.stderr, flush=True)


def fail(msg, code=1):
    print(redact("[error] " + str(msg)), file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------- HTTP 层

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    _HAS_REQUESTS = False

try:
    from nacl.public import PublicKey, SealedBox  # type: ignore
    _HAS_NACL = True
except Exception:  # noqa: BLE001
    _HAS_NACL = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(url, method="GET", headers=None, body=None, raw=False, follow=False,
         timeout=30):
    """返回 (status, payload)。payload 为 dict 或 bytes（raw=True）。"""
    hdrs = {"User-Agent": "workbuddy-checkin-bootstrap/1.0",
            "Accept": "application/vnd.github+json"}
    if headers:
        hdrs.update(headers)

    if _HAS_REQUESTS:
        try:
            resp = requests.request(method, url, headers=hdrs, json=body,
                                    timeout=timeout, allow_redirects=follow)
            if raw:
                return resp.status_code, resp.content
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, {"_raw": resp.text[:600]}
        except Exception as exc:  # noqa: BLE001
            return 0, {"_error": f"{type(exc).__name__}: {exc}"}

    data = json.dumps(body).encode() if body is not None else None
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, (resp.read() if raw else _json(resp.read()))
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            loc = exc.headers.get("Location", "")
            return exc.code, {"_location": loc}
        return exc.code, (exc.read() if raw else _json(exc.read()))
    except Exception as exc:  # noqa: BLE001
        return 0, {"_error": f"{type(exc).__name__}: {exc}"}


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw.decode("utf-8", "replace")[:600]}


class GitHub:
    def __init__(self, token):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def call(self, path, method="GET", body=None, raw=False, url=None):
        target = url or (GH_API + path)
        return http(target, method, self.headers, body, raw=raw)

    def get_user(self):
        status, data = self.call("/user")
        if status != 200 or "login" not in data:
            fail(f"PAT 校验失败 http={status} {str(data)[:200]}")
        return data

    def ensure_repo(self, owner):
        path = f"/repos/{owner}/{REPO_NAME}"
        status, data = self.call(path)
        if status == 200:
            if not data.get("private"):
                say("[repo] 已存在且为公开仓库，正在改为私有…")
                code, _ = self.call(path, "PATCH", {"private": True})
                if code != 200:
                    warn(f"改为私有失败 http={code}，请手动确认仓库可见性")
            say(f"[repo] 复用已有仓库 {data['full_name']}")
            return data["full_name"], data["html_url"], False
        if status != 404:
            fail(f"查询仓库失败 http={status} {str(data)[:200]}")
        say("[repo] 创建私有仓库…")
        code, data = self.call("/user/repos", "POST", {
            "name": REPO_NAME,
            "private": True,
            "description": "WorkBuddy daily check-in bot (auto deployed)",
            "has_issues": False, "has_wiki": False, "has_projects": False,
            "auto_init": False,
        })
        if code not in (200, 201):
            fail(f"创建仓库失败 http={code} {str(data)[:200]}")
        say(f"[repo] 已创建 {data['full_name']}")
        return data["full_name"], data["html_url"], True

    def set_secret(self, full_name, name, value):
        status, info = self.call(f"/repos/{full_name}/actions/secrets/public-key")
        if status != 200 or "key" not in info:
            fail(f"获取仓库公钥失败 http={status}")
        sealed = seal_b64(value.encode(), info["key"])
        code, data = self.call(
            f"/repos/{full_name}/actions/secrets/{name}", "PUT",
            {"encrypted_value": sealed, "key_id": info["key_id"]})
        if code not in (201, 204):
            fail(f"写入 Secret {name} 失败 http={code} {str(data)[:200]}")
        say(f"[secret] {name} 已写入（{len(value)} 字符）")

    def dispatch(self, full_name, workflow="checkin.yml", ref="main"):
        for attempt in range(1, 7):
            code, data = self.call(
                f"/repos/{full_name}/actions/workflows/{workflow}/dispatches",
                "POST", {"ref": ref})
            if code == 204:
                say("[run] 已触发 workflow_dispatch")
                return True
            if code == 404:
                say(f"[run] workflow 尚未被索引（{attempt}/6），等待后重试…")
                time.sleep(8)
                continue
            warn(f"触发失败 http={code} {str(data)[:200]}")
            return False
        warn("workflow 一直 404：可能是推送后索引延迟，稍后可在 Actions 页手动点 Run workflow")
        return False

    def set_workflow_state(self, full_name, enabled, workflow="checkin.yml"):
        """启用/停用定时工作流。缺 Secrets 时停用，避免每天红色失败。"""
        action = "enable" if enabled else "disable"
        code, data = self.call(
            f"/repos/{full_name}/actions/workflows/{workflow}/{action}", "PUT")
        if code == 204:
            say(f"[workflow] 已{'启用' if enabled else '停用'}定时运行")
            return True
        warn(f"{action} workflow 失败 http={code} {str(data)[:160]}")
        return False

    def latest_run(self, full_name, since_ts):
        code, data = self.call(
            f"/repos/{full_name}/actions/runs?event=workflow_dispatch&per_page=10")
        if code != 200:
            return None
        for run in data.get("workflow_runs", []):
            created = run.get("created_at", "")
            try:
                ts = time.mktime(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:  # noqa: BLE001
                continue
            if ts + 90 >= since_ts:
                return run
        return None

    def wait_run(self, full_name, since_ts, timeout=240):
        run = None
        for _ in range(timeout // 6):
            run = self.latest_run(full_name, since_ts)
            if run:
                break
            time.sleep(6)
        if not run:
            warn("未找到刚触发的运行记录")
            return None
        run_id = run["id"]
        say(f"[run] 运行 #{run['run_number']} id={run_id}，等待完成…")
        for _ in range(timeout // 5):
            code, data = self.call(f"/repos/{full_name}/actions/runs/{run_id}")
            if code == 200 and data.get("status") == "completed":
                say(f"[run] 结论: {data.get('conclusion')}")
                return data
            time.sleep(5)
        warn("等待运行超时，可稍后到 Actions 页面查看")
        return run

    def run_logs(self, full_name, run_id):
        url = f"{GH_API}/repos/{full_name}/actions/runs/{run_id}/logs"
        status, data = self.call(url, raw=True)
        if status in (301, 302, 303, 307, 308) and isinstance(data, dict):
            loc = data.get("_location", "")
            if loc:
                status, data = http(loc, raw=True, follow=True)
        if status != 200 or not isinstance(data, (bytes, bytearray)):
            warn(f"拉取日志失败 http={status}")
            return ""
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            chunks = []
            for name in zf.namelist():
                if name.endswith(".txt"):
                    chunks.append(zf.read(name).decode("utf-8", "replace"))
            return "\n".join(chunks)
        except Exception as exc:  # noqa: BLE001
            warn(f"解析日志压缩包失败: {exc}")
            return ""


# ---------------------------------------------------------------- 加密

def seal_b64(message, public_key_b64):
    """libsodium sealed box 加密，返回 base64（GitHub Secret 格式）。"""
    if _HAS_NACL:
        pk = PublicKey(base64.b64decode(public_key_b64))
        return base64.b64encode(SealedBox(pk).encrypt(message)).decode()
    import libsodium_seal
    return libsodium_seal.seal_b64(message, public_key_b64)


# ---------------------------------------------------------------- 文件收集

def collect_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or fn.endswith((".pyc", ".log")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((rel, full))
    return sorted(out)


# ---------------------------------------------------------------- git

def run_git(args, cwd, env_extra=None, timeout=120):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, env=env, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def push_with_git(full_name, root):
    """git 推送，失败重试 3 次。返回 (成功?, 说明)。"""
    token = os.environ["GITHUB_TOKEN"]
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    remote = f"https://github.com/{full_name}.git"

    if not shutil.which("git"):
        return False, "未安装 git"

    run_git(["init", "-q"], root)
    run_git(["config", "user.name", "workbuddy-bot"], root)
    run_git(["config", "user.email", "bot@users.noreply.github.com"], root)
    run_git(["add", "-A"], root)
    code, out = run_git(["commit", "-m", "chore: deploy workbuddy check-in bot"], root)
    if code != 0 and "nothing to commit" not in out.lower():
        warn(f"git commit 返回 {code}: {out[:200]}")
    run_git(["branch", "-M", "main"], root)
    run_git(["remote", "remove", "origin"], root)
    run_git(["remote", "add", "origin", remote], root)

    push_args = [
        "-c", f"http.extraheader=AUTHORIZATION: basic {basic}",
        "-c", "http.postBuffer=524288000",
        "-c", "http.lowSpeedLimit=1000",
        "-c", "http.lowSpeedTime=60",
        "-c", "http.version=HTTP/1.1",
        "push", "-u", "origin", "main", "--force",
    ]
    for attempt in range(1, 4):
        say(f"[push] 第 {attempt}/3 次尝试 git push…")
        code, out = run_git(push_args, root, {"GIT_TERMINAL_PROMPT": "0"},
                            timeout=180)
        if code == 0:
            say("[push] git push 成功")
            # 抹掉可能存在的凭据痕迹
            run_git(["remote", "set-url", "origin", remote], root)
            return True, "ok"
        tail = "\n".join(out.strip().splitlines()[-6:])
        warn(f"push 失败（{code}）:\n{tail}")
        if attempt < 3:
            time.sleep(5 * attempt)
    run_git(["remote", "set-url", "origin", remote], root)
    return False, "git push 三次失败"


def push_with_contents_api(gh, full_name, root):
    """兜底：用 Contents API 逐个上传文件。"""
    files = collect_files(root)
    say(f"[api] 改用 Contents API 上传 {len(files)} 个文件…")
    ok = 0
    for rel, full in files:
        with open(full, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        body = {"message": f"chore: add {rel}", "content": content, "branch": "main"}
        code, data = gh.call(f"/repos/{full_name}/contents/{rel}")
        if code == 200 and isinstance(data, dict) and data.get("sha"):
            body["sha"] = data["sha"]
        code, data = gh.call(f"/repos/{full_name}/contents/{rel}", "PUT", body)
        if code in (200, 201):
            ok += 1
            say(f"[api] {rel}")
        else:
            warn(f"上传 {rel} 失败 http={code} {str(data)[:160]}")
    say(f"[api] 完成 {ok}/{len(files)}")
    return ok == len(files)


# ---------------------------------------------------------------- 主流程

def _env(*names):
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def load_wb_creds(n=1):
    """按账号编号取凭证：优先 WORKBUDDY_*_<n>，账号 1 可回落不带后缀的写法。"""
    sfx = [f"_{n}"] + ([""] if n == 1 else [])
    access = _env(*[f"WORKBUDDY_ACCESS_TOKEN{s}" for s in sfx])
    uid = _env(*[f"WORKBUDDY_UID{s}" for s in sfx])
    refresh = _env(*[f"WORKBUDDY_REFRESH_TOKEN{s}" for s in sfx])
    if access and uid:
        return access, uid, refresh

    for name in ([f"tokens_{n}.json"] + (["tokens.json"] if n == 1 else [])):
        path = os.path.join(HERE, "state", name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            access = access or (data.get("accessToken") or "").strip()
            uid = uid or (data.get("uid") or "").strip()
            refresh = refresh or (data.get("refreshToken") or "").strip()
            say(f"[cred] 已从 {path} 补齐缺失凭证")
        except Exception as exc:  # noqa: BLE001
            warn(f"读取 {path} 失败: {exc}")
    return access, uid, refresh


def collect_accounts():
    """账号 1 必填；账号 2 只要配了任一 WORKBUDDY_*_2 就尝试加载。"""
    accounts = [(1, load_wb_creds(1))]
    has2 = any((os.environ.get(f"WORKBUDDY_{k}_2") or "").strip()
               for k in ("ACCESS_TOKEN", "UID", "REFRESH_TOKEN"))
    if has2:
        a2 = load_wb_creds(2)
        if a2[0] and a2[1]:
            accounts.append((2, a2))
        else:
            warn("检测到账号 2 的配置但不完整（accessToken / uid 缺失），已跳过")
    return accounts


def self_test():
    say("[self-test] 检查依赖与加密实现…")
    say(f"  requests : {'已安装' if _HAS_REQUESTS else '未安装（使用标准库）'}")
    say(f"  pynacl   : {'已安装' if _HAS_NACL else '未安装（使用内置实现）'}")
    import libsodium_seal
    ok = libsodium_seal._self_test()
    files = collect_files(HERE)
    say(f"  待部署文件 {len(files)} 个:")
    for rel, _ in files:
        say(f"    - {rel}")
    say("[self-test] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="部署 WorkBuddy 签到到 GitHub Actions")
    ap.add_argument("--self-test", action="store_true", help="本地自检，不联网")
    ap.add_argument("--no-persist", action="store_true",
                    help="不写入 GH_SECRETS_TOKEN（禁用令牌自动续期回写）")
    ap.add_argument("--skip-run", action="store_true", help="不触发首次运行")
    ap.add_argument("--deploy-only", action="store_true",
                    help="只建仓库并推送代码，不写 Secrets（缺凭证时用），并停用定时运行")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    gh_token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not gh_token:
        fail("缺少 GITHUB_TOKEN（classic PAT，需勾选 repo + workflow）", 2)
    _REDACT.append(gh_token)

    if args.deploy_only:
        accounts = []
    else:
        accounts = collect_accounts()
        missing = [n for n, v in (("WORKBUDDY_ACCESS_TOKEN", accounts[0][1][0]),
                                  ("WORKBUDDY_UID", accounts[0][1][1]),
                                  ("WORKBUDDY_REFRESH_TOKEN", accounts[0][1][2])) if not v]
        if missing:
            fail("缺少 WorkBuddy 凭证: " + ", ".join(missing) +
                 "（可通过环境变量传入，或先运行 extract_token.py）", 2)
    for _no, (_a, _u, _r) in accounts:
        for secret in (_a, _r):
            if secret:
                _REDACT.append(secret)

    say("=" * 60)
    say("WorkBuddy 自动签到 · GitHub Actions 部署")
    say("=" * 60)
    say(f"  GitHub PAT        : {mask(gh_token)}")
    if accounts:
        for no, (a, u, r) in accounts:
            say(f"  账号 {no} uid       : {u}")
            say(f"  账号 {no} token     : {mask(a)} / refresh {mask(r)}")
    else:
        say("  WorkBuddy 凭证    : 未提供（--deploy-only）")
    say(f"  加密实现          : {'pynacl' if _HAS_NACL else '内置 libsodium 实现'}")
    say("")

    gh = GitHub(gh_token)
    user = gh.get_user()
    owner = user["login"]
    say(f"[auth] 已认证为 {owner}")

    full_name, html_url, created = gh.ensure_repo(owner)
    say(f"[repo] {html_url}（私有）")

    # 1) 推送代码
    say("")
    pushed, reason = push_with_git(full_name, HERE)
    if not pushed:
        say(f"[push] git 通道不可用（{reason}），启用 Contents API 兜底")
        if not push_with_contents_api(gh, full_name, HERE):
            fail("Contents API 兜底也未全部成功，请检查网络后重试")

    # 2) 写入 Secrets
    say("")
    if args.deploy_only:
        say("[secret] 跳过：--deploy-only 模式不写 Secrets")
        gh.set_workflow_state(full_name, enabled=False)
        say("")
        say("=" * 60)
        say(f"仓库地址 : {html_url}（私有，已停用定时运行）")
        say("待补步骤 : 拿到 WorkBuddy 凭证后重跑本脚本（不加 --deploy-only），")
        say("           会自动写入 3 个 Secrets、启用定时并触发首次运行")
        say("=" * 60)
        return 0

    for no, (access, uid, refresh) in accounts:
        suffix = f"_{no}"
        gh.set_secret(full_name, f"WORKBUDDY_ACCESS_TOKEN{suffix}", access)
        gh.set_secret(full_name, f"WORKBUDDY_UID{suffix}", uid)
        gh.set_secret(full_name, f"WORKBUDDY_REFRESH_TOKEN{suffix}", refresh)
        say(f"[secret] 账号 {no} 的 3 个 Secrets 已写入")
    if not args.no_persist:
        gh.set_secret(full_name, "GH_SECRETS_TOKEN", gh_token)
    gh.set_workflow_state(full_name, enabled=True)

    # 3) 触发并等待
    say("")
    if args.skip_run:
        say("[run] 已跳过首次触发（--skip-run）")
        return 0

    since = time.time()
    if not gh.dispatch(full_name):
        say(f"[done] 仓库已就绪: {html_url}")
        say("       定时任务将从下一个 cron 时间点开始生效")
        return 0

    run = gh.wait_run(full_name, since)
    if not run:
        say(f"[done] 仓库已就绪: {html_url}（未能取到运行结果）")
        return 0

    logs = gh.run_logs(full_name, run["id"])
    conclusion = run.get("conclusion") or "unknown"
    say("")
    say("─" * 60)
    say("首次运行日志结论")
    say("─" * 60)
    if logs:
        interesting = [ln for ln in logs.splitlines()
                       if re.search(r"\[(ok|error|warn|auth|persist)\]|WB_RESULT|签到", ln)]
        for line in interesting[-25:]:
            say("  " + line)
        if not interesting:
            say("  （日志中未找到结论行）")
    else:
        say("  （无日志内容）")

    say("")
    say("=" * 60)
    say(f"仓库地址 : {html_url}")
    say(f"运行结论 : {conclusion}")
    say("定时     : 每天北京时间 09:10 与 20:10（UTC 01:10 / 12:10）")
    say("=" * 60)
    return 0 if conclusion == "success" else 0


if __name__ == "__main__":
    sys.exit(main())
