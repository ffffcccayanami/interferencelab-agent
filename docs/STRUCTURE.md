# 项目结构与模块职责

> 面向"读代码/改代码"的地图。README 讲怎么用，本文件讲东西在哪、彼此怎么连。

## 1. 顶层目录

```text
inferencelab-agent/
├─ backend/              FastAPI 后端（Python 3.12+，uv 管理依赖）
│  ├─ app/
│  │  ├─ main.py         应用工厂：路由、静态挂载 /app、lifespan、异常处理
│  │  ├─ settings.py     配置（.env 双路径、provider 预设解析、启动校验）
│  │  ├─ schemas.py      Pydantic 请求/响应模型与错误码
│  │  ├─ jobqueue.py     有界队列 + N worker + GPU/CPU 并发闸门
│  │  ├─ worker.py       取任务 → 建图状态 → 执行 → 落库/发事件
│  │  ├─ deps.py         任务级依赖：run 目录、LLM 客户端、节点计时
│  │  ├─ events.py       事件落库 + SSE 序列化（?after= 续传）
│  │  ├─ db/store.py     SQLite（WAL）访问层，无 ORM，含幂等迁移
│  │  ├─ api/            HTTP 路由：runs / uploads / meta / monitor
│  │  ├─ graph/          LangGraph 主图 + 三类子图（analysis/research/validation）
│  │  ├─ services/       纯功能模块（见第 3 节）
│  │  └─ validation/     受控验证 registry + 固定脚本 runner
│  ├─ tests/             128 个离线用例（不消耗 token）
│  └─ pyproject.toml     ruff / pytest / 依赖声明
├─ frontend/             Vue 3 + TS + Vite（无路由库、无图表库）
│  └─ src/
│     ├─ App.vue         侧边栏布局 + hash 路由分发
│     ├─ api/client.ts   后端类型与 fetch 封装
│     ├─ navigation.ts   #/、#/runs、#/runs/:id、#/monitor 解析
│     ├─ views/          任务页 / 全部任务 / 详情 / 报告 / 监控
│     ├─ components/     LineChart（纯 SVG 折线）
│     ├─ composables/    useRunStream（手写 SSE 读取）
│     └─ style.css       浅色主题设计令牌与组件样式
├─ docs/                 计划、架构、端点、监控、安全、评测、提供商
├─ fixtures/             离线演示数据（benchmark / Prometheus / 文档）
├─ scripts/              doctor / env_template / secret_scan / hooks / 演示数据
├─ .githooks/            pre-commit、pre-push（密钥扫描 + 测试）
└─ .github/workflows/    CI（测试/构建）与 security（gitleaks）
```

## 2. 请求生命周期

```text
POST /api/uploads        → files.py 校验类型/大小，落 uploads/，记录 sha256
POST /api/runs           → 校验参数 → queue.submit(run_id, run_class)
                           analysis→gpu 闸门，research/validation→cpu 闸门
worker（N 个）            → 拿闸门 → asyncio.to_thread(run_run)
  └─ main_graph           → validate → route → 子图 → explain → persist
       ├─ analysis        parse → compute（确定性指标）→ persist
       ├─ research        retrieve（BM25）→ draft → verify（引用存在性）
       └─ validation      等审批 → runner（registry 白名单参数）
  └─ persist             写报告/结果 JSON/PDF、判据与基线、节点耗时
GET /api/runs/:id/events → SSE，先落库再推送，支持 Last-Event-ID/after
```

## 3. backend/app/services 职责

| 模块 | 职责 | 是否调用模型 |
|---|---|---|
| `metrics.py` | 分组、吞吐、TTFT 分位、对照差值（唯一数值来源） | 否 |
| `parsing.py` | benchmark JSON/CSV、Prometheus 文本解析 | 否 |
| `retrieval.py` | 本地 BM25 + 证据片段（含截断与来源标记） | 否 |
| `reporting.py` | Markdown 报告：指标表、对照、判据、解释、边界 | 否 |
| `evaluation.py` | 判据判定、benchmark 键、基线查找、回归检测 | 否 |
| `pdf_export.py` | Markdown → PDF（reportlab） | 否 |
| `artifacts.py` / `files.py` | 产物登记、上传落盘与校验 | 否 |
| `llm.py` | 模型客户端：OpenAI 兼容 / Anthropic / Gemini、预算、重试、指标 | 是 |
| `providers.py` | 部署预设与部署形态推导 | 否 |
| `llm_metrics.py` | `llm_calls` 落库 + `llm_call` 事件 | 否 |
| `monitor.py` | 窗口聚合、趋势分桶、Prometheus 文本 | 否 |
| `server_metrics.py` | 可选 vLLM `/metrics` 快照与 diff | 否 |

## 4. 数据表（SQLite）

| 表 | 内容 |
|---|---|
| `uploads` | 上传文件元数据 + sha256 |
| `runs` | 任务状态、阶段、耗时、队列等待、`eval_status`、`benchmark_key`、`baseline_run_id`、结果摘要 |
| `run_events` | 事件流（seq 递增，SSE 续传依据） |
| `artifacts` | 报告/PDF/结果 JSON 等产物 |
| `llm_calls` | 每次模型调用：TTFT、decode、tokens、outcome |
| `node_timings` | 每个图节点的状态与耗时 |
| `llm_metrics_snapshots` | 服务端 `/metrics` 快照（可选） |

迁移策略：`store._migrate_run_columns()` 对老库做 `ALTER TABLE ADD COLUMN`，幂等。

## 5. 前端的几个约定

- **无 vue-router**：`navigation.ts` 解析 hash，`App.vue` 按 `route.name` 切视图；
- **无图表库**：`LineChart.vue` 直接画 SVG（网格、双系列、悬停数值）；
- **无状态库**：视图内 `ref` + 轮询；运行详情用 `useRunStream` 收 SSE 后再拉一次详情；
- 颜色语义统一走 `style.css` 的 CSS 变量（`--ok/--warn/--bad/--brand`）。

## 6. 常见改动入口

| 想做什么 | 改哪里 |
|---|---|
| 新增任务类型 | `graph/subgraphs/` + `main_graph.route` + `schemas.TaskType` |
| 新增部署预设 | `services/providers.py` 的 `PROFILES` |
| 新增判据指标 | `services/evaluation.py` 的 `evaluate_metrics` + 报告 `_evaluation_section` |
| 新增监控图表 | `services/monitor.py` 聚合 + `views/MonitorView.vue` |
| 新增前端页面 | `views/` + `navigation.ts` + `App.vue` |
| 改阈值/并发 | `.env`（`INFL_EVAL_*`、`INFL_WORKERS`、`INFL_*_CONCURRENCY`） |

## 7. 质量门

```powershell
cd backend
uv run ruff check .
uv run python -m pytest -q          # 128 passed（离线）

cd ..\frontend
npm run typecheck
npm run build
```

提交前钩子会跑 `secret_scan.py --staged`；CI 额外跑 gitleaks。
