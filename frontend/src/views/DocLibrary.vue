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
 * 空间所有权（用户 2026-10-14 需求：归属具象化 + 隔离）：
 * - 本人创建的空间仅本人可见、可上传、可检索（列表/详情/上传/检索
 *   均由后端按调用方身份隔离，前端只负责"看得见"的呈现）；
 * - 默认空间为 system 公共空间：所有人可见、可上传；
 * - 空间卡片以徽章标示归属（默认·公共 / 我的 / 仅自己可见），
 *   编辑/删除仅对本人空间开放（is_mine），默认空间仅 admin 可改名。
 *
 * 数据：空间为扁平列表（/v1/spaces 返回 parent_id），树形结构由前端
 * 按 parent_id 组装（childrenOf / breadcrumb），避免后端维护递归接口。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createSpace,
  currentUser,
  deleteSpace,
  getConfigItems,
  getDocument,
  getJob,
  listDocuments,
  listSpaces,
  updateSpace,
  uploadDocument,
  SECURITY_LEVEL_LABELS,
  SECURITY_LEVEL_OPTIONS,
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

// ---- 所有权视图（2026-10-14）----
/** 是否 admin（默认空间仅 admin 可改名，与后端权限一致） */
const isAdmin = computed(() => currentUser.value?.role === 'admin')

/** 空间归属徽章：默认·公共 / 我的 / 第三方 等 */
function ownerTag(s: DocSpace): { text: string; type: 'info' | 'success' | 'warning' } {
  if (s.is_default || s.owner_type === 'system') return { text: '默认·公共', type: 'info' }
  if (s.is_mine) return { text: '我的', type: 'success' }
  if (s.owner_type === 'client') return { text: '第三方', type: 'warning' }
  return { text: '他人', type: 'warning' }
}

/** 空间可否编辑/删除：本人空间随时可管；默认空间仅 admin 可改名（不可删） */
function canManageSpace(s: DocSpace): boolean {
  if (s.is_default) return isAdmin.value
  return s.is_mine
}

/** 上传对话框的空间选项标签（带归属提示） */
function spaceOptionLabel(s: DocSpace): string {
  const tag = ownerTag(s)
  return `${spacePathLabel(s.id)}（${tag.text}${s.is_default ? '·人人可上传' : ''}）`
}

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

/** 密级中文名（存量历史代码 RESTRICTED/SECRET 也能显示，回落原值） */
function secLabel(v: string): string {
  return (SECURITY_LEVEL_LABELS as Record<string, string>)[v] || v
}

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
/** 接入文件列表（受控 el-upload）；多文件按队列提交 */
const fileList = ref<any[]>([])
const pendingFiles = ref<File[]>([])

/** 打开上传对话框：默认把文档放到当前正在浏览的空间 */
function openUpload() {
  // 防御：当前空间不可上传（他人空间，理论上列表已过滤不会出现）时阻止并提示
  if (currentSpaceObj.value && !currentSpaceObj.value.can_upload) {
    ElMessage.warning(`空间「${currentSpaceObj.value.name}」仅创建者可上传文档`)
    return
  }
  // 关键修复：每次打开对话框重置文件与标题，避免残留上一次的接入信息
  //（el-upload 受控 file-list 清空 + 标题/文号归零）
  fileList.value = []
  pendingFiles.value = []
  form.title = ''
  form.issuer = ''
  form.doc_no = ''
  form.space_id = currentSpace.value || ''
  loadDualGranularity()
  dialog.value = true
}
const strategyDesc: Record<ChunkStrategy, string> = {
  paragraph: '按自然段切分（默认：段内不切断，仅超预算段按句/子句降级）',
  fixed: '定长滑动窗口（无视边界，召回最均匀）',
  sentence: '按句子切分后合并（保证单句完整，问答友好）',
  section: '按章节/条款结构切分（条款标题+正文为整体）',
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
})

// ---- 双粒度切片参数（2026-10-15：chunk_size 改为「配置中心联动」，只读展示）----
// 背景：双粒度（父子块）总开关默认开启，入库时若仍让用户填「分块大小」会与
// 配置中心的 small/large token 打架且实际不生效；故移除该输入，改为拉取并
// 只读展示配置中心当前值，引导用户到「配置中心 · 检索配置」调整。
const dualGran = ref<{
  enabled: boolean
  small: number
  small_overlap: number
  large: number
  large_overlap: number
  noise_clean: boolean
  paragraph_rebuild: boolean
} | null>(null)

/** 拉取配置中心的双粒度切片参数（只读展示；失败则置空，不影响上传） */
async function loadDualGranularity() {
  try {
    const items = await getConfigItems('retrieval')
    const raw = (k: string): unknown => items.find((i) => i.key === k)?.value?.value
    const on = raw('dual_granularity_enabled')
    dualGran.value = {
      // 缺省视为开启（与后端代码默认 dual_granularity_enabled=True 一致）
      enabled: on === undefined ? true : on === true || on === 'true',
      small: Number(raw('small_max_tokens') ?? 300),
      small_overlap: Number(raw('small_overlap_tokens') ?? 40),
      large: Number(raw('large_max_tokens') ?? 2500),
      large_overlap: Number(raw('large_overlap_tokens') ?? 200),
      // 解析清洗（页码/页眉剔除 + 乱码修复）与段落重建开关（2026-10-15 V2.0）
      noise_clean: raw('parse_noise_clean') === undefined ? true : raw('parse_noise_clean') === true || raw('parse_noise_clean') === 'true',
      paragraph_rebuild: raw('chunk_paragraph_rebuild') === undefined ? true : raw('chunk_paragraph_rebuild') === true || raw('chunk_paragraph_rebuild') === 'true',
    }
  } catch {
    dualGran.value = null
  }
}

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
  // 关键修复：之前仅重置表单却未打开对话框，导致「新建空间」按钮点击无反应。
  // 必须在重置后显式置位，对话框 v-model="spaceDialog" 才会弹出。
  spaceDialog.value = true
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

function onFileChange(_file: any, fl: any[]) {
  fileList.value = fl
  pendingFiles.value = (fl.map((f) => f.raw).filter(Boolean) as File[])
  // 仅单个文件且用户未手动填标题时默认取文件名；多文件不共用标题，由文件名兜底
  if (pendingFiles.value.length === 1 && !form.title) {
    form.title = pendingFiles.value[0].name.replace(/\.[^.]+$/, '')
  }
}

function onFileRemove(_file: any, fl: any[]) {
  fileList.value = fl
  pendingFiles.value = (fl.map((f) => f.raw).filter(Boolean) as File[])
}

// ---- 批量入库进度（多文件队列，统一轮询，全部完成后自动关闭）----
const batchJobs = ref<IngestionJob[]>([])
const jobDialog = ref(false)
let batchTimer: number | null = null

const batchAllDone = computed(() =>
  batchJobs.value.length > 0 &&
  batchJobs.value.every((j) => j.status === 'INDEXED' || j.status === 'FAILED'),
)
const batchOkCount = computed(() => batchJobs.value.filter((j) => j.status === 'INDEXED').length)
const batchFailCount = computed(() => batchJobs.value.filter((j) => j.status === 'FAILED').length)

function startBatchPoll(jobs: IngestionJob[]) {
  batchJobs.value = jobs.map((j) => ({ ...j }))
  jobDialog.value = true
  const tick = async () => {
    let allDone = true
    for (const job of batchJobs.value) {
      try {
        const j = await getJob(job.id)
        const finished = j.status === 'INDEXED' || j.status === 'FAILED'
        Object.assign(job, j)
        if (!finished) allDone = false
      } catch {
        // 单条轮询失败不视为完成，下一秒重试（保持弹窗不提前关闭）
        allDone = false
      }
    }
    if (allDone) {
      if (batchTimer) { clearInterval(batchTimer); batchTimer = null }
      if (batchFailCount.value === 0) {
        ElMessage.success(`全部 ${batchOkCount.value} 篇文档入库完成`)
      } else {
        ElMessage.warning(`${batchOkCount.value} 篇完成，${batchFailCount.value} 篇失败`)
      }
      load()
      // 完成后进度弹窗自动关闭（保留 1.5s 展示完成态）
      window.setTimeout(() => { jobDialog.value = false }, 1500)
    }
  }
  tick()
  batchTimer = window.setInterval(tick, 1000)
}

async function submit() {
  if (pendingFiles.value.length === 0) {
    ElMessage.warning('请先选择文件')
    return
  }
  uploading.value = true
  const jobs: IngestionJob[] = []
  let failed = 0
  try {
    // 按队列依次接入：逐个提交（顺序 await），全部提交完再统一轮询进度
    for (const file of pendingFiles.value) {
      try {
        const job = await uploadDocument(file, {
          title: '', // 多文件不共用标题，由各自文件名兜底
          doc_type: form.doc_type,
          issuer: form.issuer,
          doc_no: form.doc_no,
          security_level: form.security_level,
          permission_tags: form.permission_tags,
          space_id: form.space_id || undefined,
          chunk_strategy: form.chunk_strategy,
        })
        jobs.push(job)
      } catch (e: any) {
        failed += 1
        ElMessage.error(`「${file.name}」提交失败：${e?.response?.data?.detail || e.message}`)
      }
    }
    if (jobs.length === 0) return // 全部提交失败，留在对话框
    dialog.value = false
    if (failed > 0) {
      ElMessage.warning(`${failed} 个文件提交失败，其余 ${jobs.length} 个已开始入库`)
    } else {
      ElMessage.success(`已提交 ${jobs.length} 个文件，开始入库`)
    }
    startBatchPoll(jobs)
  } finally {
    uploading.value = false
  }
}

onBeforeUnmount(() => { if (batchTimer) clearInterval(batchTimer) })

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
  <div class="wrap page-fill">
    <!-- 单面板铺满内容区并内部滚动；空间卡片网格在少量卡片时保持顶部对齐 -->
    <div class="panel pad lib-panel">
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

      <!-- 空间所有权说明（用户 2026-10-14：归属规则具象化） -->
      <div class="own-banner">
        <span class="own-banner-icon">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" />
          </svg>
        </span>
        <span>
          <b>空间所有权：</b>你创建的空间<b>仅你可见、仅你可上传与检索</b>；第三方应用同理（其空间仅该应用可用）。
          <b>默认空间</b>为公共空间，所有人可见、可上传。
        </span>
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
            <el-tag :type="ownerTag(s).type" size="small" effect="dark">{{ ownerTag(s).text }}</el-tag>
            <el-tooltip v-if="!s.is_default" content="仅创建者可见、可上传、可检索" placement="top">
              <span class="sc-lock">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2">
                  <rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" />
                </svg>
              </span>
            </el-tooltip>
          </div>
          <div class="sc-desc text-muted">{{ s.description || '暂无描述' }}</div>
          <div class="sc-meta">
            <span>{{ s.doc_count }} 篇文档</span>
            <span>{{ s.chunk_count }} 切片</span>
            <span v-if="childrenOf(s.id).length">{{ childrenOf(s.id).length }} 个子空间</span>
            <span v-if="!s.is_default" class="sc-owner">所有者：{{ s.owner_name || '—' }}</span>
          </div>
          <div class="sc-ops" @click.stop>
            <el-button link type="primary" size="small" @click="enterSpace(s.id)">进入</el-button>
            <el-button link size="small" @click="openCreateSpace(s.id)">新建子空间</el-button>
            <el-button v-if="canManageSpace(s)" link size="small" @click="editSpace(s)">重命名</el-button>
            <el-button
              v-if="s.is_mine"
              link
              type="danger"
              size="small"
              @click="removeSpace(s)"
            >删除</el-button>
            <span v-if="!s.is_mine && !s.is_default" class="sc-readonly text-muted">只读（他人空间）</span>
            <span v-else-if="s.is_default && !isAdmin" class="sc-readonly text-muted">公共空间（仅管理员可改名）</span>
          </div>
        </div>
      </div>
      <el-empty v-else-if="!currentSpace" description="还没有文档空间，点击右上角「新建空间」开始（空间仅创建者可见）" :image-size="70" />

      <!-- 空间内：过滤 + 文档表格 -->
      <template v-if="currentSpace">
        <div class="filters">
          <el-input v-model="filters.q" placeholder="按标题/文号搜索" clearable style="width: 220px" @keyup.enter="load" @clear="load" />
          <el-select v-model="filters.doc_type" placeholder="文种" clearable style="width: 130px" @change="load">
            <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
          </el-select>
          <el-select v-model="filters.security_level" placeholder="密级" clearable style="width: 120px" @change="load">
            <el-option v-for="lv in SECURITY_LEVEL_OPTIONS" :key="lv.value" :label="lv.label" :value="lv.value" />
          </el-select>
          <el-button @click="load">查询</el-button>
        </div>

        <el-table :data="rows" v-loading="loading" style="width: 100%" empty-text="该空间暂无文档（点击「接入文档」上传）">
          <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
          <el-table-column label="所属空间" width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.space_name || '默认空间' }}</template>
          </el-table-column>
          <el-table-column prop="doc_type" label="文种" width="90" />
          <el-table-column label="密级" width="100">
            <template #default="{ row }"><span class="badge" :class="row.security_level">{{ secLabel(row.security_level) }}</span></template>
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
        drag multiple :auto-upload="false" :show-file-list="true"
        accept=".pdf,.docx,.doc,.ofd,.txt,.md"
        :file-list="fileList" :on-change="onFileChange" :on-remove="onFileRemove">
        <div class="up-tip">将 PDF / DOCX / OFD / TXT 文件拖到此处，或点击选择（支持一次拖拽多个，按队列依次接入）</div>
      </el-upload>
      <el-form :model="form" label-width="92px" style="margin-top: 14px">
        <el-form-item label="标题"><el-input v-model="form.title" placeholder="默认取文件名" /></el-form-item>
        <el-form-item label="文档空间">
          <el-select v-model="form.space_id" placeholder="默认空间（公共）" style="width: 300px">
            <el-option
              v-for="s in spaces"
              :key="s.id"
              :label="spaceOptionLabel(s)"
              :value="s.id"
            />
          </el-select>
          <span class="hint">仅显示你可用的空间：默认公共空间 + 本人空间（他人空间不可见、不可上传）</span>
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
            <el-option v-for="lv in SECURITY_LEVEL_OPTIONS" :key="lv.value" :label="lv.label" :value="lv.value" />
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
          <span v-if="dualGran" class="hint">
            双粒度：小块 {{ dualGran.small }} token（重叠 {{ dualGran.small_overlap }}）·
            大块 {{ dualGran.large }} token（重叠 {{ dualGran.large_overlap }}）
            <template v-if="!dualGran.enabled">（双粒度已关，回退单粒度 800 字符）</template>
            <br />由「配置中心 · 检索配置」统一管理，此处不再单独设置
            <br />页码/页眉清洗：{{ dualGran.noise_clean ? '开' : '关' }} ·
            段落重建：{{ dualGran.paragraph_rebuild ? '开' : '关' }}
          </span>
          <span v-else class="hint">由「配置中心 · 检索配置」统一管理</span>
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

    <!-- 入库进度（批量队列）对话框 -->
    <el-dialog v-model="jobDialog" title="入库进度" width="560px" :close-on-click-modal="false">
      <div v-if="batchJobs.length">
        <div v-for="job in batchJobs" :key="job.id" class="job">
          <div class="job-file">
            <span :class="{ fail: job.status === 'FAILED' }">{{ job.filename }}</span>
            <el-tag size="small" :type="job.status === 'INDEXED' ? 'success' : (job.status === 'FAILED' ? 'danger' : 'info')">
              {{ job.status }}
            </el-tag>
          </div>
          <el-progress
            :percentage="job.progress"
            :status="job.status === 'FAILED' ? 'exception' : (job.status === 'INDEXED' ? 'success' : '')"
            style="margin: 8px 0 4px" />
          <div v-if="job.total_chunks" class="job-meta text-muted">
            切片 {{ job.processed_chunks }} / {{ job.total_chunks }}
          </div>
          <div v-if="job.status === 'FAILED'" class="job-err">{{ job.error_message }}</div>
        </div>
        <div v-if="batchAllDone" class="batch-summary text-muted">
          全部完成：{{ batchOkCount }} 成功 / {{ batchFailCount }} 失败（弹窗将自动关闭）
        </div>
      </div>
    </el-dialog>

    <!-- 切片详情抽屉 -->
    <el-drawer v-model="drawer" title="文档切片" size="46%">
      <div v-loading="detailLoading">
        <div v-if="detail">
          <div class="d-head">
            <div class="d-title serif">{{ detail.title }}</div>
            <div class="text-muted">
              <span class="badge" :class="detail.security_level">{{ secLabel(detail.security_level) }}</span>
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
/* 页面根：flex column（由 .page-fill 提供），主面板弹性铺满并内部滚动，
 * 消除早期"面板短、下方大片裸背景"的怪异观感 */
.wrap { gap: 0; }
.lib-panel { flex: 1; min-height: 0; overflow: auto; }
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

/* 空间所有权说明横幅（2026-10-14） */
.own-banner {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 9px 12px; margin-bottom: 14px;
  border: 1px solid var(--brand-line); border-radius: 8px;
  background: var(--brand-soft);
  font-size: 12px; line-height: 1.7; color: var(--text);
}
.own-banner b { color: var(--text-strong); }
.own-banner-icon { color: var(--brand); flex-shrink: 0; margin-top: 2px; }

/* 空间卡片：所有权徽章 / 锁标 / 所有者 / 只读提示 */
.sc-lock { display: inline-flex; color: var(--text-faint); flex-shrink: 0; }
.sc-owner { color: var(--text-faint); }
.sc-readonly { font-size: 12px; align-self: center; }

.badge { color: var(--text); }
.st { color: var(--text-muted); font-size: 13px; }
.st.ok { color: var(--c-success); }
.up-tip { color: var(--text-muted); padding: 18px 0; }
.hint { color: var(--text-muted); font-size: 12px; margin-left: 10px; }
.job-file { color: var(--text-strong); display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.job-file .fail { color: var(--c-danger); }
.job-meta { display: flex; gap: 18px; margin-top: 10px; font-size: 13px; }
.batch-summary { margin-top: 12px; font-size: 13px; }
.job-err { color: var(--c-danger); font-size: 12px; margin-top: 8px; word-break: break-all; }
.d-head { margin-bottom: 14px; }
.d-title { font-size: 17px; color: var(--text-strong); margin-bottom: 6px; }
.chunk { padding: 12px 14px; margin-bottom: 10px; }
.c-head { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 12px; margin-bottom: 6px; }
.c-body { color: var(--text); line-height: 1.7; white-space: pre-wrap; font-size: 13px; }
</style>
