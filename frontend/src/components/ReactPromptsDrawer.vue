<script setup lang="ts">
/**
 * 深度推理提示词管理面板（抽屉）。
 *
 * 目的：让"模型到底被怎么指示"完全可见——理解/决策/观察/最终生成四段
 * 提示词全文 + 可用工具契约，全部来自后端运行时实际使用的那份
 * （GET /v1/chat/react/prompts，工具清单与轮数已按当前档位注入），
 * 所见即所跑，不需要翻代码。
 *
 * 交互：按阶段折叠（默认展开理解与决策——这两段决定模型行为），
 * 一键复制全文；工具清单展示名称 + 契约说明。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import { getReactPrompts, type ReactMode, type ReactPromptsResp } from '@/api/client'

const visible = defineModel<boolean>({ default: false })

const props = defineProps<{ reactMode?: ReactMode }>()

const loading = ref(false)
const data = ref<ReactPromptsResp | null>(null)
const err = ref('')
/** 展开的阶段 key 集合（默认展开前两段） */
const opened = ref<Set<string>>(new Set(['deep_prompt_understand', 'deep_prompt_decision']))

async function load() {
  loading.value = true
  err.value = ''
  try {
    data.value = await getReactPrompts(props.reactMode ?? null)
    // 首次按 key 建展开集合（避免依赖硬编码顺序）
    if (!opened.value.size) {
      opened.value = new Set(['deep_prompt_understand', 'deep_prompt_decision'])
    }
  } catch (e: any) {
    err.value = e?.response?.data?.detail || e?.message || '加载提示词失败'
  } finally {
    loading.value = false
  }
}

// 打开抽屉或切换档位时重新拉取（档位不同 → 决策 prompt 的轮数注入不同）
watch([visible, () => props.reactMode], ([v]) => {
  if (v) load()
}, { immediate: true })

const prompts = computed(() => data.value?.prompts ?? [])

function toggle(key: string) {
  const next = new Set(opened.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  opened.value = next
}

async function copy(text: string, name: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(`已复制「${name}」提示词`)
  } catch {
    ElMessage.warning('浏览器不支持剪贴板，请手动选中文本复制')
  }
}
</script>

<template>
  <el-drawer v-model="visible" title="深度推理提示词管理" size="720px">
    <div v-loading="loading" class="pp">
      <el-alert v-if="err" type="error" :closable="false" :title="err" show-icon />

      <template v-else-if="data">
        <div class="pp-meta">
          <el-tag size="small" type="info">
            当前档位 {{ data.mode === 'deep' ? '深度' : '快速' }}（{{ data.max_rounds }} 轮 / {{ Math.round(data.total_budget_ms / 1000) }}s）
          </el-tag>
          <span class="text-muted">以下为运行时实际使用的提示词，工具清单与轮数已按当前档位注入</span>
        </div>

        <!-- 可用工具契约 -->
        <section class="pp-sec">
          <h4>可用工具（{{ data.tools.length }}）</h4>
          <div class="tool-list">
            <div v-for="t in data.tools" :key="t.name" class="tool-item">
              <code class="tool-name">{{ t.name }}</code>
              <span class="tool-desc">{{ t.description }}</span>
            </div>
          </div>
        </section>

        <!-- 各阶段提示词 -->
        <section v-for="p in prompts" :key="p.key" class="pp-sec">
          <div class="sec-head" @click="toggle(p.key)">
            <span class="sec-name">{{ p.name }}</span>
            <span class="sec-stage text-muted">{{ p.stage }}</span>
            <div class="spacer" />
            <el-button size="small" text @click.stop="copy(p.content, p.name)">复制</el-button>
            <el-icon class="caret" :class="{ open: opened.has(p.key) }">
              <ArrowDown />
            </el-icon>
          </div>
          <div v-if="p.variables.length" class="sec-vars">
            运行时注入变量：<code v-for="v in p.variables" :key="v" class="var">{{ v }}</code>
          </div>
          <pre v-if="opened.has(p.key)" class="sec-body">{{ p.content }}</pre>
        </section>
      </template>

      <el-empty v-else-if="!loading && !err" description="暂无提示词数据" />
    </div>
  </el-drawer>
</template>

<style scoped>
.pp { min-height: 200px; }
.pp-meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; font-size: 12px; }
.pp-sec { margin-bottom: 18px; }
.pp-sec h4 { margin: 0 0 8px; font-size: 13px; color: var(--text-strong); }

.tool-list { display: flex; flex-direction: column; gap: 6px; }
.tool-item {
  display: flex; gap: 8px; align-items: baseline; padding: 6px 8px; border-radius: 6px;
  background: var(--bg-shell); border: 1px solid var(--line);
}
.tool-name {
  flex-shrink: 0; color: var(--brand); font-size: 12px;
  font-family: var(--font-mono, monospace);
}
.tool-desc { color: var(--text-muted); font-size: 12px; line-height: 1.6; }

.sec-head { display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 6px 0; }
.sec-head:hover .sec-name { color: var(--brand); }
.sec-name { font-size: 13px; color: var(--text-strong); font-weight: 600; }
.sec-stage { font-size: 11.5px; }
.spacer { flex: 1; }
.caret { transition: transform 0.2s; color: var(--text-faint); }
.caret.open { transform: rotate(180deg); }
.sec-vars { font-size: 11.5px; color: var(--text-faint); margin-bottom: 6px; }
.var {
  margin: 0 4px; padding: 0 4px; border-radius: 3px;
  background: var(--brand-soft); color: var(--brand);
  font-family: var(--font-mono, monospace);
}
.sec-body {
  margin: 0; padding: 10px 12px; border-radius: 8px;
  background: var(--bg-shell); border: 1px solid var(--line);
  color: var(--text); font-size: 12px; line-height: 1.85;
  white-space: pre-wrap; word-break: break-word;
  font-family: var(--font-mono, ui-monospace, monospace);
  max-height: 400px; overflow: auto;
}
</style>
