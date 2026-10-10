<script setup lang="ts">
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref } from 'vue'

const kpis = [
  { label: '文档总量', value: '30', unit: '篇', tone: 'brand' },
  { label: '切片总量', value: '193万', unit: '片', tone: 'brand' },
  { label: '今日检索', value: '1,284', unit: '次', tone: '' },
  { label: '检索准确率', value: '92.4', unit: '%', tone: 'good' },
  { label: '幻觉率', value: '1.8', unit: '%', tone: 'warn' },
  { label: '格式合规率', value: '98.1', unit: '%', tone: 'good' },
]

const trendEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
onMounted(() => {
  if (!trendEl.value) return
  chart = echarts.init(trendEl.value)
  chart.setOption({
    grid: { left: 36, right: 16, top: 20, bottom: 24 },
    xAxis: { type: 'category', data: ['周一', '周二', '周三', '周四', '周五', '周六', '周日'],
      axisLine: { lineStyle: { color: '#3a4a66' } }, axisLabel: { color: '#8a99b3' } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } },
      axisLabel: { color: '#8a99b3' } },
    series: [{
      type: 'line', smooth: true, data: [820, 932, 901, 1290, 1330, 620, 710],
      lineStyle: { color: '#3d7eff' }, itemStyle: { color: '#3d7eff' },
      areaStyle: { color: 'rgba(61,126,255,.18)' },
    }],
  })
})
onBeforeUnmount(() => chart?.dispose())

const health = [
  { name: 'PostgreSQL + pgvector', status: '正常', tone: 'good' },
  { name: '对象存储 (LocalFS)', status: '正常', tone: 'good' },
  { name: 'Qwen3-32B 推理(vLLM)', status: '运行中', tone: 'good' },
  { name: 'Embedding-4B', status: '就绪', tone: 'good' },
  { name: 'Reranker-0.6B', status: '就绪', tone: 'good' },
]
</script>

<template>
  <div class="grid">
    <div v-for="k in kpis" :key="k.label" class="panel card">
      <div class="label text-muted">{{ k.label }}</div>
      <div class="value" :class="k.tone">{{ k.value }}<small>{{ k.unit }}</small></div>
    </div>
  </div>

  <div class="row">
    <div class="panel chart">
      <div class="ttl">近 7 日检索量趋势</div>
      <div ref="trendEl" class="chart-box"></div>
    </div>
    <div class="panel health">
      <div class="ttl">系统健康</div>
      <div v-for="h in health" :key="h.name" class="h-item">
        <span>{{ h.name }}</span>
        <span class="dot" :class="h.tone" />{{ h.status }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: var(--gap); }
.card { padding: 16px; }
.label { font-size: 13px; }
.value { font-size: 26px; font-weight: 700; color: var(--text-strong); margin-top: 6px; }
.value small { font-size: 13px; color: var(--text-muted); margin-left: 4px; font-weight: 400; }
.value.brand { color: var(--brand); }
.value.good { color: var(--c-success); }
.value.warn { color: var(--c-warning); }
.row { display: grid; grid-template-columns: 2fr 1fr; gap: var(--gap); margin-top: var(--gap); }
.chart, .health { padding: 16px; }
.ttl { color: var(--text-strong); font-weight: 600; margin-bottom: 12px; }
.chart-box { height: 260px; }
.h-item { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--line); color: var(--text); }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.dot.good { background: var(--c-success); }
</style>
