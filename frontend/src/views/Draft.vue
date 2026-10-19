<script setup lang="ts">
/**
 * 智能起草页：按文种模板生成公文草稿（SSE 流式 + 会话历史）。
 *
 * 多轮修改机制：首次起草自动创建 draft 会话（conversation_id 由后端
 * meta 事件回传）；后续在同一会话中输入修改意见（如"语气更严厉一些"），
 * 后端将该轮 user 消息落库，历史在左侧会话栏可回看。
 *
 * 深度推理起草（2026-10-19，用户要求"按深度思考问答的方式、由模型自主
 * 决策"）：默认开启。开启后起草不再是"拿主题一把梭检索 + 直接成稿"，
 * 而是先由智能体跑 ReAct TAO 循环——是否检索、查什么关键词、要不要
 * 打开原文核对、何时收尾全部由模型逐轮自主决定，材料够了再成稿。
 * 推理过程（每轮思考/行动/观察）以 react_* 事件实时透出，内联渲染在
 * 正文上方；可用右上角开关随时退回单趟链路。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import ConversationSidebar from '@/components/ConversationSidebar.vue'
import AgentStepPanel from '@/components/AgentStepPanel.vue'
import ReasoningPanel from '@/components/ReasoningPanel.vue'
import MarkdownText from '@/components/MarkdownText.vue'
import ReActTracePanel from '@/components/ReActTracePanel.vue'
import ReactPromptsDrawer from '@/components/ReactPromptsDrawer.vue'
import {
  caller,
  exportDocx,
  getConversation,
  intentLabelOf,
  listTemplates,
  streamGenerate,
  type ConversationDetail,
  type ConversationMessage,
  type CitationPayload,
  type ExportDocxRequest,
  type PlanStep,
  type ReactAnalysis,
  type ReactMode,
  type ReactRound,
  type TemplateItem,
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

// ---- 模板（支持同一文种多模板，起草时可选具体一类）----
const templates = ref<TemplateItem[]>([])
const templatesByType = ref<Record<string, { code: string; name: string }[]>>({})
const selectedTemplate = ref<string>('') // 当前选中的模板 code
const templateLoading = ref(false)

async function loadTemplates() {
  templateLoading.value = true
  try {
    const items = await listTemplates()
    templates.value = items
    const grouped: Record<string, { code: string; name: string }[]> = {}
    for (const it of items) {
      const t = it.value.doc_type
      // 归属标注（2026-10-14 所有权）：系统标准模板为公共，本人/第三方模板私有
      const own = it.value.owner_type === 'system'
        ? '（公共）'
        : it.value.is_mine ? '（我的）' : '（第三方）'
      ;(grouped[t] ||= []).push({ code: it.key, name: `${it.value.name}${own}` })
    }
    templatesByType.value = grouped
    // 默认选中当前文种的首个模板
    syncDefaultTemplate()
  } catch {
    templatesByType.value = {}
  } finally {
    templateLoading.value = false
  }
}

/** 当前文种下的可选模板清单 */
const typeTemplates = computed(() => templatesByType.value[docType.value] || [])

/** 文种变化时，默认选中该文种首个模板 */
function syncDefaultTemplate() {
  const list = templatesByType.value[docType.value] || []
  selectedTemplate.value = list.length ? list[0].code : ''
}
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

// ---- 导出（基于已生成内容，按版本导出；按钮后置到生成完成后）----
/** 仅当存在会话且非生成中时可导出（activeId 在生成后才被后端回传填充） */
const canExport = computed(() => !!activeId.value && !loading.value)
const exportDialog = ref(false)
const exportLoading = ref(false)
const exportVersions = ref<{ id: string; label: string; preview: string }[]>([])
const selectedVersionId = ref<string>('')

// ---- Agent 步骤时间线（理解需求 → 收集依据 → 成稿 → 脱敏，过程可见）----
const steps = ref<PlanStep[]>([])

// ---- 深度推理起草（ReAct）状态（2026-10-19）----
/**
 * 开关语义：null = 跟随后端配置（默认开启）；false = 单趟旧链路。
 * 用户偏好持久化到 localStorage，与 DeepQa 页的总开关保持同一约定。
 */
const DEEP_PREF_KEY = 'draft_deep_reason'
const MODE_PREF_KEY = 'draft_react_mode'
function loadPrefs() {
  try {
    const d = localStorage.getItem(DEEP_PREF_KEY)
    deepOn.value = d === null ? null : d === '1'
    const m = localStorage.getItem(MODE_PREF_KEY)
    reactMode.value = m === 'deep' ? 'deep' : 'fast'
  } catch {
    /* 隐私模式等场景忽略：用默认值 */
  }
}
const deepOn = ref<boolean | null>(null)
const reactMode = ref<ReactMode>('fast')
const reactInfo = ref<{
  max_rounds: number
  unlimited_rounds?: boolean
  total_budget_ms: number
  tools: string[]
  channel?: 'qa' | 'draft'
} | null>(null)
const analysis = ref<ReactAnalysis | null>(null)
const rounds = ref<ReactRound[]>([])
const reactEnd = ref<{ rounds: number; stop_reason: string; stop_label?: string; citations_total: number; total_ms?: number } | null>(null)
const promptsOpen = ref(false)

/**
 * 阶段播报 + 模型思考链（与 DeepQa 同一套机制，修"SSE 在返回但页面像断了"）：
 * 模型每次调用（理解/决策/生成）要 1-4 秒，此前这段时间页面完全没有可见
 * 进展；phase 事件让页面持续显示"正在做什么 + 已等待 Ns"。
 */
const phase = ref<{ phase: string; label: string; round?: number } | null>(null)
const phaseElapsed = ref(0)
let phaseTimer: number | null = null
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
/** 有 ReAct 运行时即展示推理面板（流式中 / 结束后保留到下次起草） */
const showTrace = computed(() => reactInfo.value !== null || rounds.value.length > 0)
/** 阶段播报可见性（进行中且有阶段标签时） */
const showPhase = computed(() => loading.value && phase.value !== null)

function roundOf(no: number): ReactRound {
  let r = rounds.value.find((x) => x.round === no)
  if (!r) {
    r = { round: no, running: true }
    rounds.value.push(r)
    rounds.value.sort((a, b) => a.round - b.round)
  }
  return r
}

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
    // 还原起草文种：历史会话绑定的 doc_type 回显到文种选择框，
    // 并由 watch(docType) 自动让模板选择框默认选中该文种的首个模板，
    // 使「打开历史即可在正确文种/模板下继续生成」。
    if (detail.doc_type) {
      docType.value = detail.doc_type
      syncDefaultTemplate()
    }
    // 还原内容：把最近一条助手草稿回填到正文区，并把首条用户消息（原始主题）
    // 回填到主题输入框，使「打开历史即可看到文稿、并在此基础上继续生成」可用。
    const lastAssistant = [...detail.messages].reverse().find((m) => m.role === 'assistant')
    answer.value = lastAssistant?.content || ''
    const firstUser = detail.messages.find((m) => m.role === 'user')
    if (firstUser) topic.value = firstUser.content
    // 还原后清理本轮流式态，避免与历史内容混淆
    reasoning.value = ''
    citations.value = []
    hits.value = []
    intentLabel.value = ''
    intentKey.value = ''
    noResult.value = false
    disclaimer.value = false
    // ReAct 运行时态一并清理（历史会话的思维链从消息 reasoning 回看）
    reactInfo.value = null
    analysis.value = null
    rounds.value = []
    reactEnd.value = null
    phase.value = null
    stopPhaseTimer()
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

/** 流式生成：SSE 实时出字（meta → token* → done），思考链折叠展示。
 *
 * 深度推理起草（deep_reason）开启时事件序列多出整段 TAO 过程：
 * react_start → analysis → (react_round/thought/action/observation)*
 * → react_end，再进入成稿（meta → token → done）。
 */
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
  steps.value = []
  // 重置本轮 ReAct 展示状态
  reactInfo.value = null
  analysis.value = null
  rounds.value = []
  reactEnd.value = null
  phase.value = null
  startPhaseTimer()
  startTimer()

  controller = streamGenerate(
    {
      query: topic.value,
      template_doc_type: docType.value,
      template_code: selectedTemplate.value || undefined,
      conversation_id: activeId.value ?? undefined,
      caller_scope: caller.caller_scope,
      max_clearance: caller.max_clearance,
      rerank: false,
      // null = 跟随后端配置（默认深度推理起草）；false = 单趟旧链路
      deep_reason: deepOn.value,
      react_mode: reactMode.value,
    },
    {
      // 步骤计划：渲染 理解需求/收集依据/成稿/脱敏 时间线（过程可见）
      onPlan: (plan) => {
        steps.value = plan.map((s) => ({ ...s, status: s.status || 'pending' }))
      },
      onStepStart: (step) => {
        const target = steps.value.find((s) => s.id === step.id)
        if (target) {
          target.status = 'running'
        } else {
          steps.value.push({ ...step, status: 'running' })
        }
      },
      onStepEnd: (stepId, ok, summary) => {
        const target = steps.value.find((s) => s.id === stepId)
        if (target) {
          target.status = ok ? 'done' : 'failed'
          target.summary = summary
        }
      },
      // ---- 深度推理（ReAct）事件：智能体自主决策全过程 ----
      onReactStart: (info) => {
        reactInfo.value = info
        phaseSince = Date.now()
      },
      onAnalysis: (a) => {
        analysis.value = a
      },
      onReactRound: (round) => {
        roundOf(round)
      },
      onPhase: (p) => {
        phase.value = { phase: p.phase, label: p.label, round: p.round }
        phaseSince = Date.now()
        phaseElapsed.value = 0
      },
      onThought: (round, text) => {
        roundOf(round).thought = text
      },
      onAction: (round, tool, query, skipped) => {
        const r = roundOf(round)
        r.action = { tool, query, skipped }
      },
      onObservation: (round, o) => {
        const r = roundOf(round)
        r.hits = o.hits.map((h) => ({
          n: h.n,
          chunk_id: h.chunk_id,
          document_id: h.document_id,
          title: h.title,
          doc_no: h.doc_no,
          snippet: h.snippet,
          full_snippet: h.full_snippet,
          score: h.score,
        }))
        r.quality = o.quality
        r.note = o.note
        r.elapsed_ms = o.elapsed_ms
        r.hits_total = o.hits_total
        r.running = false
      },
      onReactEnd: (info) => {
        reactEnd.value = info
        rounds.value.forEach((r) => (r.running = false))
        phase.value = null
      },
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
        stopPhaseTimer()
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
        stopPhaseTimer()
        stopTimer()
      },
    },
  )
}

function stop() {
  controller?.abort()
  controller = null
  loading.value = false
  stopPhaseTimer()
  stopTimer()
  rounds.value.forEach((r) => (r.running = false))
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
  stopPhaseTimer()
})

onMounted(() => {
  loadPrefs()
  loadTemplates()
})
// 文种切换时，默认选中该文种首个模板（如从「通知」切到「报告」）
watch(docType, syncDefaultTemplate)
// 开关偏好持久化（用户下次进入起草页保持选择；'1'=开 / '0'=关 / 缺省=跟随后端）
watch(deepOn, (v) => {
  try {
    if (v === null) localStorage.removeItem(DEEP_PREF_KEY)
    else localStorage.setItem(DEEP_PREF_KEY, v ? '1' : '0')
  } catch {
    /* 忽略写入失败（隐私模式） */
  }
})
watch(reactMode, (v) => {
  try {
    localStorage.setItem(MODE_PREF_KEY, v)
  } catch {
    /* 忽略写入失败（隐私模式） */
  }
})

/** 打开导出弹窗：拉取会话消息，筛出 assistant 起草稿作为可导出版本。 */
async function openExport() {
  if (!activeId.value) return
  exportLoading.value = false
  try {
    const detail: ConversationDetail = await getConversation(activeId.value)
    // 按时间升序筛出所有 assistant 起草稿，编号为 V1、V2 …（天然版本管理）
    const assistants = detail.messages
      .filter((m) => m.role === 'assistant' && (m.content || '').trim())
      .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    if (!assistants.length) {
      ElMessage.warning('当前会话尚无可导出的起草内容')
      return
    }
    exportVersions.value = assistants.map((m, i) => ({
      id: m.id,
      label: `第 ${i + 1} 稿 · ${formatTs(m.created_at)}`,
      preview: (m.content || '').replace(/\s+/g, ' ').slice(0, 60),
    }))
    selectedVersionId.value = exportVersions.value[exportVersions.value.length - 1].id
    exportDialog.value = true
  } catch (e: any) {
    ElMessage.error('加载导出版本失败：' + (e?.response?.data?.detail || e.message))
  }
}

function formatTs(ts?: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return String(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 确认导出：调用后端按 conversation_id + message_id 导出已生成内容（不重新生成）。 */
async function confirmExport() {
  if (!activeId.value || !selectedVersionId.value) return
  exportLoading.value = true
  try {
    const payload: ExportDocxRequest = {
      conversation_id: activeId.value,
      message_id: selectedVersionId.value,
      template_code: selectedTemplate.value || null,
    }
    await exportDocx(payload)
    exportDialog.value = false
    ElMessage.success('已导出 docx（基于已生成内容）')
  } catch (e: any) {
    // 后端非 200 时 axios 走 catch，错误体为 JSON {detail}
    const detail = e?.response?.data?.detail || e.message
    ElMessage.error('导出失败：' + detail)
  } finally {
    exportLoading.value = false
  }
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
        <!-- 深度推理起草开关（2026-10-19：智能体自主决定是否检索/看原文/何时收尾） -->
        <el-tooltip
          :content="deepOn === false
            ? '单趟链路：直接按主题检索一次后成稿'
            : '深度推理：智能体自主决定是否检索、查什么、是否打开原文核对，材料够了再成稿（轮数不限）'"
          placement="bottom"
        >
          <span class="deep-toggle">
            <span class="dt-label">深度推理</span>
            <el-switch
              :model-value="deepOn !== false"
              size="small"
              @update:model-value="(v: boolean) => (deepOn = v)"
            />
          </span>
        </el-tooltip>
        <el-radio-group v-if="deepOn !== false" v-model="reactMode" size="small">
          <el-radio-button value="fast">快速</el-radio-button>
          <el-radio-button value="deep">深度</el-radio-button>
        </el-radio-group>
        <el-button text size="small" @click="promptsOpen = true">提示词</el-button>
        <span class="text-muted" style="font-size: 12px">当前会话：{{ activeTitle }}</span>
      </div>
      <div class="bar">
        <el-select v-model="docType" style="width: 120px" placeholder="文种">
          <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
        </el-select>
        <el-select
          v-model="selectedTemplate"
          style="width: 200px"
          placeholder="选择具体模板"
          :disabled="!typeTemplates.length"
        >
          <el-option v-for="t in typeTemplates" :key="t.code" :label="t.name" :value="t.code" />
        </el-select>
        <el-input
          v-model="topic"
          :placeholder="deepOn !== false
            ? '起草主题 / 修改意见（智能体将自主收集依据，Enter 发送）'
            : '起草主题 / 修改意见（如：语气更严厉）'"
          style="flex: 1"
          @keyup.enter="generate"
        />
        <el-button type="primary" :loading="loading" @click="generate">{{ loading ? '生成中…' : '起草' }}</el-button>
        <el-button v-if="loading" @click="stop">停止</el-button>
        <el-button v-if="canExport" :loading="exportLoading" @click="openExport">导出 docx</el-button>
      </div>

      <!-- 流式状态条：模型 / 耗时 -->
      <div v-if="loading || reasoning" class="stream-bar">
        <span v-if="loading" class="live"><i class="dot" />流式生成中</span>
        <span v-if="loading" class="mono text-muted">{{ elapsed.toFixed(1) }}s</span>
        <span v-if="modelName" class="text-muted">模型 {{ modelName }}</span>
      </div>
      <!-- 深度推理阶段播报（修"SSE 在返回但页面像断了"）：持续显示正在做什么 + 已等待 Ns -->
      <div v-if="showPhase" class="phase-bar">
        <span class="live"><i class="dot" />{{ phase?.label || '处理中…' }}</span>
        <span class="mono text-muted">已等待 {{ phaseElapsed.toFixed(1) }}s</span>
      </div>
      <!-- 推理面板（ReAct TAO 时间线）：内联在正文上方，默认展开、完整多行 -->
      <ReActTracePanel
        v-if="showTrace"
        class="live-trace"
        channel="draft"
        :rounds="rounds"
        :analysis="analysis"
        :info="reactInfo"
        :end="reactEnd"
        :loading="loading"
      />
      <!-- 思考过程：默认折叠、可展开查看；回答结束后不消失 -->
      <ReasoningPanel
        v-if="reasoning"
        :text="reasoning"
        :streaming="loading && !answer"
      />

      <!-- Agent 执行过程（意图 + 步骤时间线 + 无材料兜底提示） -->
      <AgentStepPanel
        v-if="showAgentPanel"
        :intent="intentKey"
        :intent-label="intentLabel"
        :steps="steps"
        :loading="loading"
        :no-result="noResult"
        :disclaimer="disclaimer"
      />

      <div class="paper panel">
        <div class="red">{{ docType }}</div>
        <!-- 文稿正文：Markdown 渲染（纸面浅色主题，见下方 .paper :deep 适配） -->
        <MarkdownText :text="answer || '在上方输入主题并点击「起草」，系统将基于知识库检索材料生成带引用的公文草稿，正文实时流式输出。在同一会话中继续输入修改意见即可多轮完善。'" />
        <span v-if="loading" class="cursor" />
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

      <!-- 导出弹窗：选择某一轮已生成的起草稿（多轮版本管理） -->
      <el-dialog v-model="exportDialog" title="导出 docx（选择版本）" width="560px">
        <div class="text-muted" style="font-size: 12px; margin-bottom: 10px">
          导出基于会话中已生成并存库的内容，不会重新生成。请选择要导出的某一稿：
        </div>
        <el-radio-group v-model="selectedVersionId" class="export-versions">
          <el-radio
            v-for="v in exportVersions"
            :key="v.id"
            :value="v.id"
            class="export-version"
          >
            <div class="ev-label">{{ v.label }}</div>
            <div class="ev-preview text-muted">{{ v.preview || '（空）' }}</div>
          </el-radio>
        </el-radio-group>
        <template #footer>
          <el-button @click="exportDialog = false">取消</el-button>
          <el-button type="primary" :loading="exportLoading" @click="confirmExport">导出</el-button>
        </template>
      </el-dialog>

      <!-- 深度推理提示词管理（起草通道：需求理解/推理决策可编辑，成稿提示词只读预览） -->
      <ReactPromptsDrawer v-model="promptsOpen" channel="draft" :react-mode="reactMode" />
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
/* 深度推理开关 + 档位（头部工具区） */
.deep-toggle { display: inline-flex; align-items: center; gap: 6px; }
.dt-label { font-size: 12px; color: var(--text-muted); }
/* 阶段播报条（深度推理进行中：消除静默期） */
.phase-bar {
  display: flex; align-items: center; gap: 14px; margin-bottom: 10px;
  font-size: 13px; padding: 6px 12px; border-radius: 8px;
  background: var(--brand-soft); border: 1px solid var(--brand-line);
}
.live-trace { margin-bottom: 12px; }
.cursor { display: inline-block; width: 2px; height: 1em; background: var(--brand); margin-left: 2px; vertical-align: text-bottom; animation: blink 0.8s infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.paper { padding: 28px; max-width: 820px; margin: 0 auto; background: #f7f4ec; color: #1c1c1c; }
.red { color: #c0392b; font-family: var(--font-serif); font-size: 22px; text-align: center; font-weight: 700; margin-bottom: 16px; }
.body { line-height: 1.9; white-space: pre-wrap; min-height: 200px; }
/* 纸面（浅色 #f7f4ec）上的 Markdown 适配：覆盖 MarkdownText 的深色主题令牌 */
.paper :deep(.md-body) { line-height: 1.9; min-height: 200px; }
.paper :deep(.md-body h1),
.paper :deep(.md-body h2),
.paper :deep(.md-body h3),
.paper :deep(.md-body h4) { color: #1c1c1c; font-family: var(--font-serif); }
.paper :deep(.md-body blockquote) { color: #555; background: rgba(0, 0, 0, 0.04); border-left-color: #c0392b; }
.paper :deep(.md-body th),
.paper :deep(.md-body td) { border-color: #d8d2c4; }
.paper :deep(.md-body tr:nth-child(even) td) { background: rgba(0, 0, 0, 0.03); }
.paper :deep(.md-body a) { color: #1a5276; }
.paper :deep(.md-body strong) { color: #1c1c1c; }
.paper :deep(.md-body code) { background: rgba(0, 0, 0, 0.07); }
.paper :deep(.md-body pre) { background: rgba(0, 0, 0, 0.05); }
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
/* 导出版本选择：纵向堆叠、可点击整块 */
.export-versions { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.export-version { width: 100%; height: auto; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; }
.export-version:deep(.el-radio__label) { white-space: normal; }
.ev-label { font-weight: 600; color: var(--text-strong); }
.ev-preview { font-size: 12px; margin-top: 2px; }
</style>
