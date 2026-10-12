<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { listAuditLogs, type AuditLog } from '@/api/client'

const rows = ref<AuditLog[]>([])
const loading = ref(false)
const page = ref(1)
const size = ref(20)

const filters = reactive({
  actor: '',
  action: '',
  object_type: '' as string,
  result: '' as string,
})

const OBJECT_TYPES = [
  { value: 'document', label: '文档' },
  { value: 'ingestion_job', label: '入库任务' },
  { value: 'template', label: '模板' },
  { value: 'config', label: '配置项' },
  { value: 'audit_log', label: '审计日志' },
]

async function load() {
  loading.value = true
  try {
    rows.value = await listAuditLogs({
      actor: filters.actor || undefined,
      action: filters.action || undefined,
      object_type: filters.object_type || undefined,
      result: filters.result || undefined,
      page: page.value,
      size: size.value,
    })
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)

function search() {
  page.value = 1
  load()
}

function fmtTime(t: string | null) {
  if (!t) return '—'
  return t.replace('T', ' ').slice(0, 19)
}

// 自动刷新（10s），便于审计场景实时观察
const autoRefresh = ref(false)
let refreshTimer: number | null = null
function toggleAuto() {
  if (autoRefresh.value) {
    refreshTimer = window.setInterval(load, 10000)
  } else if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="panel pad">
    <div class="head">
      <div class="ttl serif">审计日志</div>
      <span class="text-muted">谁 · 何时 · 做了什么 · 结果如何 · 主题 · 客体，全量留痕可追溯</span>
      <div class="spacer" />
      <el-checkbox v-model="autoRefresh" @change="toggleAuto">自动刷新（10s）</el-checkbox>
    </div>

    <div class="filters">
      <el-input v-model="filters.actor" placeholder="操作者（支持模糊）" clearable style="width: 200px" @keyup.enter="search" @clear="search" />
      <el-input v-model="filters.action" placeholder="动作（如：文档入库）" clearable style="width: 180px" @keyup.enter="search" @clear="search" />
      <el-select v-model="filters.object_type" placeholder="客体类型" clearable style="width: 140px" @change="search">
        <el-option v-for="o in OBJECT_TYPES" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="filters.result" placeholder="结果" clearable style="width: 120px" @change="search">
        <el-option label="成功" value="success" />
        <el-option label="失败" value="failure" />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" empty-text="暂无审计记录">
      <el-table-column label="时间" width="170">
        <template #default="{ row }"><span class="mono">{{ fmtTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column prop="actor" label="操作者" width="140" />
      <el-table-column prop="action" label="动作" width="130" />
      <el-table-column label="请求" min-width="200" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.method }} {{ row.endpoint }}</span></template>
      </el-table-column>
      <el-table-column label="客体" width="180">
        <template #default="{ row }">
          <span v-if="row.object_type">{{ row.object_type }}<span v-if="row.object_id" class="oid"> · {{ row.object_id.slice(0, 8) }}…</span></span>
          <span v-else class="text-muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="结果" width="90">
        <template #default="{ row }">
          <el-tag :type="row.result === 'success' ? 'success' : 'danger'" size="small">
            {{ row.result === 'success' ? '成功' : '失败' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态码" width="80">
        <template #default="{ row }"><span class="mono">{{ row.status_code ?? '—' }}</span></template>
      </el-table-column>
      <el-table-column label="耗时" width="90">
        <template #default="{ row }"><span class="mono">{{ row.duration_ms != null ? row.duration_ms + 'ms' : '—' }}</span></template>
      </el-table-column>
      <el-table-column prop="detail" label="详情" min-width="200" show-overflow-tooltip />
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        :total="page * size + (rows.length === size ? 1 : 0)"
        @current-change="load"
        @size-change="search" />
      <span class="text-muted">按页浏览（ newest first ）</span>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.ttl { font-size: 18px; color: var(--text-strong); }
.spacer { flex: 1; }
.filters { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.mono { font-family: var(--font-mono, monospace); font-size: 12px; }
.oid { color: var(--text-faint); }
.pager { display: flex; align-items: center; gap: 14px; margin-top: 14px; }
</style>
