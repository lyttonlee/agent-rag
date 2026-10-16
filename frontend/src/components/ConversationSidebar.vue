<script setup lang="ts">
/**
 * 会话历史侧栏（智能问答 / 公文起草共用）。
 *
 * 职责：会话列表加载、新建、切换、重命名、删除，以及**历史搜索**
 * （2026-10-15 用户需求）——支持两种定位形态：
 *   1. 关键字模糊搜索：匹配标题与消息内容，命中消息时展示高亮片段；
 *   2. 明确 ID 精确查找：输入形如会话 ID 的整串（32 位十六进制，
 *      uuid4().hex），直达某条历史；不存在时给出友好空态（后端 404）。
 *      粘贴带前后缀的文本（如「会话ID：xxx」）也能提取其中的 ID。
 * 每一条历史都可一键复制会话 ID（明确 ID 搜索的前提：ID 可发现）。
 * 搜索态下 load() 仍保持搜索结果（新消息到达不把用户踢出搜索态）。
 *
 * 不关心消息内容本身（由各页面自行渲染），只做"会话级"管理，
 * 保证问答与起草两侧的历史交互完全一致（样式与行为同源）。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import {
  deleteConversation,
  listConversations,
  updateConversation,
  type Conversation,
  type ConvType,
} from '@/api/client'

const props = defineProps<{
  convType: ConvType
  activeId: string | null
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'new'): void
  (e: 'deleted', id: string): void
}>()

const rows = ref<Conversation[]>([])
const loading = ref(false)
const renaming = ref<Conversation | null>(null)
const renameText = ref('')

// ---- 历史搜索 ----
const searchText = ref('')
const searching = ref(false)   // 是否处于搜索态（有查询词）
const idNotFound = ref('')     // 明确 ID 未命中时记录，供空态提示
let searchTimer: number | undefined

/**
 * 提取会话 ID：32 位十六进制（uuid4().hex，本系统会话 ID 的实际格式）。
 * 整串精确匹配 → 直接用；粘贴带前后缀的文本（如「会话ID：xxx」/带引号）
 * → 提取其中的 ID；其余一律按关键字模糊搜索——不用"≥8 位字母数字"
 * 这类宽规则，避免把 Training2026 这种英文关键字误判成 ID 而扑空
 * （用户 2026-10-16 反馈 ID 搜索体验问题后的收敛）。
 */
function extractConversationId(text: string): string | null {
  const m = text.match(/[0-9a-fA-F]{32}/)
  return m ? m[0].toLowerCase() : null
}

async function load() {
  // 搜索激活时刷新保持搜索结果（避免新消息到达把用户踢出搜索态）
  if (searching.value && searchText.value.trim()) {
    await runSearch()
    return
  }
  loading.value = true
  try {
    rows.value = await listConversations(props.convType)
  } catch {
    // 静默：侧栏加载失败不影响主对话区
  } finally {
    loading.value = false
  }
}

/** 执行一次搜索（提取到 32 位 ID 走精确查找，其余走关键字模糊搜索） */
async function runSearch() {
  const text = searchText.value.trim()
  if (!text) {
    clearSearch()
    return
  }
  searching.value = true
  idNotFound.value = ''
  loading.value = true
  const convId = extractConversationId(text)
  try {
    rows.value = convId
      ? await listConversations(props.convType, { id: convId })
      : await listConversations(props.convType, { keyword: text })
  } catch {
    // 明确 ID 不存在 → 后端 404：转友好空态，不弹 toast 打断输入
    rows.value = []
    if (convId) idNotFound.value = convId
  } finally {
    loading.value = false
  }
}

/** 输入防抖 350ms 自动搜索（避免每击一键都打接口） */
function onSearchInput() {
  if (searchTimer) window.clearTimeout(searchTimer)
  const text = searchText.value.trim()
  if (!text) {
    clearSearch()
    return
  }
  searchTimer = window.setTimeout(runSearch, 350)
}

/** 回车立即搜索（不等防抖） */
function onSearchEnter() {
  if (searchTimer) window.clearTimeout(searchTimer)
  runSearch()
}

/** 清空搜索：恢复常规列表 */
function clearSearch() {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchText.value = ''
  searching.value = false
  idNotFound.value = ''
  load()
}

/** HTML 转义（v-html 高亮前的安全底线——片段来自本库内容，仍先转义） */
function escHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string
  ))
}

/** 复制会话 ID 到剪贴板（明确 ID 搜索的前提：ID 可发现、可复制） */
async function copyId(row: Conversation, ev: MouseEvent) {
  ev.stopPropagation() // 不触发条目选中
  try {
    await navigator.clipboard.writeText(row.id)
    ElMessage.success('会话 ID 已复制，可粘贴到搜索框精确查找')
  } catch {
    // 剪贴板不可用（非安全上下文等）：回退展示 ID 供手动复制
    ElMessage.info(`复制失败，请手动复制：${row.id}`)
  }
}

/** 命中关键字高亮（大小写不敏感；正则特殊字符先转义） */
function highlight(text: string): string {
  const kw = searchText.value.trim()
  if (!kw || !text) return escHtml(text || '')
  const pattern = kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return escHtml(text).replace(
    new RegExp(pattern, 'gi'),
    (m) => `<mark>${m}</mark>`,
  )
}

onMounted(load)
defineExpose({ load })

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / MM-DD */
function relTime(iso: string | null): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const diff = Date.now() - then
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  const d = new Date(iso)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function startRename(row: Conversation) {
  renaming.value = row
  renameText.value = row.title
}

async function confirmRename() {
  if (!renaming.value) return
  const text = renameText.value.trim()
  if (!text) {
    ElMessage.warning('会话标题不能为空')
    return
  }
  try {
    await updateConversation(renaming.value.id, { title: text })
    renaming.value = null
    await load()
  } catch (e: any) {
    ElMessage.error('重命名失败：' + (e?.response?.data?.detail || e.message))
  }
}

async function remove(row: Conversation) {
  try {
    await ElMessageBox.confirm(`删除会话「${row.title}」及其全部消息？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  try {
    await deleteConversation(row.id)
    ElMessage.success('已删除')
    await load()
    emit('deleted', row.id)
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.response?.data?.detail || e.message))
  }
}

/** 新建会话：先退出搜索态，保证新会话出现在常规列表里 */
function startNew() {
  clearSearch()
  emit('new')
}
</script>

<template>
  <aside class="conv-side">
    <div class="conv-head">
      <span class="conv-ttl">历史会话</span>
      <el-button size="small" type="primary" @click="startNew">新建</el-button>
    </div>
    <div class="conv-search">
      <el-input
        v-model="searchText"
        placeholder="搜索标题 / 内容 / 会话 ID"
        clearable
        size="small"
        :prefix-icon="Search"
        @input="onSearchInput"
        @keyup.enter="onSearchEnter"
        @clear="clearSearch"
      />
    </div>
    <div v-if="searching" class="conv-search-info text-muted">
      <span v-if="rows.length">“{{ searchText }}” 命中 {{ rows.length }} 条</span>
      <span v-else-if="idNotFound">未找到 ID 为 {{ idNotFound }} 的会话</span>
      <span v-else>未找到与 “{{ searchText }}” 相关的会话</span>
    </div>
    <div class="conv-list" v-loading="loading">
      <div
        v-for="c in rows"
        :key="c.id"
        class="conv-item"
        :class="{ active: c.id === activeId }"
        @click="emit('select', c.id)"
      >
        <template v-if="renaming && renaming.id === c.id">
          <el-input
            v-model="renameText"
            size="small"
            @click.stop
            @keyup.enter="confirmRename"
            @keyup.esc="renaming = null"
          />
          <div class="rename-ops" @click.stop>
            <el-button size="small" type="primary" @click="confirmRename">保存</el-button>
            <el-button size="small" @click="renaming = null">取消</el-button>
          </div>
        </template>
        <template v-else>
          <div class="conv-title" :title="c.title">{{ c.title }}</div>
          <!-- 搜索命中片段（关键字高亮；仅 keyword 命中消息内容时存在） -->
          <div
            v-if="searching && c.match_snippet"
            class="conv-snippet"
            v-html="highlight(c.match_snippet)"
          />
          <div class="conv-meta">
            <span>{{ c.message_count }} 条</span>
            <span>{{ relTime(c.last_message_at || c.created_at) }}</span>
          </div>
          <div class="conv-ops" @click.stop>
            <el-button link type="primary" size="small" @click="copyId(c, $event)">复制ID</el-button>
            <el-button link type="primary" size="small" @click="startRename(c)">重命名</el-button>
            <el-button link type="danger" size="small" @click="remove(c)">删除</el-button>
          </div>
        </template>
      </div>
      <div v-if="!loading && !rows.length && !searching" class="conv-empty text-muted">
        暂无历史会话，点击「新建」开始
      </div>
    </div>
  </aside>
</template>

<style scoped>
.conv-side {
  width: 232px; flex-shrink: 0; display: flex; flex-direction: column;
  background: var(--bg-shell); border-right: 1px solid var(--line); border-radius: var(--radius);
}
.conv-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; border-bottom: 1px solid var(--line);
}
.conv-ttl { font-size: 14px; color: var(--text-strong); font-weight: 600; }
.conv-search { padding: 8px 8px 0; }
.conv-search-info { padding: 6px 12px 2px; font-size: 11px; line-height: 1.4; }
.conv-list { flex: 1; overflow: auto; padding: 8px; }
.conv-item {
  padding: 10px 12px; border-radius: var(--radius-sm); cursor: pointer;
  border: 1px solid transparent; margin-bottom: 4px;
}
.conv-item:hover { background: var(--bg-surface-2); }
.conv-item.active { background: var(--brand-soft); border-color: var(--brand-line); }
.conv-title {
  color: var(--text); font-size: 13px; line-height: 1.4;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.conv-snippet {
  margin-top: 3px; font-size: 11px; line-height: 1.45; color: var(--text-muted);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-all;
}
.conv-snippet :deep(mark) {
  background: var(--brand-soft); color: var(--brand);
  padding: 0 1px; border-radius: 2px;
}
.conv-meta {
  display: flex; justify-content: space-between; margin-top: 4px;
  color: var(--text-faint); font-size: 11px;
}
.conv-ops { display: flex; gap: 4px; margin-top: 6px; opacity: 0; transition: opacity 0.15s; }
.conv-item:hover .conv-ops { opacity: 1; }
.rename-ops { display: flex; gap: 6px; margin-top: 6px; }
.conv-empty { padding: 24px 8px; text-align: center; font-size: 12px; }
</style>
