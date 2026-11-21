#!/usr/bin/env python3
"""离线验证 checkin.py 的分支行为与退出码。

覆盖：
  a) 未签到 -> 签到成功，解析积分与连签天数          期望退出码 0
  b) 今日已签到 -> 无需重复领取，且不重复调用签到接口  期望退出码 0
  c) 接口业务错误 -> 失败，且输出不含成功字样          期望退出码 1
  d) 首次 401 -> refreshToken 续期 -> 签到成功         期望退出码 0
  e) 缺凭证 -> 明确报错                               期望退出码 2

运行: python3 tests/test_local.py
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

from mock_server import CHECKIN_PATH, MockServer  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f"  {detail}" if detail else ""))


def sandbox():
    """把脚本复制到临时目录，避免污染真实 state/。"""
    tmp = tempfile.mkdtemp(prefix="wbcheckin-test-")
    for fn in ("checkin.py", "libsodium_seal.py"):
        shutil.copy(os.path.join(ROOT, fn), os.path.join(tmp, fn))
    return tmp


def run_case(scenario, base, extra_env=None, creds=True):
    workdir = sandbox()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WORKBUDDY_", "GH_", "WB_"))}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    if creds:
        env.update({
            "WORKBUDDY_ACCESS_TOKEN": "test-access-token",
            "WORKBUDDY_UID": "test-uid-0001",
            "WORKBUDDY_REFRESH_TOKEN": "test-refresh-token",
        })
    env["WORKBUDDY_API_BASE"] = base
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run([sys.executable, "checkin.py"], cwd=workdir, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90)
    tokens = os.path.join(workdir, "state", "tokens.json")
    saved = None
    if os.path.exists(tokens):
        with open(tokens, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    log = os.path.join(workdir, "state", "checkin.log")
    log_text = open(log, "r", encoding="utf-8").read() if os.path.exists(log) else ""
    return proc, saved, log_text, workdir


def main():
    with MockServer() as srv:
        print("\n=== a) 未签到 -> 签到成功 ===")
        proc, saved, log_text, wd = run_case("a", srv.base_url("a"))
        out = proc.stdout + proc.stderr
        print(f"  exit={proc.returncode}\n  stdout={proc.stdout.strip()}")
        check("退出码为 0", proc.returncode == 0, f"got {proc.returncode}")
        check("输出包含「签到成功」", "签到成功" in out)
        check("解析出积分 100", "100" in proc.stdout)
        check("解析出连签天数 4", "4" in proc.stdout)
        check("状态查询已调用", any(c["path"].endswith("checkin-activity-status")
                                for c in srv.calls("a")))
        check("签到接口已调用", any(c["path"].endswith("daily-checkin")
                               for c in srv.calls("a")))
        check("日志已追加", "OK" in log_text)
        check("未把令牌写入日志", "test-access-token" not in log_text)
        shutil.rmtree(wd, ignore_errors=True)

        print("\n=== b) 今日已签到 -> 无需重复领取 ===")
        proc, saved, _, wd = run_case("b", srv.base_url("b"))
        out = proc.stdout + proc.stderr
        print(f"  exit={proc.returncode}\n  stdout={proc.stdout.strip()}")
        check("退出码为 0", proc.returncode == 0, f"got {proc.returncode}")
        check("输出包含「无需重复领取」", "无需重复领取" in out)
        check("未调用签到接口（避免重复领取）",
              not any(c["path"].endswith("daily-checkin") for c in srv.calls("b")))
        shutil.rmtree(wd, ignore_errors=True)

        print("\n=== c) 接口业务错误 -> 失败 ===")
        proc, saved, log_text, wd = run_case("c", srv.base_url("c"))
        out = proc.stdout + proc.stderr
        print(f"  exit={proc.returncode}\n  stderr={proc.stderr.strip()[:300]}")
        check("退出码为 1", proc.returncode == 1, f"got {proc.returncode}")
        check("输出不含「签到成功」", "签到成功" not in out)
        check("输出包含失败原因", "签到失败" in out or "code=50000" in out)
        check("日志记录失败", "FAIL" in log_text)
        shutil.rmtree(wd, ignore_errors=True)

        print("\n=== d) 401 -> 续期 -> 签到成功 ===")
        refresh_url = f"http://127.0.0.1:{srv.port}/d/v2/plugin/auth/token/refresh"
        proc, saved, _, wd = run_case("d", srv.base_url("d"),
                                      {"WORKBUDDY_REFRESH_URL": refresh_url})
        out = proc.stdout + proc.stderr
        print(f"  exit={proc.returncode}\n  stdout={proc.stdout.strip()}")
        check("退出码为 0", proc.returncode == 0, f"got {proc.returncode}")
        check("发生了续期", srv.state("d")["refreshed"])
        check("新令牌已回写 state/tokens.json",
              bool(saved) and saved.get("accessToken") == "new-access-token-after-refresh",
              str(saved.get("accessToken") if saved else None))
        check("refreshToken 轮换已同步",
              bool(saved) and saved.get("refreshToken") == "new-refresh-token")
        shutil.rmtree(wd, ignore_errors=True)

        print("\n=== e) 缺凭证 -> 退出码 2 ===")
        proc, _, _, wd = run_case("a", srv.base_url("a"), creds=False)
        print(f"  exit={proc.returncode}\n  stderr={proc.stderr.strip()[:200]}")
        check("退出码为 2", proc.returncode == 2, f"got {proc.returncode}")
        check("提示缺少凭证", "缺少凭证" in (proc.stdout + proc.stderr))
        shutil.rmtree(wd, ignore_errors=True)

    total = len(results)
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 56)
    print(f"合计 {total} 项，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    for name, _, detail in failed:
        print(f"  未通过: {name} {detail}")
    print("=" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
