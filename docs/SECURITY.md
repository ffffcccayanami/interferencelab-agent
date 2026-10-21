# 密钥与发布安全

目标：**真实 API Key 永不进入 Git 对象库**，即使仓库公开也不消耗项目所有者的额度。

## 1. 风险模型

- 本工作台是本地单用户工具，威胁不在"被别人调用接口"，而在"key 被提交到 GitHub 后被爬虫抓走"；
- 泄漏一旦发生，**删文件、改提交信息都不够**：Git 历史里仍然存在，必须轮换 key；
- 前端 `VITE_*` 变量会被打包进浏览器，等同于公开。

## 2. 三层防线

| 层 | 载体 | 行为 |
|---|---|---|
| 忽略 | `.gitignore` | `.env`、`.env.*`（保留 `.env.example`）、`*.pem`、`backend/var/`、`*.db` |
| 模板 | `scripts/env_template.py` | `--write` 从 `backend/.env` 生成占位符模板；`--check` 校验模板无真实值 |
| 提交 | `.githooks/pre-commit` | `scripts/secret_scan.py --staged`，命中即退出 1（掩码输出，不回显 key） |
| 推送 | `.githooks/pre-push` | 对整个工作区再扫一次 |
| CI | `.github/workflows/security.yml` | gitleaks（含历史）+ 本地扫描器 + 模板校验 + 前端变量守卫 |
| 运行 | `app/settings.py` | `SecretStr`；`/api/meta` 只回布尔与 `sk-a...3456` 形式指纹；异常脱敏 |

### 规则与白名单

命中规则：`sk-...`、`tvly-...`、`AKIA...`、`ghp_...`、`xox?-`、私钥块、`(api_key|token|secret|password)=<20+ 字符>`。

放行：包含 `YOUR_`、`your-`、`${`、`<`、`changeme`、`example`、`test-key` 等占位符标记的值。

误报：在命中行加 `# allow-secret: <原因>`，并在 PR 里说明；不要用 `git commit --no-verify`。

## 3. 首次配置

```powershell
copy .env.example backend/.env
# 编辑 backend/.env：LLM_BASE_URL / LLM_MODEL_ID / LLM_API_KEY
python scripts/env_template.py --check
python scripts/install_hooks.py
```

`backend/.env` 只存在于本机；仓库里永远是占位符。

## 4. 轮换清单（建议在公开发布前执行一次）

参考项目（`CFDFlow-Agent-LangGraph-Context`、`mechresearch-agent-plan-execute-fixed`）的 `.env` 中曾以明文保存过以下凭据。**它们已落盘为明文，按"已泄漏"处理**：

- [ ] DeepSeek API Key（`LLM_API_KEY`）：在控制台吊销旧 key，生成新 key，只写入 `backend/.env`
- [ ] Tavily API Key（`TAVILY_API_KEY`）：同上
- [ ] SerpAPI Key（`SERPAPI_API_KEY`）：同上
- [ ] 智谱开放平台 Key（`VISION_LLM_API_KEY`）：同上
- [ ] 确认新 key 未出现在任何 `README`、截图、录屏、日志中
- [ ] `python scripts/secret_scan.py --worktree` 通过

轮换后请同步更新本机其它项目里的旧值，避免继续使用已吊销的 key 造成排查困扰。

## 5. 泄露处置流程（Runbook）

1. **止血**：立刻在服务商后台吊销/轮换该 key（不要先清理仓库）；
2. **确认范围**：`git log -S '<key 前 6 位>' --oneline` 找出引入提交；
3. **重写历史**：`git filter-repo --replace-text replacements.txt`（或在未推送时 `git reset`）；
4. **强推并通知**：`git push --force-with-lease`，通知 fork/协作者重新 clone；
5. **复盘**：检查是哪一层防线失效（通常是 `git add -f` 或 `--no-verify`），补测试用例；
6. **记录**：在 issue 中记录时间线（不含 key 本身）。

## 6. GitHub 仓库设置（一次性，人工完成）

- Settings → Code security：开启 **Secret scanning** 与 **Push protection**；
- 开启 **Dependabot alerts**；
- 分支保护：`main` 要求 PR + CI（`ci`、`security`）通过；
- 不要给 Actions 配置任何 `LLM_*` secret：测试全部离线运行。

## 7. 发布前检查清单

- [ ] `backend/.env` 不出现在 `git status` / `git ls-files`
- [ ] `python scripts/env_template.py --check` 通过
- [ ] `python scripts/secret_scan.py --worktree` 通过
- [ ] `python scripts/check_frontend_env.py` 通过
- [ ] `fixtures/` 只有合成数据，`README` 已注明
- [ ] 截图/录屏中没有 `.env`、token、内网地址
- [ ] 记录中的实验数字能追溯到 `result.json`，不是口头估计

## 8. 模型端点相关的额外约定

- 配置文件位置：`backend/.env` 优先，其次仓库根 `.env`；两者都被忽略，`/api/meta.config_source` 会显示实际生效的文件；
- `LLM_BASE_URL` 不是密钥，但**可能是内网主机名**：
  - `/api/meta` 只返回 `host:port`（`llm_endpoint`），不返回完整路径与查询串；
  - `/api/llm/health` 只返回端点、状态码、模型列表与延迟，不返回请求头；
  - 录屏/截图前确认地址栏与页面上的端点信息可以公开；
- 前端**永不**持有模型地址凭据：只允许 `VITE_API_BASE_URL`，任何 `VITE_*KEY/TOKEN` 都会被 CI 拒绝；
- 隧道只绑 `127.0.0.1`：`ssh -N -L 8000:127.0.0.1:8000 user@host`，不要把远端 vLLM 的 8000 直接暴露公网；
- 不要为"自签名证书"关闭 TLS 校验；确有需要请在反向代理层处理证书；
- `LLM_AUTH_MODE=none` 只用于**本机/隧道**端点；云端端点一律 `bearer`。
