<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { getLLMHealth, getMeta, type LLMHealth, type Meta } from "./api/client";
import { monitorPath, parseHash, type Route } from "./navigation";
import AllRunsView from "./views/AllRunsView.vue";
import MonitorView from "./views/MonitorView.vue";
import ReportView from "./views/ReportView.vue";
import RunDetailView from "./views/RunDetailView.vue";
import TaskListView from "./views/TaskListView.vue";

const meta = ref<Meta | null>(null);
const health = ref<LLMHealth | null>(null);
const probing = ref(false);
const probeError = ref<string | null>(null);
const error = ref<string | null>(null);
const route = ref<Route>(parseHash(window.location.hash));

const runId = computed(() => (route.value.name === "run" ? route.value.id : null));
const reportId = computed(() => (route.value.name === "report" ? route.value.id : null));

const healthLabel = computed(() => {
  if (!health.value) return "";
  if (health.value.reachable) {
    const parts = [`${health.value.latency_ms ?? "?"} ms`];
    if (health.value.models.length) parts.push(`${health.value.models.length} 个模型`);
    if (health.value.model_present === false) parts.push("模型名不匹配");
    return parts.join(" · ");
  }
  return health.value.error ?? "不可达";
});

const healthTone = computed(() => {
  if (!health.value) return "warn";
  if (!health.value.reachable) return "bad";
  return health.value.model_present === false ? "warn" : "ok";
});

function onHashChange(): void {
  route.value = parseHash(window.location.hash);
}

async function checkLLM(): Promise<void> {
  probing.value = true;
  probeError.value = null;
  try {
    health.value = await getLLMHealth();
  } catch (err) {
    probeError.value = err instanceof Error ? err.message : String(err);
  } finally {
    probing.value = false;
  }
}

onMounted(async () => {
  window.addEventListener("hashchange", onHashChange);
  try {
    meta.value = await getMeta();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("hashchange", onHashChange);
});
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">IL</div>
        <div>
          <h1>InferenceLab</h1>
          <p>推理实验工作台</p>
        </div>
      </div>

      <nav class="menu">
        <a href="#/" :class="{ active: route.name !== 'monitor' }">
          <span class="menu-icon">▤</span> 任务
        </a>
        <a :href="monitorPath()" :class="{ active: route.name === 'monitor' }">
          <span class="menu-icon">◔</span> 监控
        </a>
      </nav>

      <div class="side-foot">
        <div class="row">
          <span class="dot" :class="meta?.llm_configured ? 'ok' : 'warn'" />
          <strong>{{ meta?.llm_configured ? meta?.llm_model_id : "离线模式" }}</strong>
        </div>
        <div class="faint">{{ meta?.llm_endpoint ?? "端点未配置" }}</div>
        <div class="faint">
          {{ meta?.llm_provider ?? "-" }} · {{ meta?.llm_deployment ?? "-" }} · {{ meta?.llm_auth_mode ?? "-" }}
        </div>
        <div class="faint" v-if="meta">
          并行 {{ meta.workers }} · GPU {{ meta.gpu_concurrency }} / CPU {{ meta.cpu_concurrency }}
        </div>
        <button class="btn sm" :disabled="probing" @click="checkLLM">
          {{ probing ? "检测中…" : "检测模型连通性" }}
        </button>
        <div v-if="health" class="row">
          <span class="dot" :class="healthTone" />
          <span class="faint">{{ healthLabel }}</span>
        </div>
      </div>
    </aside>

    <main class="main">
      <p v-if="error" class="banner banner-error">
        无法连接后端：{{ error }}（请确认后端已在 127.0.0.1:8010 启动）
      </p>
      <p v-if="probeError" class="banner banner-warn">连通性检测失败：{{ probeError }}</p>

      <TaskListView v-if="route.name === 'tasks'" />
      <AllRunsView v-else-if="route.name === 'all-runs'" />
      <RunDetailView v-else-if="runId" :id="runId" />
      <ReportView v-else-if="reportId" :id="reportId" />
      <MonitorView v-else />

      <footer class="footnote">
        数值由确定性代码计算，模型只负责解释与成文；真实密钥仅存放在本地 .env，永不提交，也不会下发给浏览器。
      </footer>
    </main>
  </div>
</template>
