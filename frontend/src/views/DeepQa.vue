<script setup lang="ts">
/**
 * 深度推理问答页（ReAct Agent，用户 2026-10-16 需求）。
 *
 * 与「智能问答」（Chat.vue）的本质区别：
 * - Chat.vue：**固定单轮链路**——意图路由后直接「检索一次 → 直答」，
 *   检不检、检什么由代码决定，过程对用户不可见；
 * - 本页：**智能体自主决策**——是否需要检索、用哪个工具、要不要打开
 *   文档看原文、何时收尾，全部由大模型在 TAO 循环（Thought → Action →
 *   Observation）里自行判断；第 1 轮就可以判定"这是通用常识、无需查库"
 *   并直接作答。完整思维链逐轮内联展示在答案上方。
 *
 * 用户明确的产品红线（后端与本页共同保证）：
 * 1. **不拒答**：材料不足也必须完成任务（基于通用知识作答并标注依据不足），
 *    答案末尾统一附「AI 生成，请人工鉴别」声明；
 * 2. **不降级**：没有"单跳问题直接检索"之类的自动路由，模型自己决定；
 * 3. **多轮上下文**：会话历史注入决策上下文，追问可正确解析指代；
 * 4. **提示词可见**：顶栏「提示词」按钮打开管理面板，查看运行时实际使用
 *    的四段提示词全文与工具契约。
 *
 * 会话与普通问答共用同一套会话列表（conv_type=chat），按消息上的意图
 * 徽标区分两种模式；深度推理的思维链随消息 reasoning 落库，历史回看时
 * 由 ReActTracePanel 解析还原时间线。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConversationSidebar from '@/components/ConversationSidebar.vue'
import MarkdownText from '@/components/MarkdownText.vue'
import ReasoningPanel from '@/components/ReasoningPanel.vue'
import ReactPromptsDrawer from '@/components/ReactPromptsDrawer.vue'
import ReActTracePanel from '@/components/ReActTracePanel.vue'
import {
  caller,
  getConversation,
  intentLabelOf,
  listSpaces,
  streamChat,
  updateConversation,
  type ConversationDetail,
  type ConversationMessage,
  type CitationPayload,
  type DocSpace,
  type ReactAnalysis,
  type ReactMode,
  type ReactRound,
} from '@/api/client'

// ---- 知识空间（与智能问答一致：多选限定检索范围）----
const spaces = ref<DocSpace[]>([])
const selectedSpaces = ref<string[]>([])

/** 空间完整路径标签（层级结构下避免同名空间混淆） */
const spacePath = computed(() => {
  const byId = new Map(spaces.value.map((s) => [s.id, s]))
  return (id: string): string => {
    const parts: string[] = []
    let cursor = byId.get(id)
    let guard = 0
    while (cursor && guard < 32) {
      parts.unshift(cursor.name)
      cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined
      guard += 1
    }
    return parts.join(' / ')
  }
})

// ---- 会话状态（与普通问答共用同一列表）----
const sidebar = ref<InstanceType<typeof ConversationSidebar> | null>(null)
const activeId = ref<string | null>(null)
const activeTitle = ref('新会话')
const messages = ref<ConversationMessage[]>([])
const historyLoading = ref(false)

// ---- 模式控制（用户 2026-10-16 确认：独立页面 + 双档位 + 默认全开）----
const deepOn = ref(true)
const reactMode = ref<ReactMode>('fast')
/** 档位持久化（同一浏览器下次进入沿用偏好） */
const LS_KEY = 'deepqa.prefs.v1'
function loadPrefs() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      if (typeof p.deepOn === 'boolean') deepOn.value = p.deepOn
      if (p.reactMode === 'fast' || p.reactMode === 'deep') reactMode.value = p.reactMode
    }
  } catch {
    /* localStorage 不可用时用默认值 */
  }
}
function savePrefs() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({ deepOn: deepOn.value, reactMode: reactMode.value }))
  } catch {
    /* 忽略持久化失败 */
  }
}

// ---- 流式状态 ----
const input = ref('')
const sentQuery = ref('')
const loading = ref(false)
const answer = ref('')
const citations = ref<CitationPayload[]>([])
const hits = ref<string[]>([])
const modelName = ref('')
let controller: AbortController | null = null
const listRef = ref<HTMLElement | null>(null)

// ---- ReAct 状态（SSE 事件驱动）----
const reactInfo = ref<{ max_rounds: number; total_budget_ms: number; tools: string[] } | null>(null)
const analysis = ref<ReactAnalysis | null>(null)
const rounds = ref<ReactRound[]>([])
const reactEnd = ref<{ rounds: number; stop_reason: string; stop_label?: string; citations_total: number; total_ms?: number } | null>(null)
/** 提示词管理抽屉 */
const promptsOpen = ref(false)

/**
 * 阶段播报 + 模型思考链（2026-10-16 新增，修"SSE 在返回但页面像断了"）
 *
 * 问题根因：① 模型每次调用要 1-4 秒，此前这些时间没有任何 SSE 事件，页面
 * 完全静止；② `reasoning` 事件（模型的思考内容）DeepQa 根本没接 handler，
 * 全部丢弃——用户看到的是"什么都没发生，然后突然蹦出答案"。
 * 现在：phaseLabel 持续显示"正在做什么 + 已等待 Ns"，modelThinking 实时
 * 追加模型的思考文本，两者叠加消除静默期。
 */
const phase = ref<{ phase: string; label: string; round?: number } | null>(null)
const phaseElapsed = ref(0)
/** 模型思考链实时文本（reasoning 事件累积） */
const modelThinking = ref('')
let phaseTimer: number | null = null
/** 阶段开始时间戳（用于"已思考 Ns"计时） */
let phaseSince = 0

function startPhaseTimer() {
  if (phaseTimer) return
  phaseTimer = window.setInterval(() => {
    if (phaseSince) phaseElapsed.value = Math.round((Date.now() - phaseSince) / 100) / 10
  }, 100)
}
function stopPhaseTimer() {
  if (phaseTimer) {
    clearInterval(phaseTimer)
    phaseTimer = null
  }
  phaseElapsed.value = 0
  phaseSince = 0
}
const hasMessages = computed(() => messages.value.length > 0 || loading.value)
const showTrace = computed(() => reactInfo.value !== null || rounds.value.length > 0)
/** 是否有任何可见的"进行中"信号（阶段播报或模型思考），决定是否显示等待条 */
const showProgress = computed(() => loading.value && (phase.value !== null || modelThinking.value.length > 0))

/** 示例问题（空态引导：覆盖多跳/对比/排查/无需检索四类，凸显自主决策优势） */
const examples = [
  '对比本库关于安全生产与应急管理的两份通知，要求有何差异？',
  '入库失败的任务如何排查？分别给出常见原因与处置规范。',
  '本库有哪些关于公文格式与审批流程的规定？请综合说明。',
  '公文标题该怎么拟？写一份拟稿通知的标题。（通用常识，无需查库）',
]

onMounted(async () => {
  loadPrefs()
  try {
    spaces.value = await listSpaces()
  } catch {
    ElMessage.error('加载知识空间失败')
  }
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
  resetTurn()
}

/**
 * 重置本轮流式与 ReAct 状态。
 *
 * 注意：**不动 sentQuery**——它由 send() 在调用本函数之后设置（用户气泡
 * 文本）。旧实现先设 sentQuery 再 resetTurn，导致用户气泡渲染成空白。
 */
function resetTurn() {
  answer.value = ''
  citations.value = []
  hits.value = []
  reactInfo.value = null
  analysis.value = null
  rounds.value = []
  reactEnd.value = null
  phase.value = null
  modelThinking.value = ''
  stopPhaseTimer()
}

/** 切换知识范围：持久化到会话绑定 */
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

/** 取指定轮次（不存在则新建） */
function roundOf(no: number): ReactRound {
  let r = rounds.value.find((x) => x.round === no)
  if (!r) {
    r = { round: no, running: true }
    rounds.value.push(r)
    rounds.value.sort((a, b) => a.round - b.round)
  }
  return r
}

function stop() {
  controller?.abort()
  controller = null
  loading.value = false
  stopPhaseTimer()
  phase.value = null
  rounds.value.forEach((r) => (r.running = false))
  if (activeId.value) {
    getConversation(activeId.value)
      .then((d) => {
        messages.value = d.messages
      })
      .catch(() => undefined)
  }
  answer.value = ''
  ElMessage.info('已停止，已生成部分已保留在会话中')
}

function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  if (loading.value) return

  savePrefs()
  loading.value = true
  sentQuery.value = q
  resetTurn()
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
      // 深度推理开关与档位（关闭时后端走普通问答链路）
      deep_reason: deepOn.value ? true : false,
      react_mode: reactMode.value,
    },
    {
      onMeta: (conversationId, model, cits, _truncated, meta) => {
        if (conversationId && conversationId !== activeId.value) activeId.value = conversationId
        modelName.value = model
        citations.value = cits
        scrollBottom()
      },
      // ---- ReAct 事件 → TAO 时间线 ----
      onReactStart: (info) => {
        reactInfo.value = info
        scrollBottom()
      },
      // ---- 阶段播报：每个耗时动作前后更新"正在做什么 + 已等待 Ns" ----
      onPhase: (p) => {
        // answer 阶段说明模型已进入正文生成，此时思考链不再增长，阶段条可收起
        if (p.phase === 'answer') {
          phase.value = { phase: p.phase, label: p.label }
        } else {
          phase.value = { phase: p.phase, label: p.label, round: p.round }
          phaseSince = Date.now()
          phaseElapsed.value = 0
          startPhaseTimer()
        }
        scrollBottom()
      },
      onAnalysis: (a) => {
        analysis.value = a
        scrollBottom()
      },
      onReactRound: (round, maxRounds) => {
        const r = roundOf(round)
        r.running = true
        r.max_rounds = maxRounds
        scrollBottom()
      },
      onThought: (round, text) => {
        roundOf(round).thought = text
        scrollBottom()
      },
      /**
       * 模型思考链（reasoning 事件）：此前 DeepQa 未接 handler，全部丢弃——
       * 用户看不到任何"模型在想什么"。现在实时追加并显示。
       */
      onReasoning: (t) => {
        modelThinking.value += t
        scrollBottom()
      },
      onToken: (t) => {
        // 正文开始流出即收起阶段条（答案本身是最好的进度指示）
        if (answer.value === '') phase.value = null
        answer.value += t
        scrollBottom()
      },
      onAction: (round, tool, query, skipped) => {
        const r = roundOf(round)
        r.action = { tool, query, skipped }
        scrollBottom()
      },
      onObservation: (round, o) => {
        const r = roundOf(round)
        r.hits = o.hits
        r.quality = o.quality
        r.note = o.note
        r.elapsed_ms = o.elapsed_ms
        r.hits_total = o.hits_total
        r.running = false
        scrollBottom()
      },
      onReactEnd: (info) => {
        reactEnd.value = info
        rounds.value.forEach((r) => (r.running = false))
        scrollBottom()
      },
      onDone: (finalAnswer, sensitiveHits, model, conversationId, extra) => {
        answer.value = finalAnswer
        hits.value = sensitiveHits
        modelName.value = model
        // 收尾：停掉阶段计时器（否则计时器会一直跑到组件卸载）
        stopPhaseTimer()
        phase.value = null
        if (conversationId) activeId.value = conversationId
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
        if (sensitiveHits.length) ElMessage.warning(`敏感词命中 ${sensitiveHits.length} 处，已脱敏`)
        // 注：深度推理链路**不再拒答**（无材料时基于通用知识完成任务），
        // 因此这里没有 no_result 分支；答案末尾由后端统一附 AI 生成声明。
      },
      onError: (message) => {
        ElMessage.error('问答失败：' + message)
        stopPhaseTimer()
        phase.value = null
        rounds.value.forEach((r) => (r.running = false))
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
  stopPhaseTimer()
})
</script>

<template>
  <div class="chat-wrap">
    <!-- 左：会话历史（与智能问答共用） -->
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
        <span class="mode-chip" :class="{ off: !deepOn }">{{ deepOn ? '深度推理' : '普通问答' }}</span>
        <div class="spacer" />

        <!-- 双档位：快速（低延迟）/ 深度（质量优先） -->
        <el-radio-group
          v-model="reactMode" size="small" :disabled="!deepOn"
          @change="savePrefs"
        >
          <el-radio-button value="fast">快速 2 轮</el-radio-button>
          <el-radio-button value="deep">深度 5 轮</el-radio-button>
        </el-radio-group>
        <!-- 总开关：关闭即切回普通单轮检索直答 -->
        <el-tooltip content="关闭后走普通问答（单轮检索直答）" placement="top">
          <el-switch
            v-model="deepOn" size="small" active-text="深度推理"
            @change="savePrefs"
          />
        </el-tooltip>
        <!-- 提示词管理：查看运行时实际使用的各阶段提示词 -->
        <el-tooltip content="查看模型实际使用的提示词（理解/决策/观察/生成）" placement="top">
          <el-button size="small" text @click="promptsOpen = true">提示词</el-button>
        </el-tooltip>

        <span class="text-muted bar-label">知识范围</span>
        <el-select
          :model-value="selectedSpaces"
          multiple collapse-tags collapse-tags-tooltip
          placeholder="全部空间" style="width: 200px"
          @change="onSpacesChange"
        >
          <el-option
            v-for="s in spaces"
            :key="s.id"
            :label="`${spacePath(s.id)}（${s.doc_count} 篇）`"
            :value="s.id"
          />
        </el-select>
        <span v-if="modelName" class="text-muted mono">模型 {{ modelName }}</span>
      </header>

      <!-- 消息流 -->
      <div ref="listRef" class="chat-list" v-loading="historyLoading">
        <!-- 空态 -->
        <div v-if="!hasMessages" class="empty">
          <div class="empty-ttl serif">深度推理问答</div>
          <p class="text-muted">
            智能体自主决定是否检索、调用哪个工具、要不要打开原文、什么时候收尾——
            每一步「思考 → 行动 → 观察」全程可见。材料不足时也会基于通用知识
            完成作答（末尾附 AI 生成声明），不会拒答。
          </p>
          <div class="examples">
            <el-button v-for="ex in examples" :key="ex" round size="small" @click="send(ex)">
              {{ ex }}
            </el-button>
          </div>
        </div>

        <!-- 历史消息（含深度推理的思维链回放） -->
        <template v-for="(m, i) in messages" :key="m.id">
          <div v-if="m.role === 'user'" class="msg user">
            <div class="bubble">{{ m.content }}</div>
          </div>
          <div v-else class="msg assistant">
            <!-- 深度推理消息：还原 TAO 时间线（数据来自落库的 reasoning 文本） -->
            <ReActTracePanel
              v-if="m.intent === 'deep_reason' && m.reasoning"
              class="hist-trace"
              :trace-text="m.reasoning"
            />
            <ReasoningPanel v-else-if="m.reasoning" :text="m.reasoning" />
            <div class="bubble panel">
              <MarkdownText :text="m.content" />
              <span v-if="m.intent" class="intent-badge" :title="`本轮意图：${intentLabelOf(m.intent)}`">
                {{ intentLabelOf(m.intent) }}
              </span>
            </div>
            <div v-if="m.citations?.length" class="msg-refs">
              <span class="ref-label">引用 {{ m.citations.length }} 条</span>
              <el-tag
                v-for="(c, ci) in m.citations" :key="c.chunk_id || ci"
                size="small" type="info" effect="dark" class="ref-tag" :title="c.snippet"
              >[{{ ci + 1 }}] {{ c.title }}</el-tag>
            </div>
            <div v-if="m.sensitive_hits?.length" class="msg-hits">
              敏感词命中：{{ m.sensitive_hits.join('、') }}（已脱敏）
            </div>
          </div>
        </template>

        <!-- 流式中的本轮 -->
        <template v-if="loading">
          <div class="msg user">
            <div class="bubble">{{ sentQuery }}</div>
          </div>
          <div class="msg assistant">
            <!-- 阶段进度条（消除静默期：模型每次调用 1-4 秒，此前页面完全静止） -->
            <div v-if="phase" class="phase-bar" :class="{ streaming: answer }">
              <span class="spinner" />
              <span class="phase-label">{{ phase.label }}</span>
              <span v-if="phaseElapsed > 0.3" class="phase-elapsed mono">{{ phaseElapsed }}s</span>
            </div>
            <!-- 模型思考链实时展示（reasoning 事件，此前被完全丢弃） -->
            <div v-if="modelThinking && !answer" class="thinking-live panel">
              <div class="tl-head">
                <span class="tl-tag">模型思考</span>
                <span class="text-muted tl-hint">实时生成中</span>
              </div>
              <div class="tl-text">{{ modelThinking }}</div>
            </div>
            <!-- 思维链内联在答案上方（2026-10-16 重做：旧版放在消息列表外，
                 视觉上等于"没有思维链"）。默认展开、完整多行、不截断。 -->
            <ReActTracePanel
              v-if="showTrace"
              class="live-trace"
              :rounds="rounds"
              :analysis="analysis"
              :info="reactInfo"
              :end="reactEnd"
              :loading="loading"
            />
            <div class="bubble panel">
              <MarkdownText :text="answer" /><span v-if="answer" class="cursor" />
              <span v-if="!answer && !showTrace && !showProgress" class="thinking">正在思考…</span>
            </div>
            <div v-if="citations.length" class="msg-refs">
              <span class="ref-label">引用 {{ citations.length }} 条</span>
              <el-tag
                v-for="(c, ci) in citations" :key="c.chunk_id || ci"
                size="small" type="info" effect="dark" class="ref-tag" :title="c.snippet"
              >[{{ ci + 1 }}] {{ c.title }}</el-tag>
            </div>
            <div v-if="hits.length" class="msg-hits">
              敏感词命中：{{ hits.join('、') }}（已脱敏）
            </div>
          </div>
        </template>
      </div>

      <!-- 输入区 -->
      <footer class="chat-input">
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          resize="none"
          :placeholder="deepOn
            ? `深度推理（${reactMode === 'fast' ? '快速 2 轮' : '深度 5 轮'}）：智能体自主决定是否检索与调用哪些工具，Enter 发送`
            : '普通问答：输入问题，Enter 发送，Shift+Enter 换行'"
          @keyup.enter.exact.prevent="send()"
        />
        <el-button v-if="loading" @click="stop">停止</el-button>
        <el-button v-else type="primary" :disabled="!input.trim()" @click="send()">发送</el-button>
      </footer>
    </section>

    <!-- 提示词管理抽屉 -->
    <ReactPromptsDrawer v-model="promptsOpen" :react-mode="reactMode" />
  </div>
</template>

<style scoped>
.chat-wrap { display: flex; gap: var(--gap); height: 100%; }
.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.chat-bar {
  display: flex; align-items: center; gap: 10px; padding: 12px 16px;
  border-bottom: 1px solid var(--line); flex-wrap: wrap;
}
.chat-bar .ttl { font-size: 16px; color: var(--text-strong); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-label { font-size: 12px; }
.mode-chip {
  font-size: 11px; padding: 1px 8px; border-radius: 999px;
  color: var(--brand); background: var(--brand-soft); border: 1px solid var(--brand-line);
}
.mode-chip.off { color: var(--text-faint); background: transparent; border-color: var(--line-strong); }
.chat-list { flex: 1; overflow: auto; padding: 16px; }
.empty { text-align: center; padding: 60px 20px; }
.empty-ttl { font-size: 22px; color: var(--text-strong); margin-bottom: 10px; }
.empty p { font-size: 13px; max-width: 520px; margin: 0 auto 18px; line-height: 1.7; }
.examples { display: flex; flex-direction: column; gap: 8px; align-items: center; }
.examples .el-button { max-width: 520px; }
.msg { display: flex; margin-bottom: 16px; }
.msg.user { justify-content: flex-end; }
.msg.user .bubble {
  max-width: 72%; padding: 10px 14px; border-radius: 12px 12px 2px 12px;
  background: var(--brand-deep); color: #fff; font-size: 14px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
}
.msg.assistant { flex-direction: column; align-items: flex-start; max-width: 88%; }
/* 历史消息里的思维链面板：贴左、占满消息宽度 */
.hist-trace { margin: 0 0 8px; align-self: stretch; }
/* 流式中的思维链：与答案同宽、贴左 */
.live-trace { margin: 0 0 10px; align-self: stretch; width: 100%; }
.msg.assistant .bubble {
  padding: 12px 16px; border-radius: 12px 12px 12px 2px;
  color: var(--text); font-size: 14px; line-height: 1.8; white-space: pre-wrap;
  word-break: break-word;
}
.intent-badge {
  display: inline-block; margin-left: 8px; padding: 0 6px; border-radius: 4px;
  font-size: 11px; line-height: 18px; vertical-align: middle;
  color: var(--brand); background: var(--brand-soft); border: 1px solid var(--brand-line);
}
.thinking { color: var(--text-muted); }

/* 阶段进度条：模型每次调用 1-4 秒，期间保持可见进展 */
.phase-bar {
  display: flex; align-items: center; gap: 8px; align-self: stretch;
  padding: 7px 12px; margin-bottom: 8px; border-radius: 8px;
  background: var(--brand-soft); border: 1px solid var(--brand-line);
  font-size: 12.5px; color: var(--brand);
}
.phase-bar.streaming { background: transparent; border-color: var(--line); color: var(--text-muted); }
.spinner {
  width: 11px; height: 11px; flex-shrink: 0; border-radius: 50%;
  border: 2px solid currentColor; border-right-color: transparent;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.phase-label { min-width: 0; word-break: break-word; }
.phase-elapsed { margin-left: auto; flex-shrink: 0; color: var(--text-faint); font-size: 11px; }

/* 模型思考链实时展示 */
.thinking-live {
  align-self: stretch; width: 100%; margin-bottom: 8px; padding: 8px 11px;
  border-radius: 8px; background: var(--bg-shell); border: 1px solid var(--line);
}
.tl-head { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; }
.tl-tag {
  font-size: 11px; padding: 0 6px; border-radius: 4px; line-height: 17px;
  color: var(--text-faint); border: 1px solid var(--line-strong);
}
.tl-hint { font-size: 11px; }
.tl-text {
  font-size: 12px; line-height: 1.8; color: var(--text-muted);
  white-space: pre-wrap; word-break: break-word;
  max-height: 132px; overflow: hidden; mask-image: linear-gradient(180deg, #000 72%, transparent);
}
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