# 厂商 API 与本地/云端部署

P3 之后，同一套任务与观测代码可以接多种模型来源，**切换只改配置**。

## 1. 支持矩阵

| 传输 | 覆盖 | 鉴权 | 流式 / TTFT |
|---|---|---|---|
| `openai`（OpenAI 兼容） | DeepSeek、OpenAI、通义（DashScope 兼容模式）、vLLM、Ollama、LM Studio、OpenRouter 等 | Bearer 或 无 | ✅ / ✅ |
| `anthropic` | Anthropic Messages API（Claude） | `x-api-key` + `anthropic-version` | ✅ / ✅ |
| `gemini` | Google Gemini `generateContent` | `key` 查询参数 | ❌ / ❌（记总耗时与 token） |

## 2. 部署预设

`LLM_PROFILE` 提供默认值，**显式变量永远覆盖**：

| 预设 | base_url | 鉴权 | 部署 |
|---|---|---|---|
| `deepseek` | `https://api.deepseek.com` | bearer | cloud |
| `openai` | `https://api.openai.com/v1` | bearer | cloud |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | bearer | cloud |
| `vllm` | `http://127.0.0.1:8000/v1` | none | local |
| `ollama` | `http://127.0.0.1:11434/v1` | none | local |
| `lmstudio` | `http://127.0.0.1:1234/v1` | none | local |
| `anthropic` | `https://api.anthropic.com` | x-api-key | cloud |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | query | cloud |

`LLM_DEPLOYMENT`：`auto`（按 base_url 推导）/ `local` / `tunnel` / `cloud`，只用于界面展示。

## 3. 快速配置

### 3.1 DeepSeek（厂商 API）

```dotenv
LLM_PROFILE=deepseek
LLM_MODEL_ID=deepseek-chat
LLM_API_KEY=sk-xxxx
```

### 3.2 本地 / 隧道 vLLM

```dotenv
LLM_PROFILE=vllm
LLM_MODEL_ID=qwen2.5-7b-instruct   # 与 --served-model-name 一致
# 无需 key；如需访问远端隧道，仍写 127.0.0.1:8000
```

### 3.3 Anthropic Claude

```dotenv
LLM_PROFILE=anthropic
LLM_MODEL_ID=claude-3-5-sonnet-latest
LLM_API_KEY=sk-ant-xxxx
```

### 3.4 Google Gemini

```dotenv
LLM_PROFILE=gemini
LLM_MODEL_ID=gemini-2.0-flash
LLM_API_KEY=AIza-xxxx
```

## 4. 配置优先级

1. 显式 `LLM_BASE_URL` / `LLM_AUTH_MODE` / `LLM_HEALTH_PATH` / `LLM_MODEL_ID`；
2. `LLM_PROFILE` 预设的默认值；
3. 内置缺省（`/models`、`bearer`、`openai` 传输）。

`LLM_PROVIDER` 若与预设的传输冲突（例如 `LLM_PROFILE=deepseek` + `LLM_PROVIDER=anthropic`），启动时直接报配置错误。

> 注意：填了 `LLM_PROFILE` 时，`LLM_BASE_URL` 留空才会使用预设；如果你把 `.env.example` 原样复制（里面写死了 `http://127.0.0.1:8000/v1`），会覆盖预设。示例文件里该行仅作占位。

## 5. 观测一致性

三种传输写同一张 `llm_calls` 表：

- OpenAI 兼容 / Anthropic：`streamed=true`，记录首 token、首正文、decode 速率；
- Gemini：`streamed=false`，`ttft_*` 为 `null`，只记总耗时与 token（**不估算**）；
- `/api/llm/health` 按传输选择探测：OpenAI `GET /models`、Anthropic `GET /v1/models`（带版本头）、Gemini `GET /models?key=…`；
- 返回里包含 `provider / profile / deployment / streaming_supported`，前端侧边栏同步展示。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 启动报 `LLM_PROVIDER` 与 profile 冲突 | 删掉其中一个，或改成一致 |
| Anthropic 401 | 确认用的是 `x-api-key`（不是 Bearer），且账号有该模型权限 |
| Gemini 404 model | `LLM_MODEL_ID` 不带 `models/` 前缀，例如 `gemini-2.0-flash` |
| 本地 vLLM 连不上 | 先 `curl http://127.0.0.1:8000/v1/models`，再 `python scripts/doctor.py` |
| 想接私有网关但复用预设 | 保留 `LLM_PROFILE=deepseek`，显式写 `LLM_BASE_URL` 覆盖 |
