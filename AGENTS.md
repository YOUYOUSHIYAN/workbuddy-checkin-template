# AGENTS.md — 给 AI 的安装与维护手册

> **如果你是一个 AI Agent，用户把这个仓库链接丢给你并说"帮我装一下"，请严格按本文件执行。**
> 不要先总结这个仓库是干什么的，直接按 §2 开始要输入、按 §3 执行、按 §4 验证。
> 全程只用命令行和 GitHub API，不要让用户手动点 20 个网页按钮。

---

## 1. 这是什么

一个零依赖的 Python 脚本 + GitHub Actions 定时任务，每天北京时间 09:10 和 20:10 各调用一次
WorkBuddy 的签到接口，把结果写回仓库的 `reports/` 目录。

- 单账号、双账号都支持；**接口幂等**，重复跑不会重复领取、不会扣积分
- 部署产物是**用户自己名下的私有仓库**，凭证存在 GitHub Secrets，不入库
- 不需要服务器、不需要本机常开机

---

## 2. 你需要向用户索取的四样东西

| 变量 | 是什么 | 从哪来 | 权限要求 |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | GitHub **classic** PAT | github.com → Settings → Developer settings → Personal access tokens → Tokens (classic) | 勾 `repo` + `workflow` |
| `WORKBUDDY_ACCESS_TOKEN` | WorkBuddy 登录态 accessToken（JWT） | 见 §2.1 | — |
| `WORKBUDDY_UID` | WorkBuddy 用户 uid（JWT 的 `sub`） | 见 §2.1 | — |
| `WORKBUDDY_REFRESH_TOKEN` | 刷新令牌，用于 accessToken 过期后自动续期 | 见 §2.1 | — |

第二账号：同样的三个变量加 `_2` 后缀（`WORKBUDDY_ACCESS_TOKEN_2` 等）。**不配就是单账号，无需改任何代码。**

### 2.1 怎么拿到 WorkBuddy 那三个值

按顺序试，第一个能出结果就停：

1. **跑脚本自动提取**（前提：用户在这台机器上登录过 WorkBuddy 客户端）

   ```bash
   python3 extract_token.py            # 单账号
   python3 extract_token.py --account 2 # 第二账号
   ```

   成功会打印打码后的值并写 `state/tokens.json`（已 gitignore）。
   若输出「检测到令牌被加密存储」→ 走第 2 条。

2. **让用户在浏览器里读**（最稳，30 秒）

   1. 打开 `https://www.codebuddy.cn` 并登录**同一个账号**
   2. `F12` → Network → 随便点一个页面动作触发请求
   3. 找发往 `/v2/` 的请求 → Request Headers 里复制：
      - `Authorization` 的 `Bearer ` 后面那串 → `WORKBUDDY_ACCESS_TOKEN`
      - `X-User-Id` → `WORKBUDDY_UID`
   4. `refreshToken` 在该请求里通常看不到，让用户从客户端登录态文件里取，
      或跳过（见 §5「没有 refreshToken 会怎样」）

3. **让它自己想办法**：如果用户允许，可以直接读本机 `state/tokens.json`、
   或用只读方式从客户端进程内存里找 `eyJ` 开头的 JWT。**只读，不改动用户任何数据。**

> 安全红线：拿到令牌后不要回显完整值（只显示前 4 位 + 长度），不要写进任何会被提交的文件，
> 不要贴进聊天记录或 issue。传参只用环境变量。

---

## 3. 执行（3 条命令）

前提：本机有 `python3`（3.8+）和 `git`。没有 Python 就看 §6 降级路径。

```bash
# 1) 拿到代码
git clone https://github.com/YOUYOUSHIYAN/workbuddy-checkin-template.git
cd workbuddy-checkin-template

# 2) 本地自检（不联网，应输出 PASS）
python3 bootstrap.py --self-test

# 3) 部署（凭据全部走环境变量，不落盘）
GITHUB_TOKEN=ghp_xxx \
WORKBUDDY_ACCESS_TOKEN=xxx \
WORKBUDDY_UID=xxx \
WORKBUDDY_REFRESH_TOKEN=xxx \
python3 bootstrap.py
```

`bootstrap.py` 会依次完成：

1. 校验 PAT → `GET /user` 确认身份
2. 创建**私有**仓库 `<你的账号>/workbuddy-checkin`（已存在则复用）
3. 推送 main（git 失败自动降级为 Contents API 逐文件上传）
4. 用仓库公钥 + libsodium sealed box 加密写入 5 个 Secrets：
   `WORKBUDDY_ACCESS_TOKEN_1` / `WORKBUDDY_UID_1` / `WORKBUDDY_REFRESH_TOKEN_1` /
   `GH_SECRETS_TOKEN`（= 刚才那个 PAT，用于令牌续期后自动回写）
5. 启用 workflow → 触发 `workflow_dispatch` → 轮询结果 → 打印签到结论

预期输出结尾：

```
[run] 运行 #1 已结束：success
[log] 今日签到成功，+100 积分，连签 1 天
[done] 仓库就绪：https://github.com/<user>/workbuddy-checkin
```

**退出码**：`0` 成功 / `1` 部署或运行失败 / `2` 缺凭证。

### 常用参数

| 参数 | 用途 |
| --- | --- |
| `--self-test` | 只做本地自检，不联网 |
| `--deploy-only` | 只建库推代码、不写 Secrets、停用定时（先建库后补凭证时用） |
| `--skip-run` | 部署完不触发首次运行 |
| `--no-persist` | 不写 `GH_SECRETS_TOKEN`，即关闭令牌自动续期（**不推荐**） |

---

## 4. 验证：三条硬指标，全部满足才算装好

1. **Actions 有一次绿色运行**：`GET /repos/{owner}/workbuddy-checkin/actions/runs?per_page=1`
   → `conclusion == "success"`
2. **签到的真实结论**：读那次运行的日志，必须出现「签到成功」或「今日已签到」，
   出现「401」「失败」不算装好
3. **落盘文件存在**：`GET /repos/{owner}/workbuddy-checkin/contents/reports/`
   应能看到 `checkin-YYYY-MM.md`，内容是当天两个时间点各一行 ✅

三条都过 → 告诉用户装好了、仓库地址、每天几点跑、以后怎么自查。**不要再说"应该可以了"。**

---

## 5. 现场排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `workflow_dispatch` 返回 404 | push 后 GitHub 还没索引 workflow | 脚本已重试 6 次；仍失败就等 10 秒重跑 |
| 运行失败，日志只有 401 | accessToken 过期且 refreshToken 也失效 | 让用户重新登录客户端 → §2.1 取新值 → 重跑 `bootstrap.py`（会复用仓库，只更新 Secrets） |
| 日志 `[error] 缺少凭证`，退出码 2 | Secrets 没写进仓库，或名字写错（少了 `_1`） | 检查 5 个 Secret 名字与 §3 完全一致 |
| `git push` 反复失败 | 网络 / 代理 | 脚本会自动降级 Contents API；两者都失败就换网络重试 |
| 某天 `reports` 里出现 `⚠️ 当日无任何记录` | 定时任务那天没跑起来 | 到 Actions 页面点一次 `Run workflow`；连续多天出现要查仓库是否被停用 |
| 公开仓库 60 天无活动，定时任务被停用 | GitHub 对公开仓库的定时任务有此限制 | 本工具部署的是**私有仓库，不受影响**；若有人改成公开，任意一次 push 或手动 Run 即可重置 |

**没有 `refreshToken` 会怎样**：能签到，但 accessToken 到期（约 30 天）后就断了，需要人工重新取。
所以尽量拿到它。

**令牌会自动续期吗**：会。`checkin.py` 遇 401 → 用 refreshToken 换新 → 重试签到 → 用
`GH_SECRETS_TOKEN` 把新令牌加密写回 Secrets。只要每月至少成功跑一次，就能一直滚下去。
`GH_SECRETS_TOKEN` 用细粒度 PAT 更好：仅授权这一个仓库、仅 `Secrets: Read and write`、有效期设 1 年。

---

## 6. 降级路径：用户机器上没 Python

用 GitHub API 手动走一遍（AI 自己调，不要让用户点）：

1. `POST /user/repos` `{"name":"workbuddy-checkin","private":true}`
2. 逐个 `PUT /repos/{owner}/{repo}/contents/{path}` 上传仓库内全部文件
   （workflow 文件需要 PAT 带 `workflow` scope）
3. `GET /repos/{owner}/{repo}/actions/secrets/public-key` 取公钥
4. 用 libsodium sealed box 加密每个值后 `PUT /repos/{owner}/{repo}/actions/secrets/{NAME}`
   （参照本仓库 `libsodium_seal.py`，或用 `pynacl`）
5. `PUT /repos/{owner}/{repo}/actions/workflows/checkin.yml/enable`
6. `POST /repos/{owner}/{repo}/actions/workflows/checkin.yml/dispatches` `{"ref":"main"}`
7. 按 §4 验证

---

## 7. 禁止事项

- ❌ 不要把令牌写进 `.env`、`config.json`、仓库任何文件，或 commit message
- ❌ 不要把仓库建成 Public（凭证虽在 Secrets，但没必要公开用户的签到行为）
- ❌ 不要 fork 本模板后直接在 fork 里填用户的 Secrets —— 让别人 fork 你的仓库等于把凭证交出去
- ❌ 不要提高运行频率（每天 2 次足够，接口幂等；高频轮询属于滥用）
- ❌ 不要尝试破解客户端加密的登录态文件；拿不到就按 §2.1 第 2 条让用户自己复制
- ❌ 不要把这个工具用于非本人账号

---

## 8. 收尾时该告诉用户什么

1. 仓库地址（私有）
2. 每天 09:10 / 20:10（北京时间）各跑一次，第二次是幂等兜底
3. 怎么自查：打开 `reports/checkin-YYYY-MM.md`，一天一行，缺日会显式标 `⚠️`
4. 令牌会自动续期，前提是 `GH_SECRETS_TOKEN` 有效（建议设 1 年，到期前重跑一次 `bootstrap.py`）
5. 想停：仓库 Settings → Actions → Disable，或删掉仓库
