<script setup lang="ts">
/**
 * 文档库：类文件夹的层级空间导航（用户 2026-10-13 反馈）。
 *
 * 交互模型（与操作系统的文件夹一致）：
 * - 进入文档库首先列出【根级文档空间】（文件夹卡片：名称/描述/文档数/
 *   切片数/子空间数）；
 * - 点击空间卡片进入该空间：顶部面包屑可逐级返回，空间内先展示子空间
 *   卡片、再展示属于该空间的文档表格；
 * - 任意层级可"新建子空间 / 重命名 / 删除"（有文档或子空间时后端 409
 *   保护，前端给出明确提示）；
 * - 接入文档默认归属当前所在空间。
 *
 * 数据：空间为扁平列表（/v1/spaces 返回 parent_id），树形结构由前端
 * 按 parent_id 组装（childrenOf / breadcrumb），避免后端维护递归接口。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createSpace,
  deleteSpace,
  getDocument,
  getJob,
  listDocuments,
  listSpaces,
  updateSpace,
  uploadDocument,
  type DocSpace,
  type DocSummary,
  type DocDetail,
  type IngestionJob,
  type ChunkStrategy,
  type SecurityLevel,
} from '@/api/client'

const rows = ref<DocSummary[]>([])
const loading = ref(false)
const total = ref(0)

// ---- 文档空间（层级）----
const spaces = ref<DocSpace[]>([])
/** 当前所在空间：null = 根级视图（只列空间，不列文档） */
const currentSpace = ref<string | null>(null)

async function loadSpaces() {
  try {
    spaces.value = await listSpaces()
  } catch {
    ElMessage.error('加载文档空间失败')
  }
}
onMounted(async () => {
  await loadSpaces()
  load()
})

/** id → 空间（含默认虚拟根） */
const spaceById = computed(() => {
  const m = new Map<string, DocSpace>()
  spaces.value.forEach((s) => m.set(s.id, s))
  return m
})

/** 直接子空间 */
function childrenOf(id: string | null): DocSpace[] {
  return spaces.value.filter((s) => (s.parent_id || null) === id)
}

/** 根级空间（进入文档库首先看到的内容） */
const rootSpaces = computed(() => childrenOf(null))

/** 当前空间的直接子空间（进入空间后展示） */
const childSpaces = computed(() =>
  currentSpace.value ? childrenOf(currentSpace.value) : [],
)

/** 当前空间可见的卡片列表：根级视图列根空间，空间内列子空间 */
const visibleSpaces = computed(() =>
  currentSpace.value ? childSpaces.value : rootSpaces.value,
)

/** 面包屑链：根 → … → 当前空间 */
const breadcrumb = computed<DocSpace[]>(() => {
  const chain: DocSpace[] = []
  let cursor = currentSpace.value ? spaceById.value.get(currentSpace.value) : undefined
  // 理论上不会成环（后端防环），这里仍加访问上限保护
  let guard = 0
  while (cursor && guard < 32) {
    chain.unshift(cursor)
    cursor = cursor.parent_id ? spaceById.value.get(cursor.parent_id) : undefined
    guard += 1
  }
  return chain
})

const currentSpaceObj = computed(() =>
  currentSpace.value ? spaceById.value.get(currentSpace.value) ?? null : null,
)

/** 空间的完整路径标签（"父 / 子"），用于下拉选项等场景 */
function spacePathLabel(id: string | null): string {
  if (!id) return '—'
  const parts: string[] = []
  let cursor = spaceById.value.get(id)
  let guard = 0
  while (cursor && guard < 32) {
    parts.unshift(cursor.name)
    cursor = cursor.parent_id ? spaceById.value.get(cursor.parent_id) : undefined
    guard += 1
  }
  return parts.join(' / ') || id.slice(0, 8)
}

function enterSpace(id: string) {
  currentSpace.value = id
  load()
}

function goRoot() {
  currentSpace.value = null
  rows.value = []
  total.value = 0
}

// ---- 过滤 ----
const filters = reactive({
  q: '',
  doc_type: '' as string,
  security_level: '' as string,
})

async function load() {
  // 根级视图只列空间（文件夹导航），不拉文档列表
  if (!currentSpace.value) {
    rows.value = []
    total.value = 0
    return
  }
  loading.value = true
  try {
    const data = await listDocuments({
      q: filters.q || undefined,
      doc_type: filters.doc_type || undefined,
      security_level: filters.security_level || undefined,
      space_id: currentSpace.value,
      size: 100,
    })
    rows.value = data
    total.value = data.length
  } finally {
    loading.value = false
  }
}

// ---- 接入文档（上传 + 切分策略 + 状态机进度）----
const dialog = ref(false)
const uploading = ref(false)
const fileRef = ref<File | null>(null)

/** 打开上传对话框：默认把文档放到当前正在浏览的空间 */
function openUpload() {
  form.space_id = currentSpace.value || ''
  dialog.value = true
}
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
  space_id: '',
  chunk_strategy: 'paragraph' as ChunkStrategy,
  chunk_size: 800,
  chunk_overlap: 80,
})

// ---- 空间新建 / 重命名对话框 ----
const spaceDialog = ref(false)
const spaceSaving = ref(false)
const spaceForm = reactive({ name: '', code: '', description: '', parent_id: '' as string })
const editingSpace = ref<DocSpace | null>(null)

/** 空间下拉选项：根级 + 全部空间（带路径缩进），供新建时选父空间 */
const spaceParentOptions = computed(() => {
  const opts: { id: string; label: string; depth: number }[] = []
  const walk = (parentId: string | null, depth: number) => {
    childrenOf(parentId).forEach((s) => {
      opts.push({ id: s.id, label: s.name, depth })
      walk(s.id, depth + 1)
    })
  }
  walk(null, 0)
  return opts
})

/** 打开新建空间对话框；parentId 为预期父空间（空间内新建子空间时传入） */
function openCreateSpace(parentId?: string | null) {
  editingSpace.value = null
  spaceForm.name = ''
  spaceForm.code = ''
  spaceForm.description = ''
  spaceForm.parent_id = parentId ?? currentSpace.value ?? ''
}

function editSpace(s: DocSpace) {
  editingSpace.value = s
  spaceForm.name = s.name
  spaceForm.code = s.code
  spaceForm.description = s.description || ''
  spaceForm.parent_id = s.parent_id || ''
}

async function submitSpace() {
  if (!spaceForm.name.trim()) {
    ElMessage.warning('请输入空间名称')
    return
  }
  spaceSaving.value = true
  try {
    if (editingSpace.value) {
      await updateSpace(editingSpace.value.id, {
        name: spaceForm.name.trim(),
        description: spaceForm.description.trim() || undefined,
        parent_id: spaceForm.parent_id || null,
      })
      ElMessage.success('空间已更新')
    } else {
      if (!spaceForm.code.trim()) {
        ElMessage.warning('请输入空间编码（唯一标识，建议英文）')
        spaceSaving.value = false
        return
      }
      await createSpace({
        name: spaceForm.name.trim(),
        code: spaceForm.code.trim(),
        description: spaceForm.description.trim() || undefined,
        parent_id: spaceForm.parent_id || null,
      })
      ElMessage.success('空间已创建')
    }
    await loadSpaces()
    spaceDialog.value = false
    load()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    spaceSaving.value = false
  }
}

async function removeSpace(s: DocSpace) {
  // 前端预检：有子空间时明确提示先处理（后端同样有 409 保护）
  const kids = childrenOf(s.id)
  if (kids.length > 0) {
    ElMessage.warning(`空间「${s.name}」下还有 ${kids.length} 个子空间，请先删除或移动子空间`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `删除空间「${s.name}」？空间内的文档需要先处理（迁移或删除）。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteSpace(s.id)
    ElMessage.success('已删除')
    if (currentSpace.value === s.id) currentSpace.value = s.parent_id || null
    await loadSpaces()
    load()
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.response?.data?.detail || e.message))
  }
}

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
      space_id: form.space_id || undefined,
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
      <!-- 头部：标题 + 面包屑 + 操作 -->
      <div class="head">
        <div class="ttl serif">文档库</div>
        <!-- 面包屑（类文件夹路径导航） -->
        <div class="crumb-bar">
          <span class="crumb" :class="{ active: !currentSpace }" @click="goRoot">全部空间</span>
          <template v-for="(node, i) in breadcrumb" :key="node.id">
            <span class="crumb-sep">/</span>
            <span
              class="crumb"
              :class="{ active: i === breadcrumb.length - 1 }"
              @click="enterSpace(node.id)"
            >{{ node.name }}</span>
          </template>
        </div>
        <div class="spacer" />
        <span v-if="currentSpace" class="count text-muted">
          {{ total }} 篇文档 · {{ childSpaces.length }} 个子空间
        </span>
        <span v-else class="count text-muted">{{ rootSpaces.length }} 个空间</span>
        <el-button @click="openCreateSpace()">
          {{ currentSpace ? '新建子空间' : '新建空间' }}
        </el-button>
        <el-button type="primary" @click="openUpload()">接入文档</el-button>
      </div>

      <!-- 空间卡片网格（根级视图列根空间；空间内列子空间） -->
      <div v-if="visibleSpaces.length" class="space-grid">
        <div
          v-for="s in visibleSpaces"
          :key="s.id"
          class="space-card panel"
          @click="enterSpace(s.id)"
        >
          <div class="sc-top">
            <span class="sc-icon">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
              </svg>
            </span>
            <span class="sc-name">{{ s.name }}</span>
            <el-tag v-if="s.is_default" size="small" type="info" effect="dark">默认</el-tag>
          </div>
          <div class="sc-desc text-muted">{{ s.description || '暂无描述' }}</div>
          <div class="sc-meta">
            <span>{{ s.doc_count }} 篇文档</span>
            <span>{{ s.chunk_count }} 切片</span>
            <span v-if="childrenOf(s.id).length">{{ childrenOf(s.id).length }} 个子空间</span>
          </div>
          <div class="sc-ops" @click.stop>
            <el-button link type="primary" size="small" @click="enterSpace(s.id)">进入</el-button>
            <el-button link size="small" @click="openCreateSpace(s.id)">新建子空间</el-button>
            <el-button link size="small" @click="editSpace(s)">重命名</el-button>
            <el-button
              v-if="!s.is_default"
              link
              type="danger"
              size="small"
              @click="removeSpace(s)"
            >删除</el-button>
          </div>
        </div>
      </div>
      <el-empty v-else-if="!currentSpace" description="还没有文档空间，点击右上角「新建空间」开始" :image-size="70" />

      <!-- 空间内：过滤 + 文档表格 -->
      <template v-if="currentSpace">
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

        <el-table :data="rows" v-loading="loading" style="width: 100%" empty-text="该空间暂无文档（点击「接入文档」上传）">
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
      </template>
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
        <el-form-item label="文档空间">
          <el-select v-model="form.space_id" placeholder="默认空间" style="width: 240px">
            <el-option
              v-for="s in spaces"
              :key="s.id"
              :label="spacePathLabel(s.id) + (s.is_default ? '（默认）' : '')"
              :value="s.id"
            />
          </el-select>
          <span class="hint">文档将归属到所选空间，检索可按空间过滤</span>
        </el-form-item>
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

    <!-- 空间新建 / 重命名对话框（含父空间选择，支撑层级整理） -->
    <el-dialog v-model="spaceDialog" :title="editingSpace ? '空间设置' : '新建空间'" width="560px">
      <el-form label-width="92px" style="margin-top: 4px">
        <el-form-item label="名称">
          <el-input v-model="spaceForm.name" placeholder="如：安全生产专项" />
        </el-form-item>
        <el-form-item label="编码" v-if="!editingSpace">
          <el-input v-model="spaceForm.code" placeholder="唯一标识，建议英文，如 safety-2026" />
        </el-form-item>
        <el-form-item label="父空间">
          <el-select v-model="spaceForm.parent_id" placeholder="根级（无父空间）" clearable style="width: 100%">
            <el-option label="根级（无父空间）" value="" />
            <el-option
              v-for="o in spaceParentOptions"
              :key="o.id"
              :label="o.label"
              :value="o.id"
              :disabled="editingSpace?.id === o.id"
            >
              <span :style="{ paddingLeft: (o.depth * 14) + 'px' }">{{ o.label }}</span>
            </el-option>
          </el-select>
          <span class="hint">层级结构类似文件夹；不选则为根级空间</span>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="spaceForm.description" placeholder="选填" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="spaceDialog = false">取消</el-button>
        <el-button type="primary" :loading="spaceSaving" @click="submitSpace">
          {{ editingSpace ? '保存' : '创建空间' }}
        </el-button>
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
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.ttl { font-size: 18px; color: var(--text-strong); }
.count { font-size: 13px; }

/* 面包屑（类文件夹路径） */
.crumb-bar { display: flex; align-items: center; gap: 6px; font-size: 13px; min-width: 0; }
.crumb {
  color: var(--text-muted); cursor: pointer; padding: 2px 6px; border-radius: 6px;
  max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.crumb:hover { color: var(--text); background: rgba(255, 255, 255, 0.05); }
.crumb.active { color: var(--text-strong); font-weight: 600; cursor: default; }
.crumb.active:hover { background: transparent; }
.crumb-sep { color: var(--text-faint); }

/* 空间卡片网格（文件夹样式） */
.space-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.space-card {
  padding: 14px 16px;
  cursor: pointer;
  transition: border-color 0.15s ease, transform 0.15s ease;
}
.space-card:hover {
  border-color: var(--brand-line);
  transform: translateY(-1px);
}
.sc-top { display: flex; align-items: center; gap: 8px; }
.sc-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0;
  background: var(--brand-soft); color: var(--brand);
}
.sc-name {
  font-size: 14px; font-weight: 600; color: var(--text-strong);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sc-desc {
  margin-top: 8px; font-size: 12px; line-height: 1.6; min-height: 38px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.sc-meta { display: flex; gap: 12px; margin-top: 8px; font-size: 12px; color: var(--text-muted); }
.sc-ops {
  display: flex; gap: 4px; margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--line);
  opacity: 0; transition: opacity 0.15s ease;
}
.space-card:hover .sc-ops { opacity: 1; }

.filters { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.badge { color: var(--text); }
.st { color: var(--text-muted); font-size: 13px; }
.st.ok { color: var(--c-success); }
.up-tip { color: var(--text-muted); padding: 18px 0; }
.hint { color: var(--text-muted); font-size: 12px; margin-left: 10px; }
.job-file { color: var(--text-strong); }
.job-meta { display: flex; gap: 18px; margin-top: 10px; font-size: 13px; }
.job-err { color: var(--c-danger); font-size: 12px; margin-top: 8px; word-break: break-all; }
.d-head { margin-bottom: 14px; }
.d-title { font-size: 17px; color: var(--text-strong); margin-bottom: 6px; }
.chunk { padding: 12px 14px; margin-bottom: 10px; }
.c-head { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 12px; margin-bottom: 6px; }
.c-body { color: var(--text); line-height: 1.7; white-space: pre-wrap; font-size: 13px; }
</style>
