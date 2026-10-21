<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  createRun,
  getMeta,
  listRuns,
  uploadFile,
  type Meta,
  type RunInfo,
  type ValidationEntry
} from "../api/client";
import { navigate, allRunsPath, runPath } from "../navigation";

const meta = ref<Meta | null>(null);
const runs = ref<RunInfo[]>([]);
const files = ref<File[]>([]);
const taskType = ref<"analysis" | "research" | "validation">("analysis");
const question = ref("");
const selectedEntry = ref("bench_smoke");
const params = ref<Record<string, number>>({});
const busy = ref(false);
const message = ref<string | null>(null);
const error = ref<string | null>(null);
const now = ref(Date.now());

let timer: number | null = null;
let clock: number | null = null;

const entries = computed<ValidationEntry[]>(() => meta.value?.validation_entries ?? []);
const currentEntry = computed<ValidationEntry | null>(
  () => entries.value.find((entry) => entry.entry_id === selectedEntry.value) ?? null
);
const activeRuns = computed(() =>
  runs.value.filter((run) => ["running", "queued", "awaiting_approval"].includes(run.status))
);
const recentRuns = computed(() => runs.value.slice(0, 5));
const runningCount = computed(() => runs.value.filter((run) => run.status === "running").length);
const queuedCount = computed(() => runs.value.filter((run) => run.status === "queued").length);

function syncParams(entry: ValidationEntry | null): void {
  const next: Record<string, number> = {};
  if (entry) {
    for (const [name, spec] of Object.entries(entry.params)) {
      if (spec.type === "int") {
        next[name] = spec.default ?? spec.min ?? 1;
      }
    }
  }
  params.value = next;
}

function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function elapsed(run: RunInfo): string {
  const base = run.started_at ?? run.created_at;
  const ms = Math.max(0, now.value - new Date(base).getTime());
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
}

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

function evalChip(status: string | null | undefined): string {
  if (status === "pass") return "chip ok";
  if (status === "regression") return "chip warn";
  if (status === "fail") return "chip bad";
  return "chip";
}

function evalLabel(status: string | null | undefined): string {
  if (status === "pass") return "判据通过";
  if (status === "fail") return "判据未过";
  if (status === "regression") return "回归";
  return "";
}

function summaryTag(run: RunInfo): string {
  const summary = run.result_summary;
  if (!summary) return "";
  const throughput = summary.throughput_pct;
  if (typeof throughput === "number") return `吞吐 ${throughput > 0 ? "+" : ""}${throughput.toFixed(1)}%`;
  const reduction = summary.ttft_p50_reduction_pct;
  if (typeof reduction === "number") return `TTFT −${reduction.toFixed(1)}%`;
  const citations = summary.citations;
  if (typeof citations === "number") return `引用 ${citations}`;
  const exitCode = summary.exit_code;
  if (typeof exitCode === "number") return `exit ${exitCode}`;
  return "";
}

watch(currentEntry, (entry) => syncParams(entry), { immediate: false });

function onFiles(event: Event): void {
  const input = event.target as HTMLInputElement;
  files.value = input.files ? Array.from(input.files) : [];
}

async function refreshRuns(): Promise<void> {
  try {
    const payload = await listRuns();
    runs.value = payload.runs;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function submit(): Promise<void> {
  error.value = null;
  message.value = null;
  if (!question.value.trim()) {
    error.value = "请填写要分析的问题";
    return;
  }
  if (taskType.value !== "validation" && files.value.length === 0) {
    error.value = "该任务类型至少需要上传一个文件";
    return;
  }

  busy.value = true;
  try {
    const uploadIds: string[] = [];
    for (const file of files.value) {
      const uploaded = await uploadFile(file);
      uploadIds.push(uploaded.upload_id);
    }

    const options: Record<string, unknown> = {};
    if (taskType.value === "validation") {
      options.entry_id = selectedEntry.value;
      options.params = { ...params.value };
    }

    const created = await createRun({
      task_type: taskType.value,
      question: question.value.trim(),
      upload_ids: uploadIds,
      options
    });
    message.value = `已提交，队列位置 ${created.queue_position}`;
    navigate(runPath(created.run_id));
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  try {
    meta.value = await getMeta();
    syncParams(currentEntry.value);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
  await refreshRuns();
  timer = window.setInterval(() => {
    void refreshRuns();
  }, 2500);
  clock = window.setInterval(() => {
    now.value = Date.now();
  }, 1000);
});

onBeforeUnmount(() => {
  if (timer !== null) {
    window.clearInterval(timer);
  }
  if (clock !== null) {
    window.clearInterval(clock);
  }
});
</script>

<template>
  <div class="stack">
    <header class="page-head">
      <div>
        <h1>任务</h1>
        <p>新建实验分析 / 技术研究 / 受控验证，实时查看执行状态与模型指标</p>
      </div>
      <div class="toolbar">
        <span class="chip info"><span class="dot info pulse" />运行中 {{ runningCount }}</span>
        <span class="chip">排队 {{ queuedCount }}</span>
        <span class="chip">队列上限 {{ meta?.queue_size ?? "—" }}</span>
        <span class="chip" v-if="meta">并行 {{ meta.workers }}（GPU {{ meta.gpu_concurrency }} / CPU {{ meta.cpu_concurrency }}）</span>
      </div>
    </header>

    <div class="split">
      <section class="panel">
        <div class="panel-head">
          <h2>新建任务</h2>
          <span class="hint">确定性计算 + 模型解释</span>
        </div>

        <div class="field">
          <span>任务类型</span>
          <select v-model="taskType" aria-label="任务类型">
            <option value="analysis">实验分析（确定性指标 + 对照）</option>
            <option value="research">技术研究（本地证据检索）</option>
            <option value="validation">受控验证（人工确认后执行固定脚本）</option>
          </select>
        </div>

        <div class="field">
          <span>问题</span>
          <textarea
            v-model="question"
            placeholder="例如：对比 baseline 与 apc 两组在并发 4、2.8K 输入、128 输出下的吞吐与 TTFT"
          />
        </div>

        <template v-if="taskType !== 'validation'">
          <div class="field">
            <span>上传文件</span>
            <div class="file-drop">
              支持 benchmark JSON / CSV、Prometheus 文本、.md / .txt / .pdf 文档
              <input type="file" multiple @change="onFiles" />
              <div v-if="files.length" class="faint" style="margin-top: 6px">已选择 {{ files.length }} 个文件</div>
            </div>
          </div>
        </template>

        <template v-else>
          <div class="field">
            <span>验证条目（registry 白名单）</span>
            <select v-model="selectedEntry">
              <option v-for="entry in entries" :key="entry.entry_id" :value="entry.entry_id">
                {{ entry.title }}（{{ entry.entry_id }}）
              </option>
            </select>
            <span v-if="currentEntry" class="faint">{{ currentEntry.description }}</span>
          </div>
          <div v-for="(spec, name) in currentEntry?.params ?? {}" :key="name" class="field">
            <span>{{ name }}（{{ spec.min }} ~ {{ spec.max }}）</span>
            <input v-model.number="params[name]" type="number" :min="spec.min" :max="spec.max" />
          </div>
        </template>

        <p v-if="error" class="banner banner-error" style="margin-top: 14px">{{ error }}</p>
        <p v-if="message" class="banner banner-ok" style="margin-top: 14px">{{ message }}</p>

        <div class="row" style="margin-top: 16px">
          <button class="btn primary" :disabled="busy" @click="submit">
            {{ busy ? "提交中…" : "提交任务" }}
          </button>
          <span v-if="busy" class="faint">上传并排队中</span>
        </div>
      </section>

      <div class="stack">
        <section class="panel">
          <div class="panel-head">
            <h2>运行中</h2>
            <span class="hint">{{ activeRuns.length }} 个活动任务 · 每 2.5s 刷新</span>
          </div>
          <div v-if="activeRuns.length" class="run-list">
            <a v-for="run in activeRuns" :key="run.run_id" class="run-item" :href="runPath(run.run_id)">
              <span class="dot" :class="[statusDot(run.status), run.status === 'running' ? 'pulse' : '']" />
              <div style="min-width: 0">
                <div class="title">{{ run.question }}</div>
                <div class="meta">
                  <span>{{ run.task_type }}</span>
                  <span>{{ run.stage }}</span>
                  <span>{{ elapsed(run) }}</span>
                  <span v-if="run.llm_calls_count">{{ run.llm_calls_count }} 次模型调用</span>
                </div>
              </div>
              <span :class="statusChip(run.status)">{{ run.status }}</span>
            </a>
          </div>
          <p v-else class="empty">当前没有运行中的任务</p>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>最近任务</h2>
            <div class="row">
              <span class="hint">{{ runs.length }} 条记录</span>
              <a :href="allRunsPath()"><button class="btn sm">查看全部 →</button></a>
            </div>
          </div>

          <div v-if="recentRuns.length" class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>状态</th>
                  <th class="num">耗时</th>
                  <th class="num">模型</th>
                  <th class="num">产物</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="run in recentRuns" :key="run.run_id">
                  <td style="max-width: 340px">
                    <a :href="runPath(run.run_id)">{{ run.question }}</a>
                    <div class="row" style="margin-top: 3px; gap: 5px">
                      <span class="tag">{{ run.task_type }}</span>
                      <span v-if="run.degraded" class="tag">degraded</span>
                      <span v-if="run.error" class="tag" style="color: var(--bad)">{{ run.error.code }}</span>
                      <span v-if="summaryTag(run)" class="tag">{{ summaryTag(run) }}</span>
                      <span
                        v-if="run.eval_status && run.eval_status !== 'not_applicable'"
                        :class="evalChip(run.eval_status)"
                      >{{ evalLabel(run.eval_status) }}</span>
                    </div>
                  </td>
                  <td>
                    <span :class="statusChip(run.status)">
                      <span class="dot" :class="statusDot(run.status)" />{{ run.status }}
                    </span>
                  </td>
                  <td class="num muted">{{ fmtMs(run.duration_ms) }}</td>
                  <td class="num muted">{{ run.llm_calls_count ?? 0 }}</td>
                  <td class="num muted">{{ run.artifact_count }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="empty">还没有任务</p>
          <p v-if="runs.length > recentRuns.length" class="faint" style="text-align: center; font-size: 12px">
            仅显示最近 {{ recentRuns.length }} 条，<a :href="allRunsPath()">查看全部任务</a>
          </p>
        </section>
      </div>
    </div>
  </div>
</template>
