# WorkBuddy 每日自动签到（GitHub Actions 版）

每天北京时间 **09:10** 和 **20:10** 自动签到，结果写回仓库，人不在电脑前也能事后逐日核对。
纯 Python 标准库，零依赖，不需要服务器。

- 每天 100 积分，连续第 7 天额外 1000，7 天一轮
- 接口幂等：跑两次不会重复领取、不会扣分
- 部署产物是**你自己名下的私有仓库**，凭证存 GitHub Secrets，不入库

---

## 让 AI 帮你装（推荐）

把下面这段连同仓库链接一起丢给你的 AI（WorkBuddy / Claude Code / Cursor 都行）：

```text
帮我装一下这个自动签到：https://github.com/YOUYOUSHIYAN/workbuddy-checkin-template

请先读仓库里的 AGENTS.md，严格按它执行，不要自己发挥。
过程中需要我提供什么，就按 AGENTS.md 第 2 节一次问清楚，别一步步来回问。
装完按第 4 节的三条硬指标验证，把结果和仓库地址告诉我。
```

AI 会拿走三样输入（GitHub PAT + WorkBuddy 的 accessToken / uid / refreshToken），
然后自己建私有仓库、写 Secrets、触发首次运行、验证结果。**你不需要手动点一堆网页按钮。**

---

## 手动装（3 条命令）

前提：`python3` 3.8+ 与 `git`。

```bash
git clone https://github.com/YOUYOUSHIYAN/workbuddy-checkin-template.git
cd workbuddy-checkin-template
python3 bootstrap.py --self-test        # 本地自检，应输出 PASS

GITHUB_TOKEN=ghp_xxx \
WORKBUDDY_ACCESS_TOKEN=xxx \
WORKBUDDY_UID=xxx \
WORKBUDDY_REFRESH_TOKEN=xxx \
python3 bootstrap.py
```

脚本依次：校验 PAT → 建私有仓库 `workbuddy-checkin` → 推送代码 →
加密写入 5 个 Secrets → 启用并触发 workflow → 轮询结果打印签到结论。

**GitHub PAT 要 classic 类型，勾 `repo` + `workflow`**（少了 `workflow`，推 `.github/workflows/` 会被拒绝）。

### 三个 WorkBuddy 凭证怎么拿

```bash
python3 extract_token.py                # 自动从本机登录态提取
python3 extract_token.py --account 2    # 第二账号
```

若提示「检测到令牌被加密存储」（新版客户端把字段存成 AES-GCM 信封，密钥不在文件里），
改用浏览器：打开 `codebuddy.cn` 登录 → `F12` → Network → 找一个发往 `/v2/` 的请求 →
复制 Request Headers 里 `Authorization` 的 Bearer 值（accessToken）和 `X-User-Id`（uid）。
`refreshToken` 拿不到也能用，只是约 30 天后要人工重取。

---

## 双账号

不需要改代码。加一组带 `_2` 后缀的变量即可：

```bash
WORKBUDDY_ACCESS_TOKEN_2=xxx WORKBUDDY_UID_2=xxx WORKBUDDY_REFRESH_TOKEN_2=xxx \
  python3 bootstrap.py
```

workflow 里账号 2 那步带条件（`HAS_ACCOUNT_2`），**没配 `_2` 的 Secrets 就自动跳过**，
不会产生红色失败记录。两个账号串行执行，一个失败不影响另一个。

---

## 装上之后怎么核对每天有没有签成功

打开仓库的 `reports/checkin-YYYY-MM.md`，按月一个文件，每天一张小表：

| 时间(北京) | 账号 | 结果 |
| --- | --- | --- |
| 09:10 | 账号1 | ✅ 签到成功 · +100 积分 · 连签 9 天 |
| 09:10 | 账号2 | ✅ 签到成功 · +100 积分 · 连签 2 天 |
| 20:10 | 账号1 | ✅ 已签到（今日额度已领） |

- `✅ 签到成功`：这次真领到了，`连签 N 天` 是接口返回值
- `✅ 已签到`：当天已领过，正常（每天两次，第二次必然是它）
- `❌ 失败`：接口或网络报错，去 Actions 日志看
- `⚠️ 当日无任何记录`：这天定时任务整日没跑起来，**这一行是主动补的**，别把空白当成功

交叉验证：Actions 运行列表（缺哪天一眼可见）+ 连签天数自增（漏一天会归 1）。

---

## 原理

签到就是一次带登录态的 HTTP 请求：

| 用途 | 地址 |
| --- | --- |
| 查状态 | `POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status` |
| 签到 | `POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin` |
| 换令牌 | `POST https://copilot.tencent.com/v2/plugin/auth/token/refresh`（头 `X-Refresh-Token`） |

头：`Authorization: Bearer <accessToken>`、`X-User-Id: <uid>`。
`code=0` 成功，`code=10001` 表示今天已签（同样按成功处理）。

令牌自动续期：accessToken 到期（约 30 天）后遇 401 → 用 refreshToken 换新 → 重试 →
把新令牌加密写回 Secrets（需要 `GH_SECRETS_TOKEN`）。所以只要每月成功跑一次，就能一直滚下去。

细节、边界情况、排障对照表都在 **[AGENTS.md](AGENTS.md)**。

---

## 目录

```
├── AGENTS.md                       # 给 AI 的安装手册（也可当人看的详细文档）
├── checkin.py                      # 主脚本：查状态 → 签到 → 续期 → 写日志
├── extract_token.py                # 从本机登录态提取凭证（输出自动打码）
├── bootstrap.py                    # 一键部署：建库 → 推送 → 写 Secrets → 触发 → 取日志
├── libsodium_seal.py               # 纯标准库 libsodium sealed box（加密 Secrets）
├── tools/report.py                 # 汇总结果写回 reports/（供事后核对）
├── .github/workflows/checkin.yml   # 定时任务（UTC 01:10 / 12:10）
├── install_auto.sh / .ps1          # 本机 cron / 计划任务兜底方案
└── tests/                          # mock 接口 + 31 项断言，全部离线可跑
```

本地验证：

```bash
python3 tests/test_local.py        # 21 项：签到分支、续期、缺凭证
python3 tests/test_regressions.py  # 10 项：多账号隔离、写回失败告警、文案误判
```

---

## 风险与边界

- 这是对**你自己账号**的签到做自动化，不要用于他人账号，也不要提高频率（每天 2 次足够）
- 自动化调用第三方接口可能不符合平台使用条款，账号风险自担；上游接口随时可能变，变了要跟着改
- 不要把这个仓库改成 Public 后在里面填凭证；Secrets 虽不随 fork 泄露，但没必要公开你的签到行为
- 本项目按 MIT 协议开源，不做任何可用性担保

---

## License

MIT
