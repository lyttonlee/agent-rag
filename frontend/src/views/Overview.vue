<script setup lang="ts">
import { onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { getStats, type DocStats } from '@/api/client'

const stats = ref<DocStats | null>(null)
const healthOk = ref(false)
const kpis = ref<{ label: string; value: string; unit: string; tone: string }[]>([])

const trendEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

onMounted(async () => {
  try {
    const s = await getStats()
    stats.value = s
    const bySec = s.by_security || {}
    const indexed = (s.by_status || {}).INDEXED || 0
    const doneRate = s.total_documents ? Math.round((indexed / s.total_documents) * 100) : 0
    kpis.value = [
      { label: '文档总量', value: String(s.total_documents), unit: '篇', tone: 'brand' },
      { label: '切片总量', value: String(s.total_chunks), unit: '片', tone: 'brand' },
      { label: '公开文档', value: String(bySec.PUBLIC || 0), unit: '篇', tone: 'good' },
      { label: '内部文档', value: String(bySec.INTERNAL || 0), unit: '篇', tone: '' },
      { label: '入库完成率', value: String(doneRate), unit: '%', tone: 'good' },
      { label: '状态', value: healthOk.value ? '正常' : '异常', unit: '', tone: healthOk.value ? 'good' : 'warn' },
    ]
  } catch {
    kpis.value = [{ label: '状态', value: '后端未连通', unit: '', tone: 'warn' }]
  }

  // 趋势图：以密级分布占位演示（真实检索量需接入日志）
  if (trendEl.value) {
    chart = echarts.init(trendEl.value)
    chart.setOption({
      grid: { left: 36, right: 16, top: 20, bottom: 24 },
      xAxis: { type: 'category', data: ['公开', '内部', '受限', '秘密'],
        axisLine: { lineStyle: { color: '#3a4a66' } }, axisLabel: { color: '#8a99b3' } },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } },
        axisLabel: { color: '#8a99b3' } },
      series: [{
        type: 'bar', data: [
          stats.value?.by_security?.PUBLIC || 0,
          stats.value?.by_security?.INTERNAL || 0,
          stats.value?.by_security?.RESTRICTED || 0,
          stats.value?.by_security?.SECRET || 0,
        ],
        itemStyle: { color: '#3d7eff', borderRadius: [4, 4, 0, 0] },
      }],
    })
  }
})

async function ping() {
  try {
    const r = await fetch('/v1/health')
    healthOk.value = r.ok
  } catch {
    healthOk.value = false
  }
}
ping()
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
      <div class="ttl">各密级文档分布</div>
      <div ref="trendEl" class="chart-box"></div>
    </div>
    <div class="panel health">
      <div class="ttl">系统健康</div>
      <div class="h-item">
        <span>后端服务 (FastAPI)</span>
        <span class="dot" :class="healthOk ? 'good' : 'warn'" />{{ healthOk ? '正常' : '异常' }}
      </div>
      <div class="h-item">
        <span>PostgreSQL + pgvector</span><span class="dot good" />正常
      </div>
      <div class="h-item">
        <span>向量化 (192.168.6.49:8002)</span><span class="dot good" />已上线
      </div>
      <div class="h-item">
        <span>重排 (192.168.6.49:8001)</span><span class="dot good" />已上线
      </div>
      <div class="h-item">
        <span>生成 (阿里云百炼)</span><span class="dot good" />云端
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
.dot.warn { background: var(--c-warning); }
</style>
