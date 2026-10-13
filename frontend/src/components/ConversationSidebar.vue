<script setup lang="ts">
/**
 * 会话历史侧栏（智能问答 / 公文起草共用）。
 *
 * 职责：会话列表加载、新建、切换、重命名、删除。
 * 不关心消息内容本身（由各页面自行渲染），只做"会话级"管理，
 * 保证问答与起草两侧的历史交互完全一致（样式与行为同源）。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
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

async function load() {
  loading.value = true
  try {
    rows.value = await listConversations(props.convType)
  } catch {
    // 静默：侧栏加载失败不影响主对话区
  } finally {
    loading.value = false
  }
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
</script>

<template>
  <aside class="conv-side">
    <div class="conv-head">
      <span class="conv-ttl">历史会话</span>
      <el-button size="small" type="primary" @click="emit('new')">新建</el-button>
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
          <div class="conv-meta">
            <span>{{ c.message_count }} 条</span>
            <span>{{ relTime(c.last_message_at || c.created_at) }}</span>
          </div>
          <div class="conv-ops" @click.stop>
            <el-button link type="primary" size="small" @click="startRename(c)">重命名</el-button>
            <el-button link type="danger" size="small" @click="remove(c)">删除</el-button>
          </div>
        </template>
      </div>
      <div v-if="!loading && !rows.length" class="conv-empty text-muted">
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
.conv-meta {
  display: flex; justify-content: space-between; margin-top: 4px;
  color: var(--text-faint); font-size: 11px;
}
.conv-ops { display: flex; gap: 4px; margin-top: 6px; opacity: 0; transition: opacity 0.15s; }
.conv-item:hover .conv-ops { opacity: 1; }
.rename-ops { display: flex; gap: 6px; margin-top: 6px; }
.conv-empty { padding: 24px 8px; text-align: center; font-size: 12px; }
</style>
