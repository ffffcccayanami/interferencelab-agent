<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { artifactUrl, getReport, getRun, type RunInfo } from "../api/client";
import { runPath } from "../navigation";

const props = defineProps<{ id: string }>();

const run = ref<RunInfo | null>(null);
const report = ref("");
const error = ref<string | null>(null);
const loading = ref(true);

const pdfArtifact = computed(
  () => run.value?.artifacts.find((artifact) => artifact.kind === "report_pdf") ?? null
);

onMounted(async () => {
  try {
    run.value = await getRun(props.id);
    report.value = await getReport(props.id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="stack">
    <header class="page-head">
      <div>
        <h1>报告</h1>
        <p class="mono">{{ id }}</p>
      </div>
      <div class="toolbar">
        <a :href="runPath(id)"><button class="btn ghost">返回任务</button></a>
        <a v-if="pdfArtifact" :href="artifactUrl(pdfArtifact)" target="_blank" rel="noopener">
          <button class="btn primary">下载 PDF</button>
        </a>
      </div>
    </header>

    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="error" class="banner banner-error">{{ error }}</p>
    <pre v-if="report" class="report">{{ report }}</pre>
    <p v-else-if="!loading && !error" class="empty">报告尚未生成（任务可能失败或仍在运行）</p>
  </div>
</template>
