<script setup lang="ts">
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import {
  getAuditStats,
  getStats,
  listAuditLogs,
  listJobs,
  type AuditLog,
  type AuditStats,
  type DocStats,
  type IngestionJob,
} from '@/api/client'

/** 运维台全部使用真实数据：统计来自 /documents/stats + /audit/stats，
 *  任务来自 /documents/jobs，运行日志即审计流水 /audit/logs。 */

const stats = ref<DocStats | null>(null)
const audit = ref<AuditStats | null>(null)
const jobs = ref<IngestionJob[]>([])
const logs = ref<AuditLog[]>([])

const TERMINAL_OK = 'INDEXED'
const isRunning = (j: IngestionJob) => !['INDEXED', 'FAILED'].includes(j.status)

async function loadAll() {
  try {
    stats.value = await getStats()
  } catch { /* 保持上次数据 */ }
  try {
    audit.value = await getAuditStats()
  } catch { /* 保持上次数据 */ }
  try {
    jobs.value = await listJobs(20)
  } catch { /* 保持上次数据 */ }
  try {
    logs.value = await listAuditLogs({ size: 12 })
  } catch { /* 保持上次数据 */ }
  renderChart()
}

// ---- 按动作分布（真实审计数据）----
const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
function renderChart() {
  if (!chartEl.value || !audit.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const entries = Object.entries(audit.value.by_action).sort((a, b) => b[1] - a[1]).slice(0, 8)
  chart.setOption({
    grid: { left: 8, right: 20, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } } },
    yAxis: {
      type: 'category',
      data: entries.map(([k]) => k).reverse(),
      axisLabel: { color: '#c7d4e8', fontSize: 12 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,.12)' } },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar', barWidth: 12,
      data: entries.map(([, v]) => v).reverse(),
      itemStyle: { color: '#3d7eff', borderRadius: [0, 6, 6, 0] },
      label: { show: true, position: 'right', color: '#eaf1fb', fontSize: 12 },
    }],
  })
}

let timer: number | null = null
onMounted(() => {
  loadAll()
  timer = window.setInterval(loadAll, 8000)   // 8s 自动刷新
  window.addEventListener('resize', onResize)
})
function onResize() { chart?.resize() }
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', onResize)
  chart?.dispose()
})

function fmtTime(t: string | null) {
  return t ? t.replace('T', ' ').slice(11, 19) : '--:--:--'
}
function jobStatusType(s: string): 'success' | 'danger' | 'warning' | 'info' {
  if (s === 'INDEXED') return 'success'
  if (s === 'FAILED') return 'danger'
  return 'warning'
}
</script>

<template>
  <div class="cards">
    <div class="panel card">
      <div class="name text-muted">文档总数</div>
      <div class="val">{{ stats?.total_documents ?? '—' }}</div>
    </div>
    <div class="panel card">
      <div class="name text-muted">切片总数</div>
      <div class="val">{{ stats?.total_chunks ?? '—' }}</div>
    </div>
    <div class="panel card">
      <div class="name text-muted">入库中任务</div>
      <div class="val warn">{{ jobs.filter(isRunning).length }}</div>
    </div>
    <div class="panel card">
      <div class="name text-muted">失败任务</div>
      <div class="val" :class="{ danger: jobs.some((j) => j.status === 'FAILED') }">
        {{ jobs.filter((j) => j.status === 'FAILED').length }}
      </div>
    </div>
    <div class="panel card">
      <div class="name text-muted">近 24h 审计操作</div>
      <div class="val">{{ audit?.recent_24h ?? '—' }}</div>
    </div>
    <div class="panel card">
      <div class="name text-muted">审计留痕总数</div>
      <div class="val">{{ audit?.total ?? '—' }}</div>
    </div>
  </div>

  <div class="mid">
    <div class="panel chart-box">
      <div class="ttl">操作分布（近实时，按审计动作）</div>
      <div ref="chartEl" class="chart" />
    </div>
    <div class="panel sec-box">
      <div class="ttl">安全机制（强制执行项）</div>
      <div class="sec">
        <span class="badge SECRET">涉密不出网：Provider 隔离 + 启动自检</span>
        <span class="badge INTERNAL">权限下推：检索阶段 WHERE 强制</span>
        <span class="badge RESTRICTED">敏感词：配置中心维护 · 生成后脱敏</span>
      </div>
      <div class="ttl" style="margin-top:14px">入库任务（真实状态机）</div>
      <el-table :data="jobs.slice(0, 6)" size="small" empty-text="暂无入库任务">
        <el-table-column prop="filename" label="文件" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><el-tag :type="jobStatusType(row.status)" size="small">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="进度" width="140">
          <template #default="{ row }">
            <el-progress :percentage="row.progress" :stroke-width="8" :show-text="false"
              :status="row.status === 'FAILED' ? 'exception' : row.status === 'INDEXED' ? 'success' : undefined" />
          </template>
        </el-table-column>
        <el-table-column prop="strategy" label="策略" width="100" />
      </el-table>
    </div>
  </div>

  <div class="panel log">
    <div class="ttl">运行日志（审计流水 · 谁/何时/做了什么/结果）</div>
    <div v-for="l in logs" :key="l.id" class="l">
      <span class="mono t">{{ fmtTime(l.created_at) }}</span>
      <span class="actor">{{ l.actor }}</span>
      <span class="lv" :class="l.result === 'success' ? 'INFO' : 'ERROR'">{{ l.result === 'success' ? 'OK' : 'FAIL' }}</span>
      <span class="m">{{ l.action }}<span class="text-muted"> · {{ l.method }} {{ l.endpoint }}<template v-if="l.duration_ms != null"> · {{ l.duration_ms }}ms</template></span></span>
    </div>
    <div v-if="!logs.length" class="text-muted">暂无日志（操作文档库/检索/生成后自动产生）</div>
  </div>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(6, 1fr); gap: var(--gap); }
.card { padding: 14px 16px; }
.name { font-size: 12px; }
.val { font-size: 24px; color: var(--text-strong); font-weight: 600; margin-top: 6px; }
.val.warn { color: var(--c-warning); }
.val.danger { color: var(--c-danger); }
.mid { display: grid; grid-template-columns: 5fr 7fr; gap: var(--gap); margin-top: var(--gap); }
.chart-box, .sec-box { padding: 16px; }
.ttl { color: var(--text-strong); font-weight: 600; margin-bottom: 10px; font-size: 14px; }
.chart { height: 220px; }
.sec { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; }
.log { padding: 16px; margin-top: var(--gap); }
.l { display: flex; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 13px; align-items: baseline; }
.t { color: var(--text-faint); }
.actor { color: var(--brand); min-width: 90px; }
.lv { width: 40px; font-weight: 600; }
.lv.INFO { color: var(--c-success); }
.lv.ERROR { color: var(--c-danger); }
.m { color: var(--text); }
</style>
