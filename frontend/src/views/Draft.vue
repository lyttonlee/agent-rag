<script setup lang="ts">
/**
 * 智能起草页：按文种模板生成公文草稿（SSE 流式 + 会话历史）。
 *
 * 多轮修改机制：首次起草自动创建 draft 会话（conversation_id 由后端
 * meta 事件回传）；后续在同一会话中输入修改意见（如"语气更严厉一些"），
 * 后端将该轮 user 消息落库，历史在左侧会话栏可回看。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConversationSidebar from '@/components/ConversationSidebar.vue'
import AgentStepPanel from '@/components/AgentStepPanel.vue'
import ReasoningPanel from '@/components/ReasoningPanel.vue'
import {
  caller,
  getConversation,
  intentLabelOf,
  streamGenerate,
  type ConversationDetail,
  type ConversationMessage,
  type CitationPayload,
} from '@/api/client'

const docType = ref('通知')
const topic = ref('关于开展第三季度安全生产大检查的通知')
const loading = ref(false)
const answer = ref('')
const reasoning = ref('')
const citations = ref<CitationPayload[]>([])
const hits = ref<string[]>([])
const modelName = ref('')
const elapsed = ref(0)
let timer: number | null = null
let controller: AbortController | null = null
let startedAt = 0

// ---- Agent 状态（意图识别 + 无材料兜底甄别声明）----
const intentLabel = ref('')
const intentKey = ref('')
const noResult = ref(false)
const disclaimer = ref(false)
/** 有 Agent 信息时展示面板（流式中 / 结束后保留到下次起草） */
const showAgentPanel = computed(
  () => loading.value || !!intentLabel.value || noResult.value || disclaimer.value,
)

// ---- 会话（多轮修改）----
const sidebar = ref<InstanceType<typeof ConversationSidebar> | null>(null)
const activeId = ref<string | null>(null)
const activeTitle = ref('新会话')
const history = ref<ConversationMessage[]>([])
const historyLoading = ref(false)
const hasHistory = computed(() => history.value.length > 0)

function startTimer() {
  startedAt = Date.now()
  elapsed.value = 0
  timer = window.setInterval(() => { elapsed.value = Math.floor((Date.now() - startedAt) / 100) / 10 }, 100)
}
function stopTimer() {
  if (timer) { clearInterval(timer); timer = null }
}

async function openConversation(id: string) {
  if (loading.value) controller?.abort()
  activeId.value = id
  historyLoading.value = true
  try {
    const detail: ConversationDetail = await getConversation(id)
    activeTitle.value = detail.title
    history.value = detail.messages
  } catch (e: any) {
    ElMessage.error('加载会话失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    historyLoading.value = false
  }
}

function newConversation() {
  if (loading.value) controller?.abort()
  activeId.value = null
  activeTitle.value = '新会话'
  history.value = []
  answer.value = ''
}

/** 拉取会话最新消息（done / stop 后刷新历史） */
async function refreshHistory() {
  if (!activeId.value) return
  try {
    const detail = await getConversation(activeId.value)
    activeTitle.value = detail.title
    history.value = detail.messages
  } catch {
    // 刷新失败不打扰用户（主内容已在页面渲染）
  }
  sidebar.value?.load()
}

/** 流式生成：SSE 实时出字（meta → token* → done），思考链折叠展示。 */
async function generate() {
  if (!topic.value.trim()) {
    ElMessage.warning('请输入起草主题')
    return
  }
  loading.value = true
  answer.value = ''
  reasoning.value = ''
  citations.value = []
  hits.value = []
  // 重置本轮 Agent 展示状态
  intentLabel.value = ''
  intentKey.value = ''
  noResult.value = false
  disclaimer.value = false
  startTimer()

  controller = streamGenerate(
    {
      query: topic.value,
      template_doc_type: docType.value,
      conversation_id: activeId.value ?? undefined,
      caller_scope: caller.caller_scope,
      max_clearance: caller.max_clearance,
      rerank: false,
    },
    {
      onMeta: (model, cits, _truncated, conversationId, meta) => {
        modelName.value = model
        citations.value = cits
        // 首轮：后端自动创建起草会话并回传 ID，后续修改轮次复用
        if (conversationId && conversationId !== activeId.value) {
          activeId.value = conversationId
        }
        // Agent 意图信息（老后端无此字段时保持空）
        if (meta) {
          intentLabel.value = meta.intent_label || ''
          intentKey.value = meta.intent || ''
          noResult.value = !!meta.no_result
        }
      },
      onReasoning: (text) => {
        reasoning.value += text
        // 思考面板的自动展开/收起由组件内部按 streaming 状态自行处理
      },
      onToken: (text) => {
        answer.value += text
      },
      onDone: (finalAnswer, sensitiveHits, _model, extra) => {
        // 以服务端脱敏后的完整答案为准（流式片段未脱敏）
        answer.value = finalAnswer
        hits.value = sensitiveHits
        if (extra) {
          disclaimer.value = !!extra.disclaimer
          noResult.value = !!extra.no_result
          if (extra.intent && !intentKey.value) intentKey.value = extra.intent
        }
        loading.value = false
        stopTimer()
        // 落库完成：刷新会话历史（meta 事件已回传 conversation_id 到 activeId）
        refreshHistory()
        if (sensitiveHits.length) {
          ElMessage.warning(`敏感词命中 ${sensitiveHits.length} 处，已脱敏`)
        }
      },
      onError: (message) => {
        ElMessage.error('生成失败：' + message)
        refreshHistory()
        loading.value = false
        stopTimer()
      },
    },
  )
}

function stop() {
  controller?.abort()
  controller = null
  loading.value = false
  stopTimer()
  // 后端已保存部分答案，刷新历史显示
  refreshHistory()
  ElMessage.info('已停止生成，已生成部分已保留在会话中')
}

function onDeleted(id: string) {
  if (activeId.value === id) newConversation()
}

onBeforeUnmount(() => {
  controller?.abort()
  stopTimer()
})

async function exportDocx() {
  // 直接触发文件下载（后端 /v1/export/docx 返回 FileResponse）
  const r = await fetch('/v1/export/docx', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: topic.value,
      template_doc_type: docType.value,
      caller_scope: caller.caller_scope,
      max_clearance: caller.max_clearance,
    }),
  })
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${topic.value.slice(0, 20)}.docx`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="draft-wrap">
    <!-- 左：会话历史（多轮修改） -->
    <ConversationSidebar
      ref="sidebar"
      :conv-type="'draft'"
      :active-id="activeId"
      @select="openConversation"
      @new="newConversation"
      @deleted="onDeleted"
    />

    <!-- 右：起草区 -->
    <div class="panel pad draft-main">
      <div class="head">
        <div class="ttl serif">智能起草</div>
        <span class="badge INTERNAL">按文种模板 · 引用溯源 · 流式输出</span>
        <div class="spacer" />
        <span class="text-muted" style="font-size: 12px">当前会话：{{ activeTitle }}</span>
      </div>
      <div class="bar">
        <el-select v-model="docType" style="width: 140px">
          <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
        </el-select>
        <el-input v-model="topic" placeholder="起草主题 / 修改意见（如：语气更严厉）" style="flex: 1" @keyup.enter="generate" />
        <el-button type="primary" :loading="loading" @click="generate">{{ loading ? '生成中…' : '起草' }}</el-button>
        <el-button v-if="loading" @click="stop">停止</el-button>
        <el-button :disabled="!answer" @click="exportDocx">导出 docx</el-button>
      </div>

      <!-- 流式状态条：模型 / 耗时 -->
      <div v-if="loading || reasoning" class="stream-bar">
        <span v-if="loading" class="live"><i class="dot" />流式生成中</span>
        <span v-if="loading" class="mono text-muted">{{ elapsed.toFixed(1) }}s</span>
        <span v-if="modelName" class="text-muted">模型 {{ modelName }}</span>
      </div>
      <!-- 思考过程：默认折叠、可展开查看；回答结束后不消失 -->
      <ReasoningPanel
        v-if="reasoning"
        :text="reasoning"
        :streaming="loading && !answer"
      />

      <!-- Agent 执行过程（意图 + 无材料兜底提示；单步起草无拆解步骤） -->
      <AgentStepPanel
        v-if="showAgentPanel"
        :intent="intentKey"
        :intent-label="intentLabel"
        :steps="[]"
        :loading="loading"
        :no-result="noResult"
        :disclaimer="disclaimer"
      />

      <div class="paper panel">
        <div class="red">{{ docType }}</div>
        <p class="body">{{ answer || '在上方输入主题并点击「起草」，系统将基于知识库检索材料生成带引用的公文草稿，正文实时流式输出。在同一会话中继续输入修改意见即可多轮完善。' }}<span v-if="loading" class="cursor" /></p>
        <div v-if="citations.length" class="refs">
          <div class="ttl">参考资料（{{ citations.length }}）</div>
          <div v-for="(c, i) in citations" :key="c.chunk_id">[{{ i + 1 }}] 《{{ c.title }}》</div>
        </div>
        <div v-if="hits.length" class="warn-chip">敏感词命中：{{ hits.join('、') }}（已脱敏）</div>
        <div v-if="disclaimer" class="warn-chip">提示：本次未检索到参考材料，文稿由 AI 通用能力生成，请自行甄别后使用</div>
      </div>

      <!-- 历史轮次（当前会话的往轮问答，只读时间线） -->
      <div v-if="hasHistory" class="draft-history" v-loading="historyLoading">
        <div class="hist-ttl text-muted">本轮会话历史（{{ history.length }} 条）</div>
        <div v-for="m in history" :key="m.id" class="hist-item">
          <span class="hist-role" :class="m.role">{{ m.role === 'user' ? '要求' : '草稿' }}</span>
          <span v-if="m.role === 'assistant' && m.intent" class="intent-badge" :title="`本轮意图：${intentLabelOf(m.intent)}`">
            {{ intentLabelOf(m.intent) }}
          </span>
          <div class="hist-content">{{ m.content.length > 120 ? m.content.slice(0, 120) + '…' : m.content }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.draft-wrap { display: flex; gap: var(--gap); height: 100%; }
.draft-main { flex: 1; overflow: auto; min-width: 0; }
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.ttl { font-size: 18px; color: var(--text-strong); }
.bar { display: flex; gap: 10px; margin-bottom: 14px; }
.stream-bar { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; font-size: 13px; }
.live { display: inline-flex; align-items: center; gap: 6px; color: var(--c-success); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c-success); animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
.cursor { display: inline-block; width: 2px; height: 1em; background: var(--brand); margin-left: 2px; vertical-align: text-bottom; animation: blink 0.8s infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.paper { padding: 28px; max-width: 820px; margin: 0 auto; background: #f7f4ec; color: #1c1c1c; }
.red { color: #c0392b; font-family: var(--font-serif); font-size: 22px; text-align: center; font-weight: 700; margin-bottom: 16px; }
.body { line-height: 1.9; white-space: pre-wrap; min-height: 200px; }
.refs { margin-top: 20px; border-top: 1px solid #d8d2c4; padding-top: 10px; font-size: 13px; color: #444; }
.refs .ttl { font-weight: 600; margin-bottom: 6px; }
.warn-chip { margin-top: 12px; color: #c0392b; font-size: 13px; }
.draft-history { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 12px; }
.hist-ttl { font-size: 12px; margin-bottom: 8px; }
.hist-item { display: flex; gap: 10px; padding: 6px 0; font-size: 13px; align-items: baseline; }
.hist-role {
  flex-shrink: 0; font-size: 11px; padding: 1px 8px; border-radius: 999px;
  border: 1px solid var(--line-strong); color: var(--text-muted);
}
.hist-role.user { color: var(--brand); border-color: var(--brand-line); }
.intent-badge {
  flex-shrink: 0;
  padding: 0 6px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 18px;
  color: var(--brand);
  background: var(--brand-soft);
  border: 1px solid var(--brand-line);
}
.hist-content { color: var(--text-muted); line-height: 1.6; }
</style>
