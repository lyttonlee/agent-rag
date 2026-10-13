<script setup lang="ts">
/**
 * Agent 执行过程面板：意图徽标 + 步骤时间线（复杂任务过程可见）+ 状态提示。
 *
 * 智能问答（Chat.vue）与智能起草（Draft.vue）共用，保证 Agent 体验的
 * 样式一致性与"简洁但内容具体"的要求：
 * - 头部：意图标签 + 步骤计数（N/M 步，实时推进）；
 * - 步骤行：状态图标（等待/执行中/完成/失败）+ 动作类型（检索/生成）
 *   + 步骤描述 + 结论摘要（done/failed 时展开）；
 * - 提示区：无材料直答（info）/ 超范围拒答（info）/ 任务型无材料
 *   兜底（warning，注明需自行甄别）。
 *
 * 步骤数据由 SSE plan/step_start/step_end 事件驱动，流式结束后面板
 * 保留到下一次发送，方便回看"这一轮 Agent 做了什么"。
 */
import { computed } from 'vue'
import type { PlanStep } from '@/api/client'

const props = defineProps<{
  /** 意图标识（direct_qa / complex_task / draft / chitchat / out_of_scope） */
  intent?: string | null
  /** 意图中文标签（后端 intent_label） */
  intentLabel?: string | null
  /** 步骤列表（含实时 status 与 summary） */
  steps: PlanStep[]
  /** 是否流式进行中（步骤计数带呼吸动效） */
  loading?: boolean
  /** 未检索到有用信息 */
  noResult?: boolean
  /** 任务型无材料兜底：答案需用户自行甄别 */
  disclaimer?: boolean
}>()

/** 步骤动作的中文标签 */
const ACTION_LABEL: Record<string, string> = { retrieve: '检索', generate: '生成' }

const doneCount = computed(() => props.steps.filter((s) => s.status === 'done').length)
const failedCount = computed(() => props.steps.filter((s) => s.status === 'failed').length)
const running = computed(() => props.steps.some((s) => s.status === 'running'))

/** 状态提示：按意图与检索结果分流（与后端 policies 一一对应） */
const notice = computed<{ type: 'info' | 'warning'; text: string } | null>(() => {
  if (props.disclaimer) {
    return {
      type: 'warning',
      text: '未检索到可参考的知识库材料，已基于 AI 通用能力完成，内容需您自行甄别后使用。',
    }
  }
  if (!props.noResult) return null
  if (props.intent === 'out_of_scope') {
    return { type: 'info', text: '该问题超出知识库能力边界，已明确拒答，未做推测性回答。' }
  }
  return { type: 'info', text: '未检索到相关内容，已按知识库边界委婉答复，未做推测性回答。' }
})
</script>

<template>
  <div class="agent-panel panel" :class="{ busy: loading }">
    <!-- 头部：意图 + 步骤计数 -->
    <div class="agent-head">
      <span class="agent-icon">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="4" y="4" width="16" height="16" rx="3" />
          <path d="M9 12h6M12 9v6" />
        </svg>
      </span>
      <span class="agent-ttl">Agent 执行过程</span>
      <span v-if="intentLabel" class="intent-tag">{{ intentLabel }}</span>
      <span v-if="steps.length" class="step-count mono">
        {{ doneCount + failedCount }}/{{ steps.length }} 步
        <i v-if="running" class="live-dot" />
      </span>
    </div>

    <!-- 步骤时间线 -->
    <ol v-if="steps.length" class="steps">
      <li v-for="s in steps" :key="s.id" class="step" :class="s.status || 'pending'">
        <span class="st-icon">
          <svg v-if="s.status === 'done'" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 12.5l5 5L20 6.5" /></svg>
          <svg v-else-if="s.status === 'failed'" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 6l12 12M18 6L6 18" /></svg>
          <svg v-else-if="s.status === 'running'" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" class="spin"><path d="M12 3a9 9 0 019 9" stroke-linecap="round" /></svg>
          <i v-else class="hollow" />
        </span>
        <span class="st-action">{{ ACTION_LABEL[s.action] || s.action }}</span>
        <span class="st-query" :title="s.query">{{ s.query }}</span>
        <span v-if="s.summary && (s.status === 'done' || s.status === 'failed')" class="st-summary" :title="s.summary">
          {{ s.summary }}
        </span>
      </li>
    </ol>

    <!-- 状态提示 -->
    <div v-if="notice" class="notice" :class="notice.type">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.01" />
      </svg>
      <span>{{ notice.text }}</span>
    </div>
  </div>
</template>

<style scoped>
.agent-panel {
  margin: 0 16px 12px;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 13px;
}
.agent-panel.busy { border-color: var(--brand-line); }
.agent-head { display: flex; align-items: center; gap: 8px; }
.agent-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 6px;
  background: var(--brand-soft); color: var(--brand);
}
.agent-ttl { color: var(--text-strong); font-weight: 600; }
.intent-tag {
  padding: 1px 8px; border-radius: 999px; font-size: 11px;
  background: var(--brand-soft); color: var(--brand); border: 1px solid var(--brand-line);
}
.step-count { margin-left: auto; color: var(--text-faint); font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }
.live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--brand); display: inline-block; animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }

.steps { list-style: none; margin: 8px 0 0; padding: 0; }
.step {
  display: flex; align-items: baseline; gap: 8px;
  padding: 5px 0; border-left: 1px solid var(--line); padding-left: 14px; margin-left: 6px;
  position: relative; color: var(--text-muted);
}
.step::before {
  content: ''; position: absolute; left: -4.5px; top: 11px; width: 8px; height: 8px;
  border-radius: 50%; background: var(--bg-shell); border: 1px solid var(--line-strong);
}
.step.done::before { background: var(--c-success); border-color: var(--c-success); }
.step.failed::before { background: var(--c-danger); border-color: var(--c-danger); }
.step.running::before { background: var(--brand); border-color: var(--brand); }
.st-icon { display: inline-flex; align-items: center; flex-shrink: 0; }
.step.done .st-icon { color: var(--c-success); }
.step.failed .st-icon { color: var(--c-danger); }
.step.running .st-icon { color: var(--brand); }
.step.pending .st-icon { color: var(--text-faint); }
.hollow { width: 11px; height: 11px; border-radius: 50%; border: 1px dashed var(--line-strong); display: inline-block; }
.spin { animation: spin 0.9s linear infinite; transform-origin: center; }
@keyframes spin { to { transform: rotate(360deg); } }
.st-action {
  flex-shrink: 0; font-size: 11px; padding: 0 6px; border-radius: 4px;
  border: 1px solid var(--line-strong); color: var(--text-faint);
}
.step.running .st-action { color: var(--brand); border-color: var(--brand-line); }
.step.done .st-action { color: var(--text-muted); }
.st-query { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step.running .st-query { color: var(--text-strong); }
.st-summary {
  margin-left: auto; flex-shrink: 0; max-width: 46%; font-size: 12px; color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.step.failed .st-summary { color: var(--c-danger); }

.notice {
  display: flex; align-items: flex-start; gap: 8px; margin-top: 10px;
  padding: 8px 10px; border-radius: 8px; font-size: 12px; line-height: 1.6;
}
/* info 提示用 --text（原 --text-muted 在 brand-soft 底上对比度不足） */
.notice.info { background: var(--brand-soft); color: var(--text); }
.notice.info svg { color: var(--brand); flex-shrink: 0; margin-top: 2px; }
.notice.warning { background: rgba(241, 163, 60, 0.12); color: #f5b04c; }
.notice.warning svg { color: #f5b04c; flex-shrink: 0; margin-top: 2px; }
</style>
