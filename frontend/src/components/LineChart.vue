<script setup lang="ts">
import { computed } from "vue";

export interface TrendSeries {
  label: string;
  color: string;
  values: (number | null)[];
}

const props = withDefaults(
  defineProps<{
    labels: string[];
    series: TrendSeries[];
    height?: number;
    unit?: string;
    digits?: number;
  }>(),
  { height: 180, unit: "", digits: 0 }
);

const width = 760;
const pad = { top: 14, right: 14, bottom: 26, left: 46 };
const innerW = width - pad.left - pad.right;
const innerH = computed(() => props.height - pad.top - pad.bottom);

const allValues = computed(() =>
  props.series.flatMap((item) => item.values.filter((value): value is number => value !== null && !Number.isNaN(value)))
);
const maxValue = computed(() => Math.max(1, ...allValues.value));
const hasData = computed(() => allValues.value.length > 0);

function xAt(index: number): number {
  const count = Math.max(1, props.labels.length - 1);
  return pad.left + (index / count) * innerW;
}

function yAt(value: number): number {
  return pad.top + (1 - value / maxValue.value) * innerH.value;
}

interface Dot {
  cx: number;
  cy: number;
  title: string;
}

const segments = computed(() =>
  props.series.map((item) => {
    const paths: string[] = [];
    const dots: Dot[] = [];
    let current: string[] = [];

    item.values.forEach((value, index) => {
      if (value === null || Number.isNaN(value)) {
        if (current.length > 1) paths.push(current.join(" "));
        current = [];
        return;
      }
      const cx = xAt(index);
      const cy = yAt(value);
      current.push(`${current.length ? "L" : "M"}${cx.toFixed(1)},${cy.toFixed(1)}`);
      dots.push({
        cx,
        cy,
        title: `${props.labels[index] ?? ""} · ${item.label}: ${value.toFixed(props.digits)}${props.unit}`
      });
    });
    if (current.length > 1) paths.push(current.join(" "));
    return { label: item.label, color: item.color, paths, dots };
  })
);

const gridLines = computed(() =>
  [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    y: pad.top + ratio * innerH.value,
    value: maxValue.value * (1 - ratio)
  }))
);

const xLabels = computed(() => {
  const count = props.labels.length;
  if (!count) return [];
  const indexes = Array.from(new Set([0, Math.floor((count - 1) / 2), count - 1]));
  return indexes.map((index) => ({ x: xAt(index), text: props.labels[index] ?? "" }));
});
</script>

<template>
  <div class="chart">
    <div class="chart-legend">
      <span v-for="item in series" :key="item.label" class="legend-item">
        <i :style="{ background: item.color }" />{{ item.label }}
      </span>
    </div>

    <svg v-if="hasData" :viewBox="`0 0 ${width} ${height}`" class="chart-svg" role="img">
      <g v-for="line in gridLines" :key="line.y">
        <line :x1="pad.left" :x2="width - pad.right" :y1="line.y" :y2="line.y" class="grid" />
        <text :x="pad.left - 8" :y="line.y + 4" text-anchor="end" class="axis">
          {{ line.value.toFixed(digits) }}
        </text>
      </g>

      <text v-for="label in xLabels" :key="label.x" :x="label.x" :y="height - 8" text-anchor="middle" class="axis">
        {{ label.text }}
      </text>

      <g v-for="segment in segments" :key="segment.label">
        <path
          v-for="(path, index) in segment.paths"
          :key="index"
          :d="path"
          :stroke="segment.color"
          fill="none"
          stroke-width="2"
          stroke-linejoin="round"
          stroke-linecap="round"
        />
        <circle v-for="(dot, index) in segment.dots" :key="index" :cx="dot.cx" :cy="dot.cy" r="2.4" :fill="segment.color">
          <title>{{ dot.title }}</title>
        </circle>
      </g>
    </svg>
    <p v-else class="chart-empty">窗口内还没有数据</p>
  </div>
</template>
