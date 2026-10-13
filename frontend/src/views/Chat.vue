<script setup lang="ts">
/**
 * 智能问答页：基于知识文档回答问题（多轮会话 + SSE 流式）。
 *
 * 与「智能检索问答」（/retrieve，检索调试视图）的分工：
 * - 本页是面向用户的知识问答：会话历史、多轮追问、流式出字、引用可点开；
 * - 检索页保留给运维调试（原始召回结果观察）。
 *
 * 多轮机制：首轮提问时 conversation_id 为空，后端自动创建会话并在 meta
 * 事件回传 ID；后续轮次携带该 ID，后端自动拼接历史上下文。点「停止」
 * 时后端同样保存已生成部分（GeneratorExit 分支），刷新即可看到。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConversationSidebar from '@/components/ConversationSidebar.vue'
import AgentStepPanel from '@/components/AgentStepPanel.vue'
import {
  caller,
  getConversation,
  listSpaces,
  streamChat,
  updateConversation,
  type ConversationDetail,
  type ConversationMessage,
  type CitationPayload,
  type DocSpace,
  type PlanStep,
} from '@/api/client'

// ---- 知识空间（问答范围多选）----
const spaces = ref<DocSpace[]>([])
const selectedSpaces = ref<string[]>([])

// ---- 会话状态 ----
const sidebar = ref<InstanceType<typeof ConversationSidebar> | null>(null)
const activeId = ref<string | null>(null)
const activeTitle = ref('新会话')
const messages = ref<ConversationMessage[]>([])
const historyLoading = ref(false)

// ---- 流式状态 ----
const input = ref('')
const sentQuery = ref('')
const loading = ref(false)
const reasoning = ref('')
const showReasoning = ref(false)
const answer = ref('')
const citations = ref<CitationPayload[]>([])
const hits = ref<string[]>([])
const modelName = ref('')
let controller: AbortController | null = null
const listRef = ref<HTMLElement | null>(null)

// ---- Agent 状态（意图路由 + 分步执行过程可见）----
const intentLabel = ref('')
const intentKey = ref('')
const turnSteps = ref<PlanStep[]>([])
const noResult = ref(false)
const disclaimer = ref(false)
/** 本轮 Agent 信息是否展示（流式中或流式结束后到下一次发送前） */
const showAgentPanel = computed(
  () => loading.value || !!intentLabel.value || turnSteps.value.length > 0 || noResult.value || disclaimer.value,
)

const hasMessages = computed(() => messages.value.length > 0 || loading.value)

/** 示例问题（空态引导） */
const examples = [
  '跨网传输系统的审批策略是怎样的？',
  '公文起草对字体和格式有什么要求？',
  '入库失败的任务如何排查？',
]

onMounted(async () => {
  try {
    spaces.value = await listSpaces()
  } catch {
    ElMessage.error('加载知识空间失败')
  }
  // 默认展示最近一次会话（列表已按最后活跃倒序）
  await sidebar.value?.load()
})

async function openConversation(id: string) {
  if (loading.value) controller?.abort()
  activeId.value = id
  historyLoading.value = true
  try {
    const detail: ConversationDetail = await getConversation(id)
    activeTitle.value = detail.title
    messages.value = detail.messages
    // 会话绑定的空间同步到范围选择器
    selectedSpaces.value = detail.space_ids || []
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
  messages.value = []
  answer.value = ''
  sentQuery.value = ''
  reasoning.value = ''
  citations.value = []
  hits.value = []
  resetAgentTurn()
}

/** 重置本轮 Agent 展示状态（新发送 / 新会话时调用） */
function resetAgentTurn() {
  intentLabel.value = ''
  intentKey.value = ''
  turnSteps.value = []
  noResult.value = false
  disclaimer.value = false
}

/** 切换知识范围：持久化到会话绑定（换范围继续聊） */
async function onSpacesChange(val: string[]) {
  selectedSpaces.value = val
  if (!activeId.value) return
  try {
    await updateConversation(activeId.value, { space_ids: val })
  } catch (e: any) {
    ElMessage.error('更新会话空间失败：' + (e?.response?.data?.detail || e.message))
  }
}

function scrollBottom() {
  nextTick(() => {
    const el = listRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function stop() {
  controller?.abort()
  controller = null
  loading.value = false
  // 后端已保存部分答案；重新拉取会话，把落库的部分答案渲染出来
  if (activeId.value) {
    getConversation(activeId.value)
      .then((d) => {
        messages.value = d.messages
      })
      .catch(() => undefined)
  }
  answer.value = ''
  reasoning.value = ''
  ElMessage.info('已停止，已生成部分已保留在会话中')
}

function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  if (loading.value) return

  loading.value = true
  sentQuery.value = q
  answer.value = ''
  reasoning.value = ''
  citations.value = []
  hits.value = []
  showReasoning.value = false
  resetAgentTurn()
  input.value = ''
  scrollBottom()

  controller = streamChat(
    {
      query: q,
      conversation_id: activeId.value ?? undefined,
      space_ids: selectedSpaces.value,
      caller_scope: caller.caller_scope,
      max_clearance: caller.max_clearance,
      rerank: true,
    },
    {
      onMeta: (conversationId, model, cits, _truncated, meta) => {
        // 首轮：后端自动创建会话并回传 ID，后续轮次复用
        if (conversationId && conversationId !== activeId.value) {
          activeId.value = conversationId
        }
        modelName.value = model
        citations.value = cits
        // Agent 意图信息（meta 可能来自老后端，缺省时保持空）
        if (meta) {
          intentLabel.value = meta.intent_label || ''
          intentKey.value = meta.intent || ''
          noResult.value = !!meta.no_result
        }
        scrollBottom()
      },
      onPlan: (steps) => {
        // 复杂任务计划到达：渲染步骤时间线（过程可见）
        turnSteps.value = steps.map((s) => ({ ...s, status: s.status || 'pending' }))
        scrollBottom()
      },
      onStepStart: (step) => {
        const target = turnSteps.value.find((s) => s.id === step.id)
        if (target) {
          target.status = 'running'
        } else {
          turnSteps.value.push({ ...step, status: 'running' })
        }
        scrollBottom()
      },
      onStepEnd: (stepId, ok, summary) => {
        const target = turnSteps.value.find((s) => s.id === stepId)
        if (target) {
          target.status = ok ? 'done' : 'failed'
          target.summary = summary
        }
        scrollBottom()
      },
      onReasoning: (t) => {
        reasoning.value += t
        showReasoning.value = true
        scrollBottom()
      },
      onToken: (t) => {
        answer.value += t
        showReasoning.value = false
        scrollBottom()
      },
      onDone: (finalAnswer, sensitiveHits, model, conversationId, extra) => {
        // 以服务端脱敏后的完整答案为准（流式片段未脱敏）
        answer.value = finalAnswer
        hits.value = sensitiveHits
        modelName.value = model
        if (conversationId) activeId.value = conversationId
        if (extra) {
          disclaimer.value = !!extra.disclaimer
          noResult.value = !!extra.no_result
          if (extra.intent && !intentKey.value) intentKey.value = extra.intent
        }
        // 落库完成：整页刷新历史（含本轮 user/assistant 消息）
        if (activeId.value) {
          getConversation(activeId.value)
            .then((d) => {
              activeTitle.value = d.title
              messages.value = d.messages
            })
            .catch(() => undefined)
        }
        sidebar.value?.load()
        loading.value = false
        if (sensitiveHits.length) {
          ElMessage.warning(`敏感词命中 ${sensitiveHits.length} 处，已脱敏`)
        }
      },
      onError: (message) => {
        ElMessage.error('问答失败：' + message)
        // 失败也刷新一次：后端会保留已生成部分与用户问题
        if (activeId.value) {
          getConversation(activeId.value)
            .then((d) => {
              messages.value = d.messages
            })
            .catch(() => undefined)
        }
        loading.value = false
      },
    },
  )
}

function onDeleted(id: string) {
  if (activeId.value === id) newConversation()
}

onBeforeUnmount(() => {
  controller?.abort()
})
</script>

<template>
  <div class="chat-wrap">
    <!-- 左：会话历史 -->
    <ConversationSidebar
      ref="sidebar"
      :conv-type="'chat'"
      :active-id="activeId"
      @select="openConversation"
      @new="newConversation"
      @deleted="onDeleted"
    />

    <!-- 右：对话区 -->
    <section class="chat-main panel">
      <header class="chat-bar">
        <div class="ttl serif" :title="activeTitle">{{ activeTitle }}</div>
        <div class="spacer" />
        <span class="text-muted bar-label">知识范围</span>
        <el-select
          :model-value="selectedSpaces"
          multiple
          collapse-tags
          collapse-tags-tooltip
          placeholder="全部空间"
          style="width: 220px"
          @change="onSpacesChange"
        >
          <el-option
            v-for="s in spaces"
            :key="s.id"
            :label="`${s.name}（${s.doc_count} 篇）`"
            :value="s.id"
          />
        </el-select>
        <span v-if="modelName" class="text-muted mono">模型 {{ modelName }}</span>
      </header>

      <!-- 消息流 -->
      <div ref="listRef" class="chat-list" v-loading="historyLoading">
        <!-- 空态 -->
        <div v-if="!hasMessages" class="empty">
          <div class="empty-ttl serif">智能问答</div>
          <p class="text-muted">
            基于知识库文档回答问题，回答附带引用来源；支持多轮追问与历史会话。
          </p>
          <div class="examples">
            <el-button
              v-for="ex in examples"
              :key="ex"
              round
              size="small"
              @click="send(ex)"
            >{{ ex }}</el-button>
          </div>
        </div>

        <!-- 历史消息 -->
        <template v-for="(m, i) in messages" :key="m.id">
          <div v-if="m.role === 'user'" class="msg user">
            <div class="bubble">{{ m.content }}</div>
          </div>
          <div v-else class="msg assistant">
            <div class="bubble panel">{{ m.content }}</div>
            <div v-if="m.citations?.length" class="msg-refs">
              <span class="ref-label">引用 {{ m.citations.length }} 条</span>
              <el-tag
                v-for="(c, ci) in m.citations"
                :key="c.chunk_id || ci"
                size="small"
                type="info"
                effect="dark"
                class="ref-tag"
                :title="c.snippet"
              >[{{ ci + 1 }}] {{ c.title }}</el-tag>
            </div>
            <div v-if="m.sensitive_hits?.length" class="msg-hits">
              敏感词命中：{{ m.sensitive_hits.join('、') }}（已脱敏）
            </div>
          </div>
        </template>

        <!-- 流式中的回答 -->
        <template v-if="loading">
          <div class="msg user">
            <div class="bubble">{{ sentQuery }}</div>
          </div>
          <div class="msg assistant">
            <div v-if="showReasoning && reasoning" class="reasoning">
              {{ reasoning }}<span class="cursor" />
            </div>
            <div class="bubble panel">
              {{ answer }}<span v-if="answer" class="cursor" />
              <span v-if="!answer" class="thinking">正在思考…</span>
            </div>
            <div v-if="citations.length" class="msg-refs">
              <span class="ref-label">引用 {{ citations.length }} 条</span>
              <el-tag
                v-for="(c, ci) in citations"
                :key="c.chunk_id || ci"
                size="small"
                type="info"
                effect="dark"
                class="ref-tag"
                :title="c.snippet"
              >[{{ ci + 1 }}] {{ c.title }}</el-tag>
            </div>
            <div v-if="hits.length" class="msg-hits">
              敏感词命中：{{ hits.join('、') }}（已脱敏）
            </div>
          </div>
        </template>
      </div>

      <!-- Agent 执行过程（意图 + 步骤时间线 + 状态提示；保留到下次发送） -->
      <AgentStepPanel
        v-if="showAgentPanel"
        :intent="intentKey"
        :intent-label="intentLabel"
        :steps="turnSteps"
        :loading="loading"
        :no-result="noResult"
        :disclaimer="disclaimer"
      />

      <!-- 输入区 -->
      <footer class="chat-input">
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          resize="none"
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          @keyup.enter.exact.prevent="send()"
        />
        <el-button v-if="loading" @click="stop">停止</el-button>
        <el-button v-else type="primary" :disabled="!input.trim()" @click="send()">发送</el-button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.chat-wrap { display: flex; gap: var(--gap); height: 100%; }
.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.chat-bar {
  display: flex; align-items: center; gap: 10px; padding: 12px 16px;
  border-bottom: 1px solid var(--line);
}
.chat-bar .ttl { font-size: 16px; color: var(--text-strong); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-label { font-size: 12px; }
.chat-list { flex: 1; overflow: auto; padding: 16px; }
.empty { text-align: center; padding: 60px 20px; }
.empty-ttl { font-size: 22px; color: var(--text-strong); margin-bottom: 10px; }
.empty p { font-size: 13px; max-width: 460px; margin: 0 auto 18px; line-height: 1.7; }
.examples { display: flex; flex-direction: column; gap: 8px; align-items: center; }
.examples .el-button { max-width: 420px; }
.msg { display: flex; margin-bottom: 16px; }
.msg.user { justify-content: flex-end; }
.msg.user .bubble {
  max-width: 72%; padding: 10px 14px; border-radius: 12px 12px 2px 12px;
  background: var(--brand); color: #fff; font-size: 14px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
}
.msg.assistant { flex-direction: column; align-items: flex-start; max-width: 82%; }
.msg.assistant .bubble {
  padding: 12px 16px; border-radius: 12px 12px 12px 2px;
  color: var(--text); font-size: 14px; line-height: 1.8; white-space: pre-wrap;
  word-break: break-word;
}
.reasoning {
  max-width: 100%; padding: 8px 12px; margin-bottom: 8px; font-size: 12px;
  border: 1px dashed var(--line); border-radius: 8px; color: var(--text-faint);
  line-height: 1.6; white-space: pre-wrap;
}
.thinking { color: var(--text-faint); }
.cursor { display: inline-block; width: 2px; height: 1em; background: var(--brand); margin-left: 2px; vertical-align: text-bottom; animation: blink 0.8s infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.msg-refs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.ref-label { color: var(--text-faint); font-size: 12px; }
.ref-tag { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.msg-hits { margin-top: 6px; color: var(--c-warning); font-size: 12px; }
.chat-input {
  display: flex; gap: 10px; align-items: flex-end; padding: 12px 16px;
  border-top: 1px solid var(--line);
}
.chat-input .el-textarea { flex: 1; }
</style>
