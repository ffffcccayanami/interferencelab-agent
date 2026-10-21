<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { getMonitorSummary, getMonitorTrend, type MonitorSummary, type MonitorTrend, type PercentilePair } from "../api/client";
import LineChart from "../components/LineChart.vue";
import { runPath } from "../navigation";

const summary = ref<MonitorSummary | null>(null);
const trend = ref<MonitorTrend | null>(null);
const hours = ref(24);
const error = ref<string | null>(null);
const loading = ref(true);

let timer: number | null = null;

const duration = computed<PercentilePair | null>(() => summary.value?.runs.duration_ms ?? null);
const ttft = computed<PercentilePair | null>(() => summary.value?.llm.ttft_first_token_ms ?? null);
const contentTtft = computed<PercentilePair | null>(() => summary.value?.llm.ttft_first_content_ms ?? null);
const decode = computed<PercentilePair | null>(() => summary.value?.llm.decode_tokens_per_s ?? null);

const tokenTotal = computed(() => {
  const tokens = summary.value?.llm.tokens;
  if (!tokens) return "—";
  return `${tokens.prompt.toLocaleString()} / ${tokens.completion.toLocaleString()} / ${tokens.reasoning.toLocaleString()}`;
});

const labels = computed(() => (trend.value?.series ?? []).map((point) => point.ts.slice(11, 16)));

const ttftSeries = computed(() => [
  {
    label: "P50",
    color: "#4f6ef7",
    values: (trend.value?.series ?? []).map((point) => point.ttft_p50)
  },
  {
    label: "P95",
    color: "#f59e0b",
    values: (trend.value?.series ?? []).map((point) => point.ttft_p95)
  }
]);

const decodeSeries = computed(() => [
  {
    label: "decode P50",
    color: "#12b5a5",
    values: (trend.value?.series ?? []).map((point) => point.decode_p50)
  }
]);

const volumeSeries = computed(() => [
  {
    label: "任务数",
    color: "#4f6ef7",
    values: (trend.value?.series ?? []).map((point) => point.runs)
  },
  {
    label: "失败数",
    color: "#d92d20",
    values: (trend.value?.series ?? []).map((point) => point.failures)
  }
]);

const durationSeries = computed(() => [
  {
    label: "任务耗时 P50",
    color: "#8b5cf6",
    values: (trend.value?.series ?? []).map((point) => point.duration_p50)
  }
]);

const statusEntries = computed(() => {
  const byStatus = summary.value?.runs.by_status ?? {};
  const max = Math.max(1, ...Object.values(byStatus));
  return Object.entries(byStatus).map(([status, count]) => ({
    status,
    count,
    width: Math.round((count / max) * 100)
  }));
});

const nodeRows = computed(() => {
  const nodes = summary.value?.nodes ?? {};
  const max = Math.max(1, ...Object.values(nodes).map((item) => item.p95_ms ?? 0));
  return Object.entries(nodes).map(([node, item]) => ({
    node,
    calls: item.calls,
    p95: item.p95_ms,
    width: Math.max(4, Math.round(((item.p95_ms ?? 0) / max) * 100))
  }));
});

const failureRows = computed(() => {
  const items = summary.value?.runs.failure_codes ?? [];
  const max = Math.max(1, ...items.map((item) => item.count));
  return items.map((item) => ({ ...item, width: Math.max(6, Math.round((item.count / max) * 100)) }));
});

function fmt(value: number | null | undefined, digits = 0, unit = ""): string {
  if (value === null || value === undefined) return "—";
  const text = digits > 0 ? value.toFixed(digits) : Math.round(value).toLocaleString();
  return unit ? `${text} ${unit}` : text;
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

async function refresh(): Promise<void> {
  try {
    const [nextSummary, nextTrend] = await Promise.all([
      getMonitorSummary(hours.value),
      getMonitorTrend(hours.value, hours.value > 24 ? 48 : 24)
    ]);
    summary.value = nextSummary;
    trend.value = nextTrend;
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function changeWindow(value: number): void {
  hours.value = value;
  void refresh();
}

onMounted(async () => {
  await refresh();
  timer = window.setInterval(() => {
    void refresh();
  }, 5000);
});

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer);
});
</script>

<template>
  <div class="stack">
    <header class="page-head">
      <div>
        <h1>监控</h1>
        <p>
          窗口 {{ summary?.window_hours ?? hours }} 小时 · 生成于 {{ summary?.generated_at ?? "—" }}
          <span v-if="summary?.truncated" class="chip warn">调用数已达上限，统计被截断</span>
        </p>
      </div>
      <div class="toolbar">
        <button class="btn sm" :class="{ primary: hours === 1 }" @click="changeWindow(1)">1h</button>
        <button class="btn sm" :class="{ primary: hours === 24 }" @click="changeWindow(24)">24h</button>
        <button class="btn sm" :class="{ primary: hours === 168 }" @click="changeWindow(168)">7d</button>
        <button class="btn sm" @click="refresh">刷新</button>
      </div>
    </header>

    <p v-if="error" class="banner banner-error">{{ error }}</p>
    <div v-for="alert in summary?.alerts ?? []" :key="alert.code" class="banner banner-error">
      <strong>{{ alert.code }}</strong>：{{ alert.message }}
    </div>

    <div v-if="loading" class="panel"><span class="spinner" /> 加载中…</div>

    <template v-if="summary">
      <div class="kpis">
        <div class="kpi">
          <span class="kpi-label">任务成功率</span>
          <span class="kpi-value">{{ pct(summary.runs.success_rate) }}</span>
          <span class="kpi-sub">{{ summary.runs.finished }} 完成 / 共 {{ summary.runs.total }} · 运行中 {{ summary.runs.by_status.running ?? 0 }}</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">TTFT 首 token P50 / P95</span>
          <span class="kpi-value">{{ fmt(ttft?.p50) }} / {{ fmt(ttft?.p95) }} <small>ms</small></span>
          <span class="kpi-sub">首正文 P50 {{ fmt(contentTtft?.p50) }} ms</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">decode 速率 P50 / P95</span>
          <span class="kpi-value">{{ fmt(decode?.p50, 1) }} / {{ fmt(decode?.p95, 1) }}</span>
          <span class="kpi-sub">tok/s（含思考）· {{ summary.llm.calls }} 次调用</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">Token（prompt / completion / reasoning）</span>
          <span class="kpi-value" style="font-size: 16px">{{ tokenTotal }}</span>
          <span class="kpi-sub">任务耗时 P95 {{ fmt(duration?.p95) }} ms</span>
        </div>
      </div>

      <div class="split-wide">
        <section class="panel">
          <div class="panel-head">
            <h2>TTFT 趋势</h2>
            <span class="hint">毫秒 · 按时间分桶</span>
          </div>
          <LineChart :labels="labels" :series="ttftSeries" :height="170" unit="ms" :digits="0" />
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>decode 速率趋势</h2>
            <span class="hint">token/s · 含思考 token</span>
          </div>
          <LineChart :labels="labels" :series="decodeSeries" :height="170" unit=" tok/s" :digits="0" />
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>任务量与失败</h2>
            <span class="hint">每个时间桶的任务数</span>
          </div>
          <LineChart :labels="labels" :series="volumeSeries" :height="170" :digits="0" />
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>任务耗时趋势</h2>
            <span class="hint">P50 · 毫秒</span>
          </div>
          <LineChart :labels="labels" :series="durationSeries" :height="170" unit="ms" :digits="0" />
        </section>
      </div>

      <section class="panel">
        <div class="panel-head">
          <h2>判据与回归</h2>
          <span class="hint">
            通过率 {{ pct(summary.runs.evaluation.pass_rate) }} ·
            {{ summary.runs.evaluation.evaluated }} 个已判定
          </span>
        </div>
        <div class="row" style="margin-bottom: 10px">
          <span class="chip ok">通过 {{ summary.runs.evaluation.counts.pass ?? 0 }}</span>
          <span class="chip bad">未通过 {{ summary.runs.evaluation.counts.fail ?? 0 }}</span>
          <span class="chip warn">回归 {{ summary.runs.evaluation.counts.regression ?? 0 }}</span>
          <span class="chip">不适用 {{ summary.runs.evaluation.counts.not_applicable ?? 0 }}</span>
        </div>
        <div v-if="summary.runs.evaluation.regressions.length" class="table-wrap">
          <table>
            <thead>
              <tr><th>任务</th><th>判定</th><th>原因</th><th>基线</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in summary.runs.evaluation.regressions" :key="item.run_id">
                <td><a :href="runPath(item.run_id)" class="mono">{{ item.run_id.slice(0, 18) }}</a></td>
                <td>
                  <span :class="item.eval_status === 'fail' ? 'chip bad' : 'chip warn'">{{ item.eval_status }}</span>
                </td>
                <td class="muted">{{ item.reason ?? "—" }}</td>
                <td class="muted">
                  <a v-if="item.baseline_run_id" :href="runPath(item.baseline_run_id)" class="mono">
                    {{ item.baseline_run_id.slice(0, 18) }}
                  </a>
                  <span v-else>—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="empty">窗口内没有未通过或回归的任务</p>
      </section>

      <div class="split-wide">
        <section class="panel">
          <div class="panel-head">
            <h2>节点耗时 P95</h2>
            <span class="hint">来自 node_timings</span>
          </div>
          <div v-if="nodeRows.length" class="bars">
            <div v-for="row in nodeRows" :key="row.node" class="bar-row">
              <span class="muted" :title="row.node">{{ row.node }}</span>
              <div class="bar-track"><div class="bar-fill" :style="{ width: row.width + '%' }" /></div>
              <span class="mono faint">{{ fmt(row.p95) }}ms</span>
            </div>
          </div>
          <p v-else class="empty">还没有节点耗时数据</p>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>状态与失败码</h2>
            <span class="hint">窗口内分布</span>
          </div>
          <div class="bars">
            <div v-for="item in statusEntries" :key="item.status" class="bar-row">
              <span class="muted">{{ item.status }}</span>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :class="{ bad: item.status === 'failed' || item.status === 'interrupted' }"
                  :style="{ width: item.width + '%' }"
                />
              </div>
              <span class="mono faint">{{ item.count }}</span>
            </div>
          </div>
          <div v-if="failureRows.length" class="bars" style="margin-top: 14px">
            <div v-for="row in failureRows" :key="row.code" class="bar-row">
              <span class="muted" :title="row.code">{{ row.code }}</span>
              <div class="bar-track"><div class="bar-fill bad" :style="{ width: row.width + '%' }" /></div>
              <span class="mono faint">{{ row.count }}</span>
            </div>
          </div>
          <p v-else class="empty" style="padding-bottom: 0">窗口内没有失败任务</p>
        </section>
      </div>

      <section class="panel">
        <div class="panel-head">
          <h2>最慢任务</h2>
          <span class="hint">Top {{ summary.runs.slowest.length }}</span>
        </div>
        <div v-if="summary.runs.slowest.length" class="table-wrap">
          <table>
            <thead>
              <tr><th>任务</th><th>类型</th><th>状态</th><th class="num">耗时</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in summary.runs.slowest" :key="item.run_id">
                <td><a :href="runPath(item.run_id)" class="mono">{{ item.run_id.slice(0, 18) }}</a></td>
                <td class="muted">{{ item.task_type }}</td>
                <td><span class="tag">{{ item.status }}</span></td>
                <td class="num">{{ fmt(item.duration_ms) }} ms</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="empty">窗口内没有任务</p>
      </section>
    </template>
  </div>
</template>
