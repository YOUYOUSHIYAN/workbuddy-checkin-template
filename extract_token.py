#!/usr/bin/env python3
"""从本机 WorkBuddy 客户端登录态文件提取凭证，写入 state/tokens.json。

支持的登录态文件位置：
    Windows  %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
    macOS    ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
    Linux    ~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info

如果客户端把令牌加密存储（新版本行为），本脚本会明确报错并给出替代方案，
不会尝试猜测或破解。

用法:
    python3 extract_token.py                 # 自动定位并提取
    python3 extract_token.py --src 路径       # 手动指定文件
    python3 extract_token.py --out 路径       # 自定义输出位置
"""

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "state", "tokens.json")

CANDIDATES = {
    "win32": [os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "CodeBuddyExtension", "Data", "Public", "auth",
                           "workbuddy-desktop.info")],
    "darwin": [os.path.expanduser(
        "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/"
        "workbuddy-desktop.info")],
    "linux": [os.path.expanduser(
        "~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info")],
}


def mask(value):
    """打码显示，永不输出完整令牌。"""
    if not value:
        return "(空)"
    if len(value) <= 10:
        return value[:2] + "*" * (len(value) - 2)
    return f"{value[:4]}{'*' * 8}{value[-4:]} (长度 {len(value)})"


def locate(src=None):
    if src:
        return src if os.path.exists(src) else None
    platform = "win32" if sys.platform.startswith("win") else (
        "darwin" if sys.platform == "darwin" else "linux")
    for path in CANDIDATES.get(platform, []):
        if path and os.path.exists(path):
            return path
    return None


def is_encrypted(value):
    """判断字段值是否为加密信封。"""
    if isinstance(value, dict):
        if value.get("$wbEncrypted") or value.get("envelope"):
            return True
        if "ciphertext" in value and ("nonce" in value or "authTag" in value):
            return True
    if isinstance(value, str):
        head = value.strip()[:16].upper()
        if head.startswith("ENCRYPTED"):
            return True
    return False


def dig(data, *names):
    """在嵌套 JSON 中按字段名取值，兼容 auth.accessToken 与扁平结构。"""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in names and key not in found:
                    found[key] = val
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="提取 WorkBuddy 登录凭证")
    ap.add_argument("--src", help="登录态文件路径（默认自动定位）")
    ap.add_argument("--out", default="", help="输出 JSON 路径")
    ap.add_argument("--account", default="",
                    help="账号编号：写到 state/tokens_<编号>.json，与 checkin.py --account 对应")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="以 JSON 输出结果（令牌仍然打码）")
    args = ap.parse_args(argv)

    # 未显式指定 --out 时，按 --account 决定文件名，保证与 checkin.py 读的路径一致
    if not args.out:
        suffix = f"_{args.account.strip()}" if args.account.strip() else ""
        args.out = os.path.join(HERE, "state", f"tokens{suffix}.json")

    path = locate(args.src)
    if not path:
        msg = ("未找到登录态文件。请确认 WorkBuddy 客户端已登录，"
               "或用 --src 手动指定 workbuddy-desktop.info 的路径。")
        print(f"[error] {msg}", file=sys.stderr)
        if args.as_json:
            print(json.dumps({"ok": False, "reason": "file_not_found"},
                             ensure_ascii=False))
        return 2

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        msg = "登录态文件不是合法 JSON，可能为新版加密格式。"
        print(f"[error] {msg}", file=sys.stderr)
        print_alternative()
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 读取失败: {exc}", file=sys.stderr)
        return 2

    fields = dig(data, "accessToken", "refreshToken", "uid", "machineId",
                 "sessionId", "sessionState", "uin")

    encrypted = [k for k in ("accessToken", "refreshToken") if is_encrypted(fields.get(k))]
    if encrypted:
        msg = (f"检测到令牌被加密存储（字段 {', '.join(encrypted)} 为 AES-GCM 信封），"
               "无法直接解析。")
        print(f"[error] {msg}", file=sys.stderr)
        if args.as_json:
            print(json.dumps({"ok": False, "reason": "encrypted",
                              "fields": encrypted}, ensure_ascii=False))
        print_alternative()
        return 3

    access = fields.get("accessToken")
    refresh = fields.get("refreshToken")
    uid = fields.get("uid") or fields.get("uin")

    access = access if isinstance(access, str) else ""
    refresh = refresh if isinstance(refresh, str) else ""
    uid = str(uid) if uid else ""

    if not access or not uid:
        msg = "文件中没有可用的 accessToken / uid 明文字段。"
        print(f"[error] {msg}", file=sys.stderr)
        if args.as_json:
            print(json.dumps({"ok": False, "reason": "missing_fields"},
                             ensure_ascii=False))
        print_alternative()
        return 3

    payload = {"accessToken": access, "uid": uid, "refreshToken": refresh}
    for extra in ("machineId", "sessionId"):
        val = fields.get(extra)
        if not val and extra == "sessionId":
            val = fields.get("sessionState")  # 客户端里叫 sessionState
        if isinstance(val, str) and val:
            payload[extra] = val

    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(out_path, 0o600)
        perm = "600"
    except Exception:  # noqa: BLE001 - Windows 部分文件系统不支持 POSIX 权限
        perm = "N/A"

    if args.as_json:
        print(json.dumps({
            "ok": True, "source": path, "out": out_path,
            "accessToken": mask(access), "refreshToken": mask(refresh),
            "uid": uid,
        }, ensure_ascii=False))
        return 0

    print(f"[ok] 来源文件: {path}")
    print(f"     accessToken : {mask(access)}")
    print(f"     refreshToken: {mask(refresh)}")
    print(f"     uid         : {uid}")
    print(f"     已写入      : {out_path} (权限 {perm})")
    print("\n下一步：把 accessToken / refreshToken / uid 填入 GitHub Secrets，")
    print("或直接运行 bootstrap.py 由脚本代为写入。")
    return 0


def print_alternative():
    print("""
替代获取方式（推荐）：
  1. 浏览器打开 https://www.codebuddy.cn 并登录同一账号
  2. 按 F12 打开开发者工具 -> Network(网络) 面板
  3. 随便点一个页面内的按钮，找到发往 /v2/ 的请求
  4. 在 Request Headers 中复制 Authorization 的 Bearer 值（即 accessToken）
     以及 X-User-Id 的值（即 uid）
  5. 把这两个值发给我，或写入 state/tokens.json：
     {"accessToken": "...", "uid": "...", "refreshToken": "..."}
""", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
