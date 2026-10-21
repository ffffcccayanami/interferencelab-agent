<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";

import { listRuns, type RunInfo } from "../api/client";
import { runPath } from "../navigation";

const runs = ref<RunInfo[]>([]);
const error = ref<string | null>(null);
const loading = ref(true);

let timer: number | null = null;

function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
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
  if (status === "pass") return "通过";
  if (status === "fail") return "未过";
  if (status === "regression") return "回归";
  return "";
}

async function refresh(): Promise<void> {
  try {
    const payload = await listRuns(100);
    runs.value = payload.runs;
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
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
        <h1>全部任务</h1>
        <p>最近 {{ runs.length }} 条 · 每 5s 刷新</p>
      </div>
      <div class="toolbar">
        <a href="#/"><button class="btn ghost">返回概览</button></a>
        <button class="btn" @click="refresh">刷新</button>
      </div>
    </header>

    <p v-if="error" class="banner banner-error">{{ error }}</p>

    <section class="panel">
      <p v-if="loading" class="muted">加载中…</p>
      <div v-else-if="runs.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>任务</th>
              <th>类型</th>
              <th>状态</th>
              <th class="num">耗时</th>
              <th class="num">模型</th>
              <th class="num">产物</th>
              <th>创建</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="run in runs" :key="run.run_id">
              <td style="max-width: 420px">
                <a :href="runPath(run.run_id)">{{ run.question }}</a>
              </td>
              <td class="muted">{{ run.task_type }}</td>
              <td>
                <span :class="statusChip(run.status)">{{ run.status }}</span>
                <span
                  v-if="run.eval_status && run.eval_status !== 'not_applicable'"
                  :class="evalChip(run.eval_status)"
                  style="margin-left: 5px"
                >{{ evalLabel(run.eval_status) }}</span>
              </td>
              <td class="num muted">{{ fmtMs(run.duration_ms) }}</td>
              <td class="num muted">{{ run.llm_calls_count ?? 0 }}</td>
              <td class="num muted">{{ run.artifact_count }}</td>
              <td class="muted" style="white-space: nowrap">{{ run.created_at.slice(5, 16).replace("T", " ") }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">还没有任务</p>
    </section>
  </div>
</template>
