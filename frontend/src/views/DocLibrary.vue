<script setup lang="ts">
import { onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client from '@/api/client'
import {
  caller,
  getDocument,
  listDocuments,
  uploadDocument,
  getJob,
  type DocSummary,
  type DocDetail,
  type IngestionJob,
  type ChunkStrategy,
  type SecurityLevel,
} from '@/api/client'

const rows = ref<DocSummary[]>([])
const loading = ref(false)
const total = ref(0)

// ---- 过滤 ----
const filters = reactive({
  q: '',
  doc_type: '' as string,
  security_level: '' as string,
})

async function load() {
  loading.value = true
  try {
    const data = await listDocuments({
      q: filters.q || undefined,
      doc_type: filters.doc_type || undefined,
      security_level: filters.security_level || undefined,
      size: 100,
    })
    rows.value = data
    total.value = data.length
  } finally {
    loading.value = false
  }
}
load()

// ---- 接入文档（上传 + 切分策略 + 状态机进度）----
const dialog = ref(false)
const uploading = ref(false)
const fileRef = ref<File | null>(null)
const strategyDesc: Record<ChunkStrategy, string> = {
  paragraph: '按段落边界切分（默认，最稳妥，尽量不切断语义）',
  fixed: '定长滑动窗口（无视边界，召回最均匀）',
  sentence: '按句子切分后合并（保证单句完整，问答友好）',
  section: '按章节/条款结构切分（公文条款级，保留结构）',
}
const form = reactive({
  title: '',
  doc_type: '通知',
  issuer: '',
  doc_no: '',
  security_level: 'PUBLIC' as SecurityLevel,
  permission_tags: '',
  chunk_strategy: 'paragraph' as ChunkStrategy,
  chunk_size: 800,
  chunk_overlap: 80,
})

function onFileChange(file: any) {
  fileRef.value = file.raw as File
  if (!form.title) form.title = file.name.replace(/\.[^.]+$/, '')
}

// 状态机步骤
const STEPS = ['已提交', '解析中', '切片中', '向量化中', '已完成']
const STATUS_STEP: Record<string, number> = {
  PENDING: 0, PARSING: 1, CHUNKING: 2, EMBEDDING: 3, INDEXED: 4,
}
const activeJob = ref<IngestionJob | null>(null)
const jobDialog = ref(false)
let pollTimer: number | null = null

function startPoll(jobId: string) {
  jobDialog.value = true
  const tick = async () => {
    try {
      const j = await getJob(jobId)
      activeJob.value = j
      if (j.status === 'INDEXED' || j.status === 'FAILED') {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
        if (j.status === 'INDEXED') ElMessage.success('入库完成')
        else ElMessage.error('入库失败：' + (j.error_message || '未知错误'))
        load()
      }
    } catch (e) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    }
  }
  tick()
  pollTimer = window.setInterval(tick, 1000)
}

async function submit() {
  if (!fileRef.value) {
    ElMessage.warning('请先选择文件')
    return
  }
  uploading.value = true
  try {
    const job = await uploadDocument(fileRef.value, {
      title: form.title,
      doc_type: form.doc_type,
      issuer: form.issuer,
      doc_no: form.doc_no,
      security_level: form.security_level,
      permission_tags: form.permission_tags,
      chunk_strategy: form.chunk_strategy,
      chunk_size: form.chunk_size,
      chunk_overlap: form.chunk_overlap,
    })
    dialog.value = false
    ElMessage.success('已提交，开始入库')
    startPoll(job.id)
  } catch (e: any) {
    ElMessage.error('上传失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    uploading.value = false
    fileRef.value = null
  }
}

onBeforeUnmount(() => { if (pollTimer) clearInterval(pollTimer) })

// ---- 切片详情抽屉 ----
const drawer = ref(false)
const detail = ref<DocDetail | null>(null)
const detailLoading = ref(false)
async function openDetail(id: string) {
  drawer.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getDocument(id)
  } catch (e: any) {
    ElMessage.error('获取文档分片失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    detailLoading.value = false
  }
}

// 占位：内联接入（第三方即时接入，保留演示）
async function quickIngest() {
  ElMessage.info('请使用「接入文档」上传文件；内联接入为第三方接口能力')
}
</script>

<template>
  <div class="wrap">
    <div class="panel pad">
      <div class="head">
        <div class="ttl serif">文档库</div>
        <span class="count text-muted">共 {{ total }} 篇 · 真实数据</span>
        <div class="spacer" />
        <el-button type="primary" @click="dialog = true">接入文档</el-button>
      </div>

      <div class="filters">
        <el-input v-model="filters.q" placeholder="按标题/文号搜索" clearable style="width: 220px" @keyup.enter="load" @clear="load" />
        <el-select v-model="filters.doc_type" placeholder="文种" clearable style="width: 130px" @change="load">
          <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
        </el-select>
        <el-select v-model="filters.security_level" placeholder="密级" clearable style="width: 120px" @change="load">
          <el-option label="公开" value="PUBLIC" /><el-option label="内部" value="INTERNAL" />
          <el-option label="受限" value="RESTRICTED" /><el-option label="秘密" value="SECRET" />
        </el-select>
        <el-button @click="load">查询</el-button>
      </div>

      <el-table :data="rows" v-loading="loading" style="width: 100%" empty-text="暂无文档（请先接入）">
        <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
        <el-table-column prop="doc_type" label="文种" width="90" />
        <el-table-column label="密级" width="100">
          <template #default="{ row }"><span class="badge" :class="row.security_level">{{ row.security_level }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span class="st" :class="{ ok: row.status === 'INDEXED' }">{{ row.status }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="切片数" width="90" />
        <el-table-column label="权限标签" min-width="160">
          <template #default="{ row }">{{ (row.permission_tags || []).join(', ') || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }"><el-button link type="primary" @click="openDetail(row.id)">切片</el-button></template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 接入文档对话框 -->
    <el-dialog v-model="dialog" title="接入文档（解析 → 切片 → 向量化 → 入库）" width="620px">
      <el-upload
        drag :auto-upload="false" :show-file-list="true" accept=".pdf,.docx,.doc,.txt"
        :on-change="onFileChange" :limit="1">
        <div class="up-tip">将 PDF / DOCX 拖到此处，或点击选择</div>
      </el-upload>
      <el-form :model="form" label-width="92px" style="margin-top: 14px">
        <el-form-item label="标题"><el-input v-model="form.title" placeholder="默认取文件名" /></el-form-item>
        <el-form-item label="文种">
          <el-select v-model="form.doc_type" style="width: 160px">
            <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="发文机关"><el-input v-model="form.issuer" /></el-form-item>
        <el-form-item label="文号"><el-input v-model="form.doc_no" /></el-form-item>
        <el-form-item label="密级">
          <el-select v-model="form.security_level">
            <el-option label="公开" value="PUBLIC" /><el-option label="内部" value="INTERNAL" />
            <el-option label="受限" value="RESTRICTED" /><el-option label="秘密" value="SECRET" />
          </el-select>
        </el-form-item>
        <el-form-item label="权限标签"><el-input v-model="form.permission_tags" placeholder="scope:dept:xxx,scope:role:yyy" /></el-form-item>
        <el-form-item label="切分策略">
          <el-select v-model="form.chunk_strategy" style="width: 160px">
            <el-option v-for="(d, k) in strategyDesc" :key="k" :label="k" :value="k" />
          </el-select>
          <span class="hint">{{ strategyDesc[form.chunk_strategy] }}</span>
        </el-form-item>
        <el-form-item label="切片尺寸">
          <el-input-number v-model="form.chunk_size" :min="64" :max="4000" :step="50" />
          <span class="hint">单切片最大字符数</span>
        </el-form-item>
        <el-form-item label="重叠字符">
          <el-input-number v-model="form.chunk_overlap" :min="0" :max="1000" :step="10" />
          <span class="hint">相邻切片重叠</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submit">提交入库</el-button>
      </template>
    </el-dialog>

    <!-- 入库进度（状态机）对话框 -->
    <el-dialog v-model="jobDialog" title="入库进度" width="520px" :close-on-click-modal="false">
      <div v-if="activeJob" class="job">
        <div class="job-file">{{ activeJob.filename }} · 策略 {{ activeJob.strategy }}</div>
        <el-steps :active="STATUS_STEP[activeJob.status] ?? 0" align-center finish-status="success" style="margin: 18px 0">
          <el-step v-for="s in STEPS" :key="s" :title="s" />
        </el-steps>
        <el-progress
          :percentage="activeJob.progress"
          :status="activeJob.status === 'FAILED' ? 'exception' : (activeJob.status === 'INDEXED' ? 'success' : '')" />
        <div class="job-meta text-muted">
          <span v-if="activeJob.total_chunks">切片 {{ activeJob.processed_chunks }} / {{ activeJob.total_chunks }}</span>
          <span>状态 {{ activeJob.status }}</span>
        </div>
        <div v-if="activeJob.status === 'FAILED'" class="job-err">{{ activeJob.error_message }}</div>
      </div>
    </el-dialog>

    <!-- 切片详情抽屉 -->
    <el-drawer v-model="drawer" title="文档切片" size="46%">
      <div v-loading="detailLoading">
        <div v-if="detail">
          <div class="d-head">
            <div class="d-title serif">{{ detail.title }}</div>
            <div class="text-muted">
              <span class="badge" :class="detail.security_level">{{ detail.security_level }}</span>
              文种 {{ detail.doc_type }} · 切片 {{ detail.chunk_count }} 片
            </div>
          </div>
          <div v-for="c in detail.chunks" :key="c.seq" class="chunk panel">
            <div class="c-head"><b>#{{ c.seq }}</b><span class="text-muted">{{ c.tokens }} 字</span></div>
            <div class="c-body">{{ c.content }}</div>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.ttl { font-size: 18px; color: var(--text-strong); flex: 1; }
.count { font-size: 13px; }
.filters { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.badge { color: var(--text); }
.st { color: var(--text-muted); font-size: 13px; }
.st.ok { color: var(--c-success); }
.up-tip { color: var(--text-muted); padding: 18px 0; }
.hint { color: var(--text-faint); font-size: 12px; margin-left: 10px; }
.job-file { color: var(--text-strong); }
.job-meta { display: flex; gap: 18px; margin-top: 10px; font-size: 13px; }
.job-err { color: var(--c-danger); font-size: 12px; margin-top: 8px; word-break: break-all; }
.d-head { margin-bottom: 14px; }
.d-title { font-size: 17px; color: var(--text-strong); margin-bottom: 6px; }
.chunk { padding: 12px 14px; margin-bottom: 10px; }
.c-head { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 12px; margin-bottom: 6px; }
.c-body { color: var(--text); line-height: 1.7; white-space: pre-wrap; font-size: 13px; }
</style>
