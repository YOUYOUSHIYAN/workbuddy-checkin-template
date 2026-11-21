#!/usr/bin/env python3
"""针对三个真实隐患的回归测试（防止以后改回去）。

1. 否定词误判：状态接口文案同时含「未签到」「已签到」时，不能被当成今天已签到。
2. --account 接线：用 _N 后缀的环境变量与 tokens_N.json，日志带账号标签。
3. 续期后写回失败必须发出 ::warning:: 注解，不能静默吞掉。

运行：python3 tests/test_regressions.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from mock_server import MockServer  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def run(workdir, args, env_extra, base_url):
    env = dict(os.environ)
    for key in [k for k in env if k.startswith(("WORKBUDDY_", "WB_GITHUB_REPO"))]:
        env.pop(key, None)
    env.update(env_extra)
    env["WORKBUDDY_API_BASE"] = base_url
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "checkin.py", *args], cwd=workdir,
                          env=env, capture_output=True, text=True, timeout=90)
    return proc


def prepare():
    tmp = tempfile.mkdtemp(prefix="wbreg_")
    for name in ("checkin.py", "libsodium_seal.py"):
        shutil.copy(os.path.join(ROOT, name), tmp)
    return tmp


def main():
    global ROOT
    pkg_root = ROOT

    print("=== 1) 否定词优先：\"今日未签到，上次已签到\" 不能判成已签到 ===")
    tmp = prepare()
    with MockServer() as srv:
        p = run(tmp, [], {"WORKBUDDY_ACCESS_TOKEN": "t", "WORKBUDDY_UID": "u1"},
                srv.base_url("e"))
        check("退出码为 0", p.returncode == 0, f"got {p.returncode}")
        check("确实调用了签到接口",
              any(c["path"].endswith("daily-checkin") for c in srv.calls("e")),
              str([c["path"] for c in srv.calls("e")]))
        check("输出为签到成功而非无需领取",
              "签到成功" in p.stdout and "无需" not in p.stdout,
              p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")

    print()
    print("=== 2) --account 接线 ===")
    tmp2 = prepare()
    with MockServer() as srv:
        p = run(tmp2, ["--account", "2"],
                {"WORKBUDDY_ACCESS_TOKEN_2": "tok-2", "WORKBUDDY_UID_2": "uid-account-2"},
                srv.base_url("a"))
        check("退出码为 0", p.returncode == 0, f"got {p.returncode}")
        uids = [c["uid"] for c in srv.calls("a") if c["path"].endswith("checkin-activity-status")]
        check("使用 _2 后缀的 UID", uids[:1] == ["uid-account-2"], str(uids))
        check("日志带账号标签", "账号2" in p.stdout, p.stdout.strip().splitlines()[0] if p.stdout.strip() else "")
        state_file = os.path.join(tmp2, "state", "tokens_2.json")
        check("状态文件按账号分文件", not os.path.exists(state_file),
              "未续期时不应生成 tokens_2.json")

    print()
    print("=== 3) 续期写回失败必须告警 ===")
    tmp3 = prepare()
    with MockServer() as srv:
        p = run(tmp3, ["--account", "1"],
                {"WORKBUDDY_ACCESS_TOKEN_1": "bad", "WORKBUDDY_UID_1": "u1",
                 "WORKBUDDY_REFRESH_TOKEN_1": "r1",
                 # 续期地址必须也指向 mock，否则会打到真实域名
                 "WORKBUDDY_REFRESH_URL": srv.base_url("d") + "/v2/plugin/auth/token/refresh"},
                srv.base_url("d"))
        check("退出码为 0（续期后签到成功）", p.returncode == 0, f"got {p.returncode}")
        check("有 ::warning:: 注解", "::warning::" in p.stdout,
              [l for l in p.stdout.splitlines() if "::warning::" in l][:1])
        check("状态文件按账号分文件", os.path.exists(os.path.join(tmp3, "state", "tokens_1.json")))

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)
    shutil.rmtree(tmp3, ignore_errors=True)

    print()
    print("=" * 56)
    print(f"合计 {len(PASS) + len(FAIL)} 项，通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
