<script setup lang="ts">
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref } from 'vue'

const gauges = [
  { name: 'GPU 利用率', value: 63, unit: '%' },
  { name: '显存占用', value: 41, unit: '%' },
  { name: '检索队列', value: 12, unit: '' },
]
const chartEls = ref<HTMLDivElement[]>([])
let charts: echarts.ECharts[] = []

function render(el: HTMLDivElement | null, v: number) {
  if (!el) return
  const c = echarts.init(el)
  c.setOption({
    series: [{
      type: 'gauge', radius: '92%', progress: { show: true, width: 10, itemStyle: { color: '#3d7eff' } },
      axisLine: { lineStyle: { width: 10, color: [[1, 'rgba(255,255,255,.1)']] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      pointer: { show: false }, detail: { valueAnimation: true, fontSize: 22, color: '#eaf1fb', offsetCenter: [0, 0] },
      data: [{ value: v }],
    }],
  })
  charts.push(c)
}
onMounted(() => chartEls.value.forEach((el, i) => render(el, gauges[i].value)))
onBeforeUnmount(() => charts.forEach((c) => c.dispose()))

const logs = [
  { t: '10:02:11', lv: 'INFO', m: '检索请求 trace=8f2a 命中 8 片，密级下推过滤 0 片' },
  { t: '10:02:09', lv: 'INFO', m: 'Embedding 批次完成 64 片 / 2.1s' },
  { t: '10:01:55', lv: 'WARN', m: 'Reranker 延迟 380ms（高于基线 300ms）' },
  { t: '10:01:40', lv: 'INFO', m: '文档 external_id=sc-030 入库完成，切片 42' },
]
</script>

<template>
  <div class="grid">
    <div v-for="(g, i) in gauges" :key="g.name" class="panel gauge">
      <div class="name text-muted">{{ g.name }}</div>
      <div ref="chartEls" class="g-box" />
      <div class="val">{{ g.value }}{{ g.unit }}</div>
    </div>
    <div class="panel cap">
      <div class="name text-muted">容量</div>
      <div class="cap-row"><span>原文存储</span><b>44 / 512 GB</b></div>
      <div class="cap-row"><span>向量</span><b>7.9 / 64 GB</b></div>
      <div class="cap-row"><span>文档总量</span><b>30 / 200,000</b></div>
    </div>
  </div>

  <div class="panel log">
    <div class="ttl">安全态势与运行日志</div>
    <div class="sec">
      <span class="badge SECRET">涉密不出网：生效</span>
      <span class="badge INTERNAL">权限下推：强制</span>
      <span class="badge RESTRICTED">敏感词校验：开启</span>
    </div>
    <div v-for="l in logs" :key="l.t" class="l">
      <span class="mono t">{{ l.t }}</span>
      <span class="lv" :class="l.lv">{{ l.lv }}</span>
      <span class="m">{{ l.m }}</span>
    </div>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap); }
.gauge, .cap { padding: 14px; text-align: center; }
.name { font-size: 13px; }
.g-box { height: 120px; }
.val { font-size: 18px; color: var(--text-strong); font-weight: 600; }
.cap-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--line); }
.cap-row b { color: var(--brand); }
.log { padding: 16px; margin-top: var(--gap); }
.ttl { color: var(--text-strong); font-weight: 600; margin-bottom: 10px; }
.sec { display: flex; gap: 10px; margin-bottom: 12px; }
.l { display: flex; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
.t { color: var(--text-faint); }
.lv { width: 48px; }
.lv.INFO { color: var(--brand); } .lv.WARN { color: var(--c-warning); } .lv.ERROR { color: var(--c-danger); }
.m { color: var(--text); }
</style>
