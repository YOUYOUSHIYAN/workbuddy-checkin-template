#!/usr/bin/env python3
"""把当次运行的签到结果汇总进 reports/checkin-YYYY-MM.md 并提交回仓库。

为什么需要它：Actions runner 是临时的，state/checkin.log 随运行结束就消失。
只有把结果写回仓库，才能事后一眼看出「过去每一天每个账号到底有没有签成功」。

关键设计：如果某一天整日没有任何记录（运行没触发 / 失败在更早阶段），
脚本会显式插入「无记录」行——否则空白会让人误以为那天签到成功。

用法：
    WORKBUDDY_LOG_FILE=state/checkin.log python3 tools/report.py --dry-run
    python3 tools/report.py            # 实际写入并 git commit/push
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS = os.path.join(ROOT, "reports")

# [2026-11-16 09:10:03] [主号] OK 签到成功，获得 100 积分，连续签到第 9 天
LINE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*"
                  r"\[(?P<who>[^\]]+)\]\s*(?P<rest>.*)$")
WEEK = "一二三四五六日"


def read_rows(path):
    """解析日志为 [(date, time, who, status, detail)]。"""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = LINE.match(raw.strip())
            if not m:
                continue
            ts = m.group("ts")
            who = m.group("who").strip()
            rest = m.group("rest").strip()
            status = "UNKNOWN"
            for key in ("OK", "ALREADY", "FAIL"):
                if rest.upper().startswith(key):
                    status = key
                    rest = rest[len(key):].strip()
                    break
            rows.append((ts[:10], ts[11:16], who, status, rest))
    return rows


POINTS = re.compile(r"获得\s*(\d+)\s*积分")
STREAK = re.compile(r"连续签到第?\s*(\d+)\s*天")


def fmt(status, detail):
    """把日志正文压成一行可读结论，去掉与状态标签重复的措辞。"""
    pts = POINTS.search(detail)
    streak = STREAK.search(detail)
    if status == "OK":
        bits = ["✅ 签到成功"]
        if pts:
            bits.append(f"+{pts.group(1)} 积分")
        if streak:
            bits.append(f"连签 {streak.group(1)} 天")
        return " · ".join(bits)
    if status == "ALREADY":
        return "✅ 已签到（今日额度已领）"
    if status == "FAIL":
        return f"❌ 失败 · {detail or '未知错误'}"
    return detail or "（空）"


def with_gaps(rows, today, min_gap_date=None):
    """在表格里插入「无记录」的日期段，让漏跑可见。"""
    by_day = {}
    for d, tm, who, status, detail in rows:
        by_day.setdefault(d, []).append((tm, who, status, detail))

    dates = sorted(by_day)
    out = []
    if not dates:
        out.append("_暂无任何记录。_")
        return out

    first = date.fromisoformat(dates[0])
    last = max(max(date.fromisoformat(d) for d in dates), today)
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        out.append(f"## {key}（周{WEEK[cursor.weekday()]}）")
        out.append("")
        if key in by_day:
            out.append("| 时间(北京) | 账号 | 结果 |")
            out.append("| --- | --- | --- |")
            for tm, who, status, detail in sorted(by_day[key]):
                out.append(f"| {tm} | {who} | {fmt(status, detail)} |")
        else:
            out.append("| 时间(北京) | 账号 | 结果 |")
            out.append("| --- | --- | --- |")
            out.append("| — | — | ⚠️ 当日无任何记录（定时任务未运行或运行前就失败） |")
        out.append("")
        cursor += timedelta(days=1)
    return out


def header(month_key):
    return [
        f"# 签到记录 · {month_key}",
        "",
        "本文件由 GitHub Actions 每次运行后自动追加，**不要手工编辑**（会被下次提交覆盖）。",
        "",
        "- `✅ 签到成功`：本次运行领到了积分，`连签 N 天` 为接口返回的实际值",
        "- `✅ 已签到`：当天已有人签过，属正常情况（每天跑两次，第二次必然是这个结果）",
        "- `❌ 失败`：接口或网络报错，需要排查",
        "- `⚠️ 当日无任何记录`：这一天定时任务没有成功跑起来",
        "",
        "---",
        "",
    ]


def render(log_path, today=None):
    today = today or datetime.now(BJ).date()
    rows = read_rows(log_path)
    body = with_gaps(rows, today)
    return "\n".join(header(today.strftime("%Y-%m")) + body) + "\n", rows


def sh(args, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def commit(month_file, today):
    sh(["git", "config", "user.name", "github-actions[bot]"])
    sh(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    rel = os.path.relpath(month_file, ROOT)
    add = sh(["git", "add", rel])
    if add.returncode != 0:
        print(f"[report] git add 失败: {add.stderr.strip()[:300]}")
        return False
    diff = sh(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("[report] 记录无变化，跳过提交")
        return True
    cm = sh(["git", "commit", "-m", f"chore: 签到记录 {today.isoformat()}"])
    if cm.returncode != 0:
        print(f"[report] git commit 失败: {cm.stderr.strip()[:300]}")
        return False
    for attempt in (1, 2, 3):
        push = sh(["git", "push", "origin", "HEAD:main"])
        if push.returncode == 0:
            print("[report] 已提交并推送到仓库")
            return True
        print(f"[report] push 第 {attempt} 次失败，尝试同步后重试")
        sh(["git", "pull", "--rebase", "--autostash", "origin", "main"])
    print("[report] push 连续失败，本次记录未写回（签到本身不受影响）")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="汇总签到记录并提交")
    ap.add_argument("--log", default=(os.environ.get("WORKBUDDY_LOG_FILE")
                                      or os.path.join(ROOT, "state", "checkin.log")))
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写入文件")
    args = ap.parse_args(argv)

    today = datetime.now(BJ).date()
    text, rows = render(args.log, today)

    if args.dry_run:
        print(text)
        print(f"[report] 解析到 {len(rows)} 条记录", file=sys.stderr)
        return 0

    if not rows:
        print("[report] 日志为空（可能两个账号都在更早阶段失败），不生成空文件")
        return 0

    os.makedirs(REPORTS, exist_ok=True)
    month_file = os.path.join(REPORTS, f"checkin-{today.strftime('%Y-%m')}.md")
    with open(month_file, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"[report] 已写入 {month_file}（{len(rows)} 条）")

    comment = os.environ.get("GITHUB_STEP_SUMMARY")
    if comment:
        try:
            with open(comment, "a", encoding="utf-8") as fh:
                fh.write("\n## 本次签到结果\n\n")
                fh.write(text.split("---", 1)[-1].strip() + "\n")
        except Exception:  # noqa: BLE001
            pass

    commit(month_file, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
