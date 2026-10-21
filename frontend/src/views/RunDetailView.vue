<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { approveRun, artifactUrl, cancelRun, getRun, type RunEvaluation, type RunInfo } from "../api/client";
import { useRunStream } from "../composables/useRunStream";
import { reportPath, runPath } from "../navigation";

const props = defineProps<{ id: string }>();

const run = ref<RunInfo | null>(null);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const approvalParams = ref<Record<string, number>>({});
const now = ref(Date.now());
const stream = useRunStream(props.id);

let timer: number | null = null;
let clock: number | null = null;

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const isTerminal = computed(() => (run.value ? TERMINAL.has(run.value.status) : false));
const entries = computed(() => Object.entries(run.value?.pending_approval?.params ?? {}));
const calls = computed(() => run.value?.llm_calls ?? []);
const nodeTimings = computed(() => run.value?.node_timings ?? []);
const maxNodeMs = computed(() => Math.max(1, ...nodeTimings.value.map((item) => item.duration_ms)));

const summaryRows = computed(() => {
  const summary = run.value?.result_summary;
  if (!summary) return [];
  return Object.entries(summary)
    .filter(([key]) => key !== "evaluation")
    .map(([key, value]) => ({
      key,
      value: typeof value === "object" ? JSON.stringify(value) : String(value)
    }));
});

const evaluation = computed<RunEvaluation | null>(() => {
  const raw = run.value?.result_summary?.evaluation;
  return raw ? (raw as unknown as RunEvaluation) : null;
});

function evalChipClass(status: string | undefined): string {
  if (status === "pass") return "chip ok";
  if (status === "regression") return "chip warn";
  if (status === "fail") return "chip bad";
  return "chip";
}

function evalLabel(status: string | undefined): string {
  if (status === "pass") return "判据通过";
  if (status === "fail") return "判据未通过";
  if (status === "regression") return "相对基线回归";
  return "不适用";
}

const llmTotals = computed(() => {
  const reduce = (key: "prompt_tokens" | "completion_tokens" | "reasoning_tokens") =>
    calls.value.reduce((total, call) => total + (call[key] ?? 0), 0);
  const last = calls.value[calls.value.length - 1];
  return {
    prompt: reduce("prompt_tokens"),
    completion: reduce("completion_tokens"),
    reasoning: reduce("reasoning_tokens"),
    lastTtft: last?.ttft_first_token_ms ?? null,
    lastContentTtft: last?.ttft_first_content_ms ?? null,
    lastDecode: last?.decode_tokens_per_s ?? null,
    errors: calls.value.filter((call) => call.outcome !== "ok").length
  };
});

const elapsedText = computed(() => {
  if (!run.value) return "—";
  if (run.value.duration_ms !== null) {
    return run.value.duration_ms >= 1000 ? `${(run.value.duration_ms / 1000).toFixed(1)} s` : `${run.value.duration_ms} ms`;
  }
  const base = run.value.started_at ?? run.value.created_at;
  const seconds = Math.max(0, Math.floor((now.value - new Date(base).getTime()) / 1000));
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
});

function statusDot(status: string): string {
  if (status === "succeeded") return "ok";
  if (status === "failed" || status === "interrupted") return "bad";
  if (status === "awaiting_approval") return "warn";
  return "info";
}

function statusChip(status: string): string {
  if (status === "succeeded") return "chip ok";
  if (status === "failed" || status === "interrupted") return "chip bad";
  if (status === "awaiting_approval") return "chip warn";
  return "chip info";
}

function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function fmtNum(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "—";
  return digits > 0 ? value.toFixed(digits) : Math.round(value).toLocaleString();
}

function barWidth(ms: number): string {
  return `${Math.max(4, Math.round((ms / maxNodeMs.value) * 100))}%`;
}

async function refresh(): Promise<void> {
  try {
    const payload = await getRun(props.id);
    run.value = payload;
    if (payload.pending_approval) {
      approvalParams.value = { ...payload.pending_approval.params };
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function approve(): Promise<void> {
  if (!run.value?.pending_approval) {
    return;
  }
  error.value = null;
  notice.value = null;
  try {
    await approveRun(props.id, run.value.pending_approval.approval_id, approvalParams.value);
    notice.value = "审批已提交，任务重新排队";
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function cancel(): Promise<void> {
  error.value = null;
  try {
    await cancelRun(props.id);
    notice.value = "取消请求已提交";
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

watch(
  () => stream.events.value.length,
  () => {
    void refresh();
  }
);

onMounted(async () => {
  await refresh();
  stream.start();
  timer = window.setInterval(() => {
    if (!isTerminal.value) {
      void refresh();
    }
  }, 2000);
  clock = window.setInterval(() => {
    now.value = Date.now();
  }, 1000);
});

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer);
  if (clock !== null) window.clearInterval(clock);
});
</script>

<template>
  <div class="stack">
    <header class="page-head">
      <div>
        <h1>{{ run?.question ?? "任务详情" }}</h1>
        <p>
          <span class="mono">{{ id }}</span> · {{ run?.task_type ?? "—" }} ·
          {{ run?.created_at ?? "" }}
        </p>
      </div>
      <div class="toolbar">
        <span v-if="run" :class="statusChip(run.status)">
          <span class="dot" :class="[statusDot(run.status), run.status === 'running' ? 'pulse' : '']" />
          {{ run.status }}
        </span>
        <span class="chip">{{ run?.stage ?? "—" }}</span>
        <span v-if="run?.degraded" class="chip warn">degraded</span>
        <button v-if="!isTerminal" class="btn danger" @click="cancel">取消</button>
        <a v-if="run" :href="reportPath(id)"><button class="btn">查看报告</button></a>
        <a href="#/"><button class="btn ghost">返回列表</button></a>
      </div>
    </header>

    <p v-if="error" class="banner banner-error">{{ error }}</p>
    <p v-if="notice" class="banner banner-ok">{{ notice }}</p>

    <div v-if="run" class="kpis">
      <div class="kpi">
        <span class="kpi-label">总耗时</span>
        <span class="kpi-value">{{ elapsedText }}</span>
        <span class="kpi-sub">排队 {{ fmtMs(run.queue_wait_ms) }}</span>
      </div>
      <div class="kpi">
        <span class="kpi-label">首 token / 首正文</span>
        <span class="kpi-value">{{ fmtMs(llmTotals.lastTtft) }}</span>
        <span class="kpi-sub">首正文 {{ fmtMs(llmTotals.lastContentTtft) }}</span>
      </div>
      <div class="kpi">
        <span class="kpi-label">decode 速率</span>
        <span class="kpi-value">{{ fmtNum(llmTotals.lastDecode, 1) }} <small>tok/s</small></span>
        <span class="kpi-sub">{{ calls.length }} 次调用 · {{ llmTotals.errors }} 次异常</span>
      </div>
      <div class="kpi">
        <span class="kpi-label">Token（prompt / completion）</span>
        <span class="kpi-value">{{ fmtNum(llmTotals.prompt) }} / {{ fmtNum(llmTotals.completion) }}</span>
        <span class="kpi-sub">reasoning {{ fmtNum(llmTotals.reasoning) }}</span>
      </div>
    </div>

    <div v-if="run?.error" class="banner banner-error">
      <strong>{{ run.error.code }}</strong>：{{ run.error.message }}
      <pre v-if="run.error.details" class="code" style="margin-top: 8px">{{ JSON.stringify(run.error.details, null, 2) }}</pre>
    </div>

    <section v-if="run?.pending_approval" class="panel">
      <div class="panel-head">
        <h2>待人工确认</h2>
        <span class="chip warn"><span class="dot warn" />awaiting_approval</span>
      </div>
      <p class="muted" style="margin: 0 0 10px">
        只有 registry 中登记的参数会被执行；修改任何参数都会导致审批哈希不匹配。
      </p>
      <p v-if="run.pending_approval.entry" class="faint" style="margin: 0 0 10px">
        {{ run.pending_approval.entry.title }} · {{ run.pending_approval.entry.description }}
      </p>
      <div class="split-wide">
        <div class="field" v-for="[name] in entries" :key="name">
          <span>{{ name }}</span>
          <input v-model.number="approvalParams[name]" type="number" />
        </div>
      </div>
      <p class="faint mono" style="font-size: 11px">approval_id：{{ run.pending_approval.approval_id.slice(0, 24) }}…</p>
      <button class="btn primary" @click="approve">确认并执行</button>
    </section>

    <div v-if="run?.warnings?.length" class="banner banner-warn">
      <div v-for="warning in run.warnings" :key="warning">- {{ warning }}</div>
    </div>

    <div class="split-wide">
      <section class="panel">
        <div class="panel-head">
          <h2>阶段耗时</h2>
          <span class="hint">{{ nodeTimings.length }} 个节点</span>
        </div>
        <div v-if="nodeTimings.length" class="stepper">
          <div v-for="timing in nodeTimings" :key="timing.id" class="step">
            <span class="step-name" :title="timing.node">{{ timing.node }}</span>
            <div class="step-track">
              <div
                class="step-fill"
                :class="{ failed: timing.status !== 'ok' }"
                :style="{ width: barWidth(timing.duration_ms) }"
              />
            </div>
            <span class="step-ms">{{ fmtMs(timing.duration_ms) }}</span>
          </div>
        </div>
        <p v-else class="empty">没有节点耗时记录</p>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>结果摘要</h2>
          <span class="hint">持久化在 runs.result_summary</span>
        </div>
        <div v-if="summaryRows.length" class="bars">
          <div v-for="row in summaryRows" :key="row.key" class="bar-row" style="grid-template-columns: 150px minmax(0, 1fr)">
            <span class="faint">{{ row.key }}</span>
            <span class="mono" style="grid-column: span 2">{{ row.value }}</span>
          </div>
        </div>
        <p v-else class="empty">任务结束后生成摘要</p>
      </section>
    </div>

    <section v-if="evaluation" class="panel">
      <div class="panel-head">
        <h2>判据与结论</h2>
        <span :class="evalChipClass(evaluation.status)">{{ evalLabel(evaluation.status) }}</span>
      </div>
      <p class="muted" style="margin: 0 0 10px">{{ evaluation.reason }}</p>

      <div v-if="evaluation.checks.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>检查项</th>
              <th class="num">阈值</th>
              <th class="num">实测</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="check in evaluation.checks" :key="check.name">
              <td>{{ check.label }}</td>
              <td class="num muted">{{ check.threshold }}{{ check.unit }}</td>
              <td class="num">{{ check.actual === null ? "—" : check.actual.toFixed(2) + check.unit }}</td>
              <td>
                <span :class="check.passed ? 'chip ok' : 'chip bad'">{{ check.passed ? "通过" : "未通过" }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="evaluation.baseline" class="faint" style="margin: 10px 0 0">
        基线
        <a :href="runPath(evaluation.baseline.run_id)">{{ evaluation.baseline.run_id }}</a>
        · 吞吐 {{ evaluation.baseline.throughput_pct ?? "—" }}% · TTFT 降低
        {{ evaluation.baseline.ttft_p50_reduction_pct ?? "—" }}%
      </p>

      <div v-if="evaluation.regression" class="banner banner-warn" style="margin: 12px 0 0">
        <div v-for="item in evaluation.regression.details" :key="item.metric">
          {{ item.label }}：{{ item.baseline }}% → {{ item.current }}%（{{ item.delta }}%，容忍
          {{ evaluation.regression.tolerance_pct }}%）
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>模型调用</h2>
        <span class="hint">TTFT 为客户端口径（含网络与排队）；decode 含思考 token</span>
      </div>
      <div v-if="calls.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>节点</th>
              <th class="num">#</th>
              <th class="num">首 token</th>
              <th class="num">首正文</th>
              <th class="num">decode</th>
              <th class="num">prompt</th>
              <th class="num">completion</th>
              <th class="num">reasoning</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="call in calls" :key="call.id">
              <td>{{ call.node }}</td>
              <td class="num muted">{{ call.attempt }}</td>
              <td class="num">{{ fmtMs(call.ttft_first_token_ms) }}</td>
              <td class="num">{{ fmtMs(call.ttft_first_content_ms) }}</td>
              <td class="num">{{ fmtNum(call.decode_tokens_per_s, 1) }} tok/s</td>
              <td class="num muted">{{ fmtNum(call.prompt_tokens) }}</td>
              <td class="num muted">{{ fmtNum(call.completion_tokens) }}</td>
              <td class="num muted">{{ fmtNum(call.reasoning_tokens) }}</td>
              <td>
                <span :class="call.outcome === 'ok' ? 'chip ok' : 'chip warn'">{{ call.outcome }}</span>
                <span v-if="!call.streamed" class="tag" style="margin-left: 5px">非流式</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">该任务没有模型调用（离线模式或受控验证）</p>
    </section>

    <section v-if="run?.server_metrics" class="panel">
      <div class="panel-head">
        <h2>服务端指标（vLLM /metrics）</h2>
        <span class="hint">状态：{{ run.server_metrics.status }}</span>
      </div>
      <div v-if="run.server_metrics.counters && Object.keys(run.server_metrics.counters).length" class="table-wrap">
        <table>
          <tbody>
            <tr v-for="(value, key) in run.server_metrics.counters" :key="key">
              <td class="muted mono">{{ key }}</td>
              <td class="num">{{ value }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">服务端未提供可用指标</p>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>产物</h2>
        <span class="hint">{{ run?.artifacts?.length ?? 0 }} 个文件</span>
      </div>
      <div v-if="run?.artifacts?.length" class="artifact-grid">
        <div v-for="artifact in run.artifacts" :key="artifact.artifact_id" class="artifact">
          <span class="tag">{{ artifact.kind }}</span>
          <span class="name" :title="artifact.filename">{{ artifact.filename }}</span>
          <span class="faint">{{ artifact.size }} B</span>
          <a :href="artifactUrl(artifact)" target="_blank" rel="noopener">下载 →</a>
        </div>
      </div>
      <p v-else class="empty">任务尚未产出文件</p>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>事件流</h2>
        <span class="hint">SSE · {{ stream.events.value.length }} 条</span>
      </div>
      <div class="event-log">
        <div v-for="(event, index) in stream.events.value" :key="index">
          #{{ event.seq ?? "-" }} {{ event.type }} {{ JSON.stringify(event.payload).slice(0, 140) }}
        </div>
      </div>
      <p v-if="!stream.events.value.length" class="empty">等待事件…</p>
    </section>
  </div>
</template>
