<script setup lang="ts">
/**
 * 思考过程折叠面板：回答结束后不消失，默认折叠、用户可展开查看全程推理。
 *
 * 设计由来（用户 2026-10-13 反馈）：思考过程原先在正文一开始输出就被
 * 收起消失，正确行为应是"显示折叠的思考过程，允许用户展开查看"。
 *
 * 交互约定：
 * - 思考链流式输出中自动展开（让用户看到"正在推理"）；
 * - 正文开始输出后自动收起（保持答案区整洁）；
 * - 回答结束后面板保留，默认折叠，点击标题栏随时展开/收起；
 * - 历史消息中的思考过程同样以本组件呈现（随消息持久化，不丢失）。
 *
 * 实现说明（2026-10-13 修复）：本组件完全自管理展开状态（非受控）。
 * 早期版本做过"受控/非受控混合"（open?: boolean prop + update:open 事件），
 * 但 Vue 对 Boolean 类型 prop 有隐式转换——父组件不传 open 时 props.open
 * 会被转换为 false 而非 undefined，isOpen 判断 `props.open !== undefined`
 * 恒为真，组件永远读到受控值 false，点击展开失效。混合模式无实际调用方
 * 需要，故彻底移除：展开状态仅由内部 innerOpen 管理，streaming 翻转时
 * 自动开合，父组件零负担。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  /** 思考链全文 */
  text: string
  /** 是否流式输出中（true 自动展开；翻转为 false 时自动收起） */
  streaming?: boolean
}>()

/** 内部展开状态：流式输出中展开，其余情况（含历史消息）默认折叠 */
const innerOpen = ref(!!props.streaming)
const isOpen = computed(() => innerOpen.value)

// streaming 翻转驱动自动开合：推理中展开、正文开始后收起
// （与旧版 Chat.vue 中 onReasoning→true / onToken→false 的外部控制等价）
watch(
  () => props.streaming,
  (v) => {
    innerOpen.value = !!v
  },
)

function toggle() {
  innerOpen.value = !innerOpen.value
}
</script>

<template>
  <div class="reasoning-panel" :class="{ open: isOpen }">
    <button type="button" class="rp-head" @click="toggle">
      <span class="rp-icon">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 18h6M10 21h4M12 3a6 6 0 00-4 10.5c.7.8 1 1.5 1 2.5h6c0-1 .3-1.7 1-2.5A6 6 0 0012 3z" />
        </svg>
      </span>
      <span class="rp-ttl">思考过程</span>
      <span class="rp-count">{{ text.length }} 字</span>
      <span class="rp-spacer" />
      <span class="rp-hint">{{ isOpen ? '收起' : '展开' }}</span>
      <svg class="rp-chevron" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>
    <div v-show="isOpen" class="rp-body">
      {{ text }}<span v-if="streaming" class="cursor" />
    </div>
  </div>
</template>

<style scoped>
/* 思考面板：低调的辅助信息层——虚线边框 + 弱化文字，不抢答案焦点 */
.reasoning-panel {
  max-width: 100%;
  margin-bottom: 8px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
  overflow: hidden;
}
.rp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 12px;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-muted);
  text-align: left;
}
.rp-head:hover { color: var(--text); }
.rp-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 6px;
  background: var(--brand-soft);
  color: var(--brand);
  flex-shrink: 0;
}
.rp-ttl { font-weight: 600; color: var(--text); }
.rp-count { color: var(--text-muted); }
.rp-spacer { flex: 1; }
.rp-hint { color: var(--brand); }
.rp-chevron { color: var(--text-muted); transition: transform 0.18s ease; }
.reasoning-panel.open .rp-chevron { transform: rotate(180deg); }
.rp-body {
  max-height: 180px;
  overflow: auto;
  padding: 4px 12px 10px 40px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  background: var(--brand);
  margin-left: 2px;
  vertical-align: text-bottom;
  animation: blink 0.8s infinite;
}
@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
</style>
