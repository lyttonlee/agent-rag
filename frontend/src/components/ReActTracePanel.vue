<script setup lang="ts">
/**
 * ReAct 深度推理过程面板：把"完整推理思维链"逐轮、完整、全文地展示出来。
 *
 * 设计要点（2026-10-16 用户反馈后重做）：
 * - **不截断**：Thought / Action query / 命中片段默认全文多行展示。旧版用
 *   `white-space: nowrap` 单行省略，长思维链等于看不见——这是"没有思维链
 *   体现"的直接原因，已彻底移除；
 * - **理解阶段可见**：模型对诉求的复述、是否需要查库的判断、候选方向都
 *   显式展示（这是"问题分析"环节，之前只在 single_hop=false 时才显示）；
 * - **过程可核对**：每轮显示耗时、命中总数、命中标题+摘要（可展开全文）、
 *   检索质量信号（向量距离/关键词命中/top1 相关度，悬浮有解释）；
 * - **两种数据源统一**：流式实时（rounds prop）与历史回看（traceText
 *   落库的 TAO 文本，按「【第N轮】」前缀解析还原）。
 */
import { computed } from 'vue'
import type { ReactAnalysis, ReactObsHit, ReactRound } from '@/api/client'

const props = defineProps<{
  /** 流式轮次数据（实时） */
  rounds?: ReactRound[]
  /** 提问理解结果 */
  analysis?: ReactAnalysis | null
  /** 循环收尾信息 */
  end?: { rounds: number; stop_reason: string; stop_label?: string; citations_total: number; total_ms?: number } | null
  /** 档位参数（来自 react_start） */
  info?: {
    max_rounds: number
    /** true = 不限轮数（2026-10-19 起默认），前端据此显示"不限"而非比例 */
    unlimited_rounds?: boolean
    total_budget_ms: number
    tools: string[]
  } | null
  /** 历史 TAO 全链文本（与 rounds 二选一，用于消息回看） */
  traceText?: string | null
  /** 是否流式进行中（当前轮呼吸点 + 面板高亮） */
  loading?: boolean
}>()

/** 工具名 → 中文标签 + 说明（悬浮可见工具契约） */
const TOOL_META: Record<string, { label: string; hint: string }> = {
  knowledge_search: { label: '知识库检索', hint: '混合检索（向量+关键词）返回相关片段摘要' },
  doc_lookup: { label: '列出候选文件', hint: '按标题/文号定位文件，只返回元数据' },
  read_document: { label: '打开原文', hint: '读取文件/切片的完整原文用于核对条款' },
  finish: { label: '决定作答', hint: '模型判断信息已足够（或可基于通用知识作答），不再调用工具' },
}

/** 停止原因 → 兜底中文（后端一般已给 stop_label） */
const STOP_LABEL: Record<string, string> = {
  final: '信息充分（模型主动结束）',
  max_rounds: '达到轮数上限',
  budget: '超出时间预算',
  no_progress: '连续多轮无新材料，已强制收尾',
  parse_fail: '决策输出连续不可解析',
  decision_fail: '决策调用连续失败',
}

function toolLabel(t: string): string {
  return TOOL_META[t]?.label || t
}
function toolHint(t: string): string {
  return TOOL_META[t]?.hint || ''
}

/** 历史 trace 文本解析：逐行还原轮次（格式契约见后端 build_trace_text） */
function parseTrace(text: string): ReactRound[] {
  const out: ReactRound[] = []
  for (const line of text.split('\n')) {
    const m = line.match(/^【第(\d+)轮】(.*)$/)
    if (!m) continue
    const round = Number(m[1])
    const body = m[2] || ''
    const fail = body.match(/^决策失败[：:](.*)$/)
    if (fail) {
      out.push({ round, note: fail[1].trim(), hits: [] })
      continue
    }
    const thought = body.match(/^思考：(.*?)(?:→|$)/)?.[1]?.trim() || ''
    const actMatch = body.match(/调用：(\S+?)(?:（已跳过）)?\("(.*?)"\)/)
    const isFinish = body.includes('决定：直接作答')
    out.push({
      round,
      thought,
      action: isFinish
        ? { tool: 'finish', query: '' }
        : actMatch
          ? { tool: actMatch[1], query: actMatch[2], skipped: body.includes('（已跳过）') }
          : undefined,
      hits: [],
      note: body.match(/（(.*?)）$/)?.[1] || '',
    })
  }
  return out
}

/** 历史模式的理解阶段（从 trace 首行「【提问理解】…」提取） */
const historyUnderstanding = computed<string | null>(() => {
  if (!props.traceText) return null
  const line = props.traceText.split('\n').find((l) => l.startsWith('【提问理解】'))
  return line ? line.replace('【提问理解】', '') : null
})

/** 展示用轮次：优先流式数据，其次历史解析 */
const displayRounds = computed<ReactRound[]>(() => {
  if (props.rounds?.length) return props.rounds
  return props.traceText ? parseTrace(props.traceText) : []
})

/** 历史模式的收尾信息（从 trace 末行「【结束】…」提取） */
const historyEnd = computed(() => {
  if (props.end || !props.traceText) return props.end || null
  const m = props.traceText.match(/【结束】停止原因：(.*?)（(\w+)）；共 (\d+) 轮；累计引用 (\d+) 条/)
  if (!m) return null
  return {
    rounds: Number(m[3]),
    stop_reason: m[2],
    stop_label: m[1],
    citations_total: Number(m[4]),
  }
})

const endInfo = computed(() => props.end || historyEnd.value)

/** 质量信号 → 语义色（向量距离越低越相关=绿；null=无数据） */
function qualityColor(v: number | null | undefined, invert = false): string {
  if (v === null || v === undefined) return 'var(--text-faint)'
  const good = invert ? v < 0.45 : v > 0.5
  const warn = invert ? v < 0.6 : v > 0.2
  return good ? 'var(--c-success)' : warn ? '#f5b04c' : 'var(--text-faint)'
}

function qualityTitle(q: Record<string, number | null>): string {
  const parts: string[] = []
  if (q.top_vector_distance !== undefined) {
    parts.push(`向量距离 ${q.top_vector_distance ?? '—'}（越低越相关）`)
  }
  if (q.keyword_hits !== undefined) parts.push(`关键词命中 ${q.keyword_hits ?? 0}`)
  if (q.top1_score !== undefined) parts.push(`top1 相关度 ${q.top1_score ?? '—'}`)
  if (q.file_count !== undefined) parts.push(`命中文件 ${q.file_count ?? 0}`)
  if (q.chunk_count !== undefined) parts.push(`读取切片 ${q.chunk_count ?? 0} 片`)
  return parts.join(' · ')
}

/** 命中展示文本：优先完整片段，回落截断摘要 */
function hitText(h: ReactObsHit): string {
  return (h.full_snippet || h.snippet || '').trim()
}

/** 当前是否有正在运行的最后一轮 */
const lastRunning = computed(() => {
  const rs = displayRounds.value
  return !!(rs.length && rs[rs.length - 1].running)
})

/**
 * 是否不限轮数（2026-10-19 起后端默认 max_rounds=0）。
 * 优先看后端显式标记，兼容旧后端只看 0 值——避免把"不限"显示成"0 轮"。
 */
const unlimitedRounds = computed(
  () => props.info?.unlimited_rounds === true || (props.info?.max_rounds ?? 1) <= 0,
)

/** 轮数展示文案：不限轮数时不显示 "N/0"，只显示实际轮数 */
const roundsText = computed(() => {
  const total = endInfo.value?.rounds ?? displayRounds.value.length
  if (unlimitedRounds.value || !props.info) return `${total} 轮`
  return `${total}/${props.info.max_rounds} 轮`
})

/** 是否设置了时间预算（0 = 不限，不显示倒计时） */
const hasBudget = computed(() => (props.info?.total_budget_ms ?? 0) > 0)

</script>

<template>
  <div class="react-panel panel" :class="{ busy: loading }">
    <!-- 头部：模式徽标 + 轮数进度 + 预算 -->
    <div class="rp-head">
      <span class="rp-icon">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 3a4 4 0 014 4v1a4 4 0 01-4 4 4 4 0 01-4-4V7a4 4 0 014-4z" />
          <path d="M6 12a6 6 0 006 6 6 6 0 006-6M12 18v3" />
        </svg>
      </span>
      <span class="rp-ttl">深度推理过程</span>
      <span v-if="endInfo || info" class="rp-count mono">
        {{ roundsText }}
        <span v-if="unlimitedRounds" class="rp-unlimited">不限轮数</span>
        <i v-if="lastRunning" class="live-dot" />
      </span>
    </div>

    <!-- ① 提问理解（"问题分析"环节：模型复述诉求 + 自述是否需要查库） -->
    <div v-if="analysis || historyUnderstanding" class="rp-understand">
      <div class="us-line">
        <span class="us-tag">提问理解</span>
        <span v-if="analysis" class="us-text">{{ analysis.restate }}</span>
        <span v-else class="us-text">{{ historyUnderstanding }}</span>
      </div>
      <div v-if="analysis" class="us-line">
        <span class="us-tag alt">是否查库</span>
        <span class="verdict" :class="analysis.needs_knowledge ? 'yes' : 'no'">
          {{ analysis.needs_knowledge ? '模型判断：需要查知识库' : '模型判断：无需查库' }}
        </span>
        <span v-if="analysis.needs_knowledge_reason" class="us-reason">
          （{{ analysis.needs_knowledge_reason }}）
        </span>
        <span class="us-hint">—— 最终是否调用工具由推理阶段逐轮自主决定</span>
      </div>
      <div v-if="analysis?.candidates?.length" class="us-line">
        <span class="us-tag alt">候选方向</span>
        <span v-for="(c, i) in analysis.candidates" :key="i" class="cand">{{ c }}</span>
      </div>
      <div v-if="analysis?.ambiguities?.length" class="us-line">
        <span class="us-tag alt">待澄清</span>
        <span v-for="(a, i) in analysis.ambiguities" :key="i" class="cand warn">{{ a }}</span>
      </div>
    </div>

    <!-- 可用工具（模型能做什么，悬浮有工具契约说明） -->
    <div v-if="info?.tools?.length" class="rp-tools">
      <span class="rp-label">可用工具</span>
      <el-tooltip
        v-for="t in info.tools" :key="t" :content="toolHint(t) || t" placement="top"
      >
        <span class="tool-chip">{{ toolLabel(t) }}</span>
      </el-tooltip>
      <span v-if="hasBudget" class="rp-budget text-muted">
        预算 {{ Math.round((info?.total_budget_ms || 0) / 1000) }}s
      </span>
    </div>

    <!-- ② TAO 轮次时间线：思考 / 行动 / 观察，全部完整多行展示 -->
    <ol v-if="displayRounds.length" class="rp-rounds">
      <li v-for="r in displayRounds" :key="r.round" class="rp-round" :class="{ running: r.running }">
        <span class="round-no mono">{{ r.round }}</span>
        <div class="round-body">
          <!-- Thought：完整多行，不截断 -->
          <div v-if="r.thought" class="tao thought">
            <span class="tao-tag">思考</span>
            <span class="tao-text">{{ r.thought }}</span>
          </div>
          <!-- 决策失败轮 -->
          <div v-else-if="r.note && !r.action" class="tao failed">
            <span class="tao-tag">中断</span>
            <span class="tao-text">{{ r.note }}</span>
          </div>
          <!-- Action：工具 + 完整 query -->
          <div v-if="r.action" class="tao action" :class="{ skipped: r.action.skipped }">
            <span class="tao-tag">行动</span>
            <el-tooltip :content="toolHint(r.action.tool) || r.action.tool" placement="top">
              <span class="tool-name">{{ toolLabel(r.action.tool) }}</span>
            </el-tooltip>
            <code v-if="r.action.query" class="tool-query">{{ r.action.tool }}({{ r.action.query }})</code>
            <span v-if="r.action.skipped" class="skip-flag">已跳过</span>
          </div>
          <!-- Observation：命中明细（标题 + 摘要全文，可展开更全文） -->
          <div v-if="r.hits?.length || r.note" class="tao obs">
            <span class="tao-tag">观察</span>
            <span v-if="r.hits_total && r.hits_total > (r.hits?.length || 0)" class="hit-count">
              命中 {{ r.hits_total }} 条（展示前 {{ r.hits?.length }} 条）
            </span>
            <span v-else-if="r.hits?.length" class="hit-count">命中 {{ r.hits.length }} 条</span>
            <ul v-if="r.hits?.length" class="hit-list">
              <li v-for="(h, i) in r.hits" :key="h.chunk_id || i" class="hit-item">
                <div class="hit-head">
                  <span class="hit-idx">[{{ i + 1 }}]</span>
                  <span class="hit-title">{{ h.title }}</span>
                  <span v-if="h.doc_no" class="hit-docno">{{ h.doc_no }}</span>
                  <i v-if="h.score" class="score-dot" :style="{ background: qualityColor(h.score) }" />
                </div>
                <div v-if="hitText(h)" class="hit-snip">{{ hitText(h) }}</div>
              </li>
            </ul>
            <span v-if="r.note && !r.hits?.length" class="obs-note">{{ r.note }}</span>
            <div class="obs-foot">
              <span
                v-if="r.quality && Object.keys(r.quality).length" class="quality"
                :title="qualityTitle(r.quality)"
              >
                <i
                  v-if="r.quality.top_vector_distance !== undefined"
                  class="q-dot" :style="{ background: qualityColor(r.quality.top_vector_distance, true) }"
                />
                <i
                  v-if="r.quality.top1_score !== undefined"
                  class="q-dot" :style="{ background: qualityColor(r.quality.top1_score) }"
                />
                <span class="quality-label">检索质量</span>
              </span>
              <span v-if="r.elapsed_ms !== undefined" class="elapsed">{{ r.elapsed_ms }}ms</span>
            </div>
          </div>
        </div>
      </li>
    </ol>

    <!-- 历史回放时的纯文本兜底（解析不出轮次也要留住内容） -->
    <pre v-else-if="traceText" class="rp-raw">{{ traceText }}</pre>

    <!-- 收尾条 -->
    <div v-if="endInfo" class="rp-end">
      结束：{{ endInfo.stop_label || STOP_LABEL[endInfo.stop_reason] || endInfo.stop_reason }}
      · 共 {{ endInfo.rounds }} 轮
      <template v-if="endInfo.total_ms"> · 总耗时 {{ (endInfo.total_ms / 1000).toFixed(1) }}s</template>
      · 累计引用 {{ endInfo.citations_total }} 条
      <span v-if="endInfo.citations_total === 0" class="rp-note">
        （模型自主决定未查库或未命中，已基于通用知识完成作答）
      </span>
    </div>
  </div>
</template>

<style scoped>
.react-panel { margin: 0 0 10px; padding: 12px 14px; border-radius: 10px; font-size: 13px; }
.react-panel.busy { border-color: var(--brand-line); }
.rp-head { display: flex; align-items: center; gap: 8px; }
.rp-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 6px;
  background: var(--brand-soft); color: var(--brand);
}
.rp-ttl { color: var(--text-strong); font-weight: 600; }
.rp-count { margin-left: auto; color: var(--text-faint); font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }
/* "不限轮数"徽标：悬停说明"由模型自行决定何时收尾" */
.rp-unlimited {
  padding: 0 5px; border-radius: 4px; font-size: 10.5px;
  background: var(--brand-soft); color: var(--brand); font-family: var(--font-sans, inherit);
}
.live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--brand); display: inline-block; animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }

/* 提问理解块 */
.rp-understand {
  margin-top: 10px; padding: 8px 10px; border-radius: 8px;
  background: var(--bg-shell); border: 1px solid var(--line);
  display: flex; flex-direction: column; gap: 5px;
}
.us-line { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; font-size: 12px; line-height: 1.7; }
.us-tag {
  flex-shrink: 0; font-size: 11px; padding: 0 6px; border-radius: 4px; line-height: 17px;
  color: var(--brand); background: var(--brand-soft); border: 1px solid var(--brand-line);
}
.us-tag.alt { color: var(--text-faint); background: transparent; border-color: var(--line-strong); }
.us-text { color: var(--text); }
.verdict { font-weight: 600; }
.verdict.yes { color: var(--brand); }
.verdict.no { color: var(--c-success); }
.us-reason, .us-hint { color: var(--text-faint); font-size: 11px; }
.cand {
  padding: 0 6px; border-radius: 4px; font-size: 11px;
  background: var(--brand-soft); color: var(--brand); border: 1px solid var(--brand-line);
}
.cand.warn { background: transparent; color: #f5b04c; border-color: rgba(245,176,76,0.4); }

.rp-tools { margin-top: 8px; font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.rp-label { color: var(--text-faint); flex-shrink: 0; }
.tool-chip { padding: 0 6px; border-radius: 4px; border: 1px solid var(--line-strong); color: var(--text-faint); cursor: help; }
.rp-budget { margin-left: auto; }

.rp-rounds { list-style: none; margin: 10px 0 0; padding: 0; }
.rp-round {
  display: flex; gap: 10px; padding: 9px 0 9px 12px;
  border-left: 1px solid var(--line); position: relative;
}
.rp-round::before {
  content: ''; position: absolute; left: -4.5px; top: 15px; width: 8px; height: 8px;
  border-radius: 50%; background: var(--bg-shell); border: 1px solid var(--line-strong);
}
.rp-round.running::before { background: var(--brand); border-color: var(--brand); animation: pulse 1.2s infinite; }
.round-no {
  flex-shrink: 0; width: 20px; height: 20px; border-radius: 5px; margin-top: 1px;
  font-size: 11px; line-height: 20px; text-align: center;
  background: var(--brand-soft); color: var(--brand);
}
.round-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }

/* TAO 三段：全部完整多行展示（旧版单行截断导致"看不见思维链"，已移除 nowrap） */
.tao { display: flex; align-items: baseline; gap: 7px; font-size: 12.5px; line-height: 1.75; min-width: 0; }
.tao-tag {
  flex-shrink: 0; font-size: 11px; padding: 0 5px; border-radius: 4px; line-height: 17px;
  border: 1px solid var(--line-strong); color: var(--text-faint); background: var(--bg-shell);
}
.tao.thought .tao-tag { color: var(--brand); border-color: var(--brand-line); background: var(--brand-soft); }
.tao-text {
  color: var(--text); white-space: pre-wrap; word-break: break-word;
  min-width: 0; flex: 1;
}
.tao.failed .tao-tag, .tao.failed .tao-text { color: var(--c-danger); border-color: rgba(226,75,74,0.4); }

/* 行动：工具名 + 完整 query（code 风格，可换行） */
.tao.action { align-items: flex-start; }
.tao.action .tool-name { color: var(--text-strong); flex-shrink: 0; font-weight: 600; cursor: help; }
.tao.action.skipped .tool-name, .skip-flag { color: #f5b04c; }
.skip-flag { flex-shrink: 0; font-size: 11px; }
.tool-query {
  flex: 1; min-width: 0; color: var(--text-muted); word-break: break-word;
  background: var(--bg-shell); border: 1px solid var(--line); border-radius: 4px;
  padding: 1px 6px; font-size: 11.5px; line-height: 1.6;
  font-family: var(--font-mono, ui-monospace, monospace); white-space: pre-wrap;
}

/* 观察：命中明细列表 */
.tao.obs { flex-direction: column; align-items: stretch; gap: 5px; }
.hit-count { color: var(--text-faint); font-size: 11px; }
.hit-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.hit-item {
  padding: 5px 8px; border-radius: 6px;
  background: var(--bg-shell); border: 1px solid var(--line);
}
.hit-head { display: flex; align-items: baseline; gap: 6px; }
.hit-idx { color: var(--brand); font-size: 11px; flex-shrink: 0; font-weight: 600; }
.hit-title { color: var(--text-strong); font-size: 12px; font-weight: 600; }
.hit-docno { color: var(--text-faint); font-size: 11px; }
.score-dot, .q-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; margin-left: auto; }
.hit-snip {
  margin-top: 3px; color: var(--text-muted); font-size: 11.5px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
}
.obs-note { color: #f5b04c; font-size: 12px; }
.obs-foot { display: flex; align-items: center; gap: 10px; }
.quality { display: inline-flex; align-items: center; gap: 4px; cursor: help; }
.quality .q-dot { margin-left: 0; }
.quality-label { color: var(--text-faint); font-size: 11px; }
.elapsed { color: var(--text-faint); font-size: 11px; margin-left: auto; font-family: var(--font-mono, monospace); }

.rp-raw {
  margin: 10px 0 0; padding: 8px 10px; border-radius: 8px; max-height: 320px; overflow: auto;
  background: var(--bg-shell); border: 1px solid var(--line);
  color: var(--text-muted); font-size: 12px; line-height: 1.8;
  white-space: pre-wrap; word-break: break-word;
  font-family: var(--font-mono, monospace);
}
.rp-end {
  margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line);
  color: var(--text-faint); font-size: 12px; line-height: 1.7;
}
.rp-note { color: var(--text-muted); }
</style>
