# 接入云端 / 隧道模型端点

本工作台**只让后端**访问模型端点。浏览器（前端）永远不持有模型地址的凭据，也不直接连模型端口。

## 1. 端口一览

| 组件 | 默认端口 | 绑定地址 | 谁访问 |
|---|---|---|---|
| 前端 dev server | `5175` | `127.0.0.1` | 你的浏览器 |
| 后端 API | `8010` | `127.0.0.1`（`INFL_API_HOST`） | 浏览器（经 Vite 代理）、脚本 |
| 模型端点（示例） | `8000` | `127.0.0.1`（SSH 隧道）或云端 `443` | **只有后端** |
| SQLite / 产物 | — | `backend/var/` | 只有后端 |

```text
浏览器 ──5175──▶ Vite（代理 /api） ──8010──▶ FastAPI ──▶ 模型端点（8000 隧道 / 云端 443）
```

三条硬规则：

1. 模型端口**不要**绑定 `0.0.0.0` 或对公网开放；
2. 前端不要出现 `VITE_LLM_*`、key、模型地址（`VITE_*` 会打包进浏览器）；
3. 云端端点优先走 HTTPS + Bearer；本机/隧道端点用 `LLM_AUTH_MODE=none`。

## 2. 三种接入方式

### A. 云端 HTTPS 端点（需要 key）

```dotenv
LLM_ENABLED=true
LLM_MODEL_ID=<服务端 /models 返回的 id>
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=<真实 key>
LLM_AUTH_MODE=bearer
LLM_TIMEOUT=120
```

### B. SSH 隧道（推荐给云上 vLLM，端点无需 key）

在**本机**开一个隧道，把远端 8000 映射到本机 8000：

```powershell
ssh -N -L 8000:127.0.0.1:8000 user@your-server
```

然后：

```dotenv
LLM_ENABLED=true
LLM_MODEL_ID=<vLLM 启动时 --served-model-name 的值>
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_API_KEY=
LLM_AUTH_MODE=none
```

### C. 本机 vLLM

```powershell
python -m vllm.entrypoints.openai.api_server `
  --model <本地模型路径或 HF id> `
  --served-model-name qwen2.5-7b-instruct `
  --host 127.0.0.1 --port 8000
```

配置同 B（`LLM_AUTH_MODE=none`）。

## 3. 配置从哪读

后端按顺序查找（先找到先用，进程环境变量优先级最高）：

1. `backend/.env`
2. 仓库根 `.env`

`/api/meta` 的 `config_source` 字段会告诉你实际生效的是哪个文件，避免"改了没生效"。

## 4. 三步验证

```powershell
# 1) 端点本身（免费，不产生 token）
curl.exe http://127.0.0.1:8000/v1/models

# 2) 后端看到的端点（同样不产生 token）
curl.exe http://127.0.0.1:8010/api/llm/health

# 3) 真正跑一次任务（会产生 token）
#    在前端提交一个"实验分析"，报告底部会出现"模型解释"段落
```

`/api/llm/health` 返回示例：

```json
{
  "enabled": true,
  "configured": true,
  "endpoint": "127.0.0.1:8000",
  "auth_mode": "none",
  "model_id": "qwen2.5-7b-instruct",
  "health_path": "/models",
  "reachable": true,
  "status_code": 200,
  "latency_ms": 12.4,
  "models": ["qwen2.5-7b-instruct"],
  "model_present": true,
  "error": null
}
```

前端顶栏的"检测模型连通性"按钮调用的是同一个接口。

## 5. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `configured=false` | `LLM_ENABLED=false`，或 `LLM_BASE_URL`/`LLM_MODEL_ID` 为空，或 bearer 模式下没有 key |
| `reachable=false` + `连接失败` | 隧道没开、端口写错、容器没起；先手动 `curl /v1/models` |
| `reachable=true` + `HTTP 404` | 端点没实现 `/models`（部分网关）；把 `LLM_HEALTH_PATH` 改成 `/health`，或忽略该错误继续用 |
| `model_present=false` | `LLM_MODEL_ID` 与 `/models` 里的 id 不一致（vLLM 用 `--served-model-name`） |
| 任务报 `LLM_UNAVAILABLE` | 端点通了但生成失败：看后端日志里脱敏后的错误；检查模型名、上下文长度、`max_tokens` |
| 报 `LLM_FORMAT_ERROR` 且提示"输出被 max_tokens 截断" | **推理模型**（deepseek-flash、R1 类）的思考 token 也计入 `max_tokens`；提高 `LLM_MAX_OUTPUT_TOKENS`（默认 2048），或缩短输入证据。客户端已会自动翻倍重试一次 |
| 任务报 `BUDGET_EXCEEDED` | 单任务调用次数/提示词超限，调 `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_PROMPT_CHARS` |
| 报告只有数字、没有解释 | 模型不可达或未配置时按设计降级（`degraded=true`），确定性结果仍然完整 |

## 6. 为什么前端不放模型、监测也不再部署一个模型

- **前端**：浏览器里任何 key 都是公开的；直连模型端口还会绕过预算与审计。让后端做唯一出口；
- **监测**：`/api/metrics` 是纯 SQL 的确定性指标（任务数、队列深度、模型是否已配置），`/api/llm/health` 是免费探测。不需要第二个 LLM；
- **告警解释**：如果以后要"让模型解释监控异常"，复用同一个 `LLM_*` 端点，在告警触发后调用一次并单独计预算，不要新增第二套模型配置。
