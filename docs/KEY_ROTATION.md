# 轮换 API Key：具体怎么操作

## 0. 先分清"为什么轮换"

参考项目（`CFDFlow-Agent-LangGraph-Context`、`mechresearch-agent-plan-execute-fixed`）的 `.env` 里曾经**明文保存**过四个凭据。它们没有被提交进 Git（那边没有仓库），但已经以明文形式落在磁盘上，任何拿到这些文件、截图或备份的人都能直接调用。

所以要做的事是：**让旧值立刻失效**，然后按需生成新值。

本仓库（InferenceLab Agent）只会用到其中一个：

| 凭据 | 本仓库是否使用 | 说明 |
|---|---|---|
| DeepSeek / 任意 OpenAI 兼容 `LLM_API_KEY` | **是**（仅 `LLM_AUTH_MODE=bearer` 时） | 用于报告解释；`none` 模式不需要 |
| Tavily `TAVILY_API_KEY` | 否 | 参考项目的联网检索，本仓库已改为本地检索 |
| SerpAPI `SERPAPI_API_KEY` | 否 | 同上 |
| 智谱 `VISION_LLM_API_KEY` | 否 | 参考项目的视觉审稿，本仓库无此功能 |

→ 结论：四个都要轮换（为了止损），但**只有第一个需要写进本仓库的 `.env`**。

## 1. 通用四步

1. **登录服务商控制台**，找到 API Keys / 密钥管理；
2. **删除或禁用旧 key**（不是"再建一个"，旧的要失效）；
3. **新建 key**，只复制一次，粘贴到本地 `.env`（`backend/.env` 或仓库根 `.env`）；
4. **重启后端**并验证：旧 key 调用应返回 401，新 key 正常。

> 不要把 key 贴进聊天窗口、issue、截图或录屏。本项目所有文档与模板只出现占位符。

## 2. 各平台入口与操作

### 2.1 DeepSeek（`LLM_API_KEY`）

1. 打开 <https://platform.deepseek.com/api_keys> 并登录；
2. 在列表中找到旧 key（就是之前 `.env` 里 `sk-4d0d...` 那条），点删除/禁用；
3. 点"创建 API key"，命名如 `inferencelab-local`，复制 `sk-...`；
4. 写入本地 `.env`：

```dotenv
LLM_ENABLED=true
LLM_MODEL_ID=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的新key
LLM_AUTH_MODE=bearer
```

5. 重启后端，打开前端点"检测模型连通性"，应显示 `ms · N 个模型`。

### 2.2 Tavily（`TAVILY_API_KEY`）

1. 打开 <https://app.tavily.com/home> 登录；
2. API Keys 区域删除旧 key（`tvly-dev-...`）；
3. 新建 key 并复制；
4. **不需要写入本仓库**。如果还要用参考项目，写到那个项目的 `.env` 里。

### 2.3 SerpAPI（`SERPAPI_API_KEY`）

1. 打开 <https://serpapi.com/manage-api-key> 登录；
2. 点击旧 key 旁的删除/重新生成；
3. 新 key 只写到需要它的项目里，本仓库不配置。

### 2.4 智谱开放平台（`VISION_LLM_API_KEY`）

1. 打开 <https://open.bigmodel.cn/usercenter/apikeys> 登录；
2. 删除旧 key（`23cc5e4a...` 那条）；
3. 新建并复制；
4. 本仓库不使用视觉模型，不配置。

## 3. 如何确认旧 key 真的失效

用旧 key 发一个最小请求，期望 `401 Unauthorized`（数字以服务商为准）：

```powershell
# OpenAI 兼容端点（DeepSeek 等）
curl.exe -s -o NUL -w "%{http_code}`n" https://api.deepseek.com/v1/models `
  -H "Authorization: Bearer 旧key"

# Tavily
curl.exe -s -o NUL -w "%{http_code}`n" https://api.tavily.com/search `
  -H "Content-Type: application/json" `
  -d '{\"api_key\":\"旧key\",\"query\":\"test\"}'
```

返回 401/403 即视为已失效；仍然 200 说明删除动作没生效，回到控制台复查。

## 4. 轮换完成后

- [ ] 本仓库只保留 `LLM_API_KEY`（或 `LLM_AUTH_MODE=none` 时留空）
- [ ] `python scripts/secret_scan.py --worktree` 通过
- [ ] `python scripts/env_template.py --check` 通过
- [ ] 前端顶栏显示"模型已连接"，`/api/llm/health` 中 `reachable=true`
- [ ] 旧 key 请求返回 401
- [ ] 参考项目的 `.env` 里不再有有效旧值（或直接删除这些文件）

## 5. 如果怀疑已经泄露

1. **先轮换**（上面的步骤），不要先动仓库；
2. 确认泄露范围：`git log -S "<key 前 6 位>" --oneline`；
3. 如果进过 Git：`git filter-repo --replace-text` 重写历史 → `git push --force-with-lease`；
4. 检查服务商账单/用量是否有异常调用；
5. 复盘是哪一步绕过了钩子（`git add -f`、`--no-verify`），补一条测试。
