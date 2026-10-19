<script setup lang="ts">
/**
 * 深度推理提示词管理面板（抽屉）。
 *
 * 目的：让"模型到底被怎么指示"完全可见、可改——理解 / 决策 / 最终生成
 * 三段提示词全文 + 可用工具契约，全部来自后端运行时实际生效的那份
 * （GET /v1/chat/react/prompts：工具清单与收尾规则已注入，自定义覆盖时
 * 返回覆盖文本）。所见即所跑，不需要翻代码。
 *
 * 双通道（2026-10-19 新增）：``channel`` 决定拉哪一套——
 * - ``chat``（默认）：问答通道（提问理解 / 推理决策 / 最终答案生成）；
 * - ``draft``：公文起草通道（起草需求理解 / 起草推理决策；最终成稿提示词
 *   在配置中心「提示词模板」维护，此处只读预览并标注来源）。
 * 起草页与深度问答页各挂各的通道，互不覆盖。
 *
 * 能力（2026-10-19 新增编辑）：
 * - **查看**：按阶段折叠，一键复制；展示运行时变量占位符；
 * - **编辑保存**（admin）：改完点保存即写配置中心，**立即生效**（无需重启）；
 * - **恢复默认**：提交空字符串清除覆盖，回到代码内置版本；
 * - **来源标记**：custom=已自定义 / default=内置默认 / builtin=只读逻辑 /
 *   external=在配置中心其他页维护（只读预览）。
 *
 * 轮数语义：后端默认 ``max_rounds=0`` = **不限轮数**，面板显示"不限"，
 * 不再出现"0 轮"这种误导性文案。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import {
  getReactPrompts,
  saveReactPrompts,
  type ReactMode,
  type ReactPromptsResp,
  type ReactPromptItem,
} from '@/api/client'

const visible = defineModel<boolean>({ default: false })

const props = defineProps<{
  reactMode?: ReactMode
  /** 通道：chat=问答（默认）/ draft=公文起草 */
  channel?: 'chat' | 'draft'
}>()

const channel = computed<'chat' | 'draft'>(() => props.channel ?? 'chat')

const loading = ref(false)
const saving = ref(false)
const data = ref<ReactPromptsResp | null>(null)
const err = ref('')
/** 展开的阶段 key 集合（默认展开前两段：理解与决策决定模型行为） */
const opened = ref<Set<string>>(new Set(['deep_prompt_understand', 'deep_prompt_decision']))
/** 正在编辑的提示词 key（null = 无） */
const editingKey = ref<string | null>(null)
/** 编辑草稿（保存前不写库） */
const draft = ref('')

async function load() {
  loading.value = true
  err.value = ''
  try {
    data.value = await getReactPrompts(props.reactMode ?? null, channel.value)
  } catch (e: any) {
    err.value = e?.message || '加载提示词失败'
  } finally {
    loading.value = false
  }
}

// 打开抽屉 / 切换档位 / 切换通道时重新拉取
watch([visible, () => props.reactMode, channel], ([v]) => {
  if (v) {
    editingKey.value = null
    // 起草通道默认展开的是起草版前两段
    opened.value = new Set(channel.value === 'draft'
      ? ['deep_prompt_draft_understand', 'deep_prompt_draft_decision']
      : ['deep_prompt_understand', 'deep_prompt_decision'])
    load()
  }
}, { immediate: true })

const prompts = computed(() => data.value?.prompts ?? [])

/** 轮数展示：0 = 不限（后端默认），不显示成 "0 轮" */
const roundsLabel = computed(() =>
  data.value?.unlimited_rounds ? '不限轮数' : `${data.value?.max_rounds ?? 0} 轮`,
)
/** 预算展示：0 = 不限 */
const budgetLabel = computed(() => {
  const ms = data.value?.total_budget_ms ?? 0
  return ms > 0 ? `${Math.round(ms / 1000)}s` : '不限时长'
})

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

/** 进入编辑态：以**当前生效文本**为草稿（不是默认文本） */
function startEdit(p: ReactPromptItem) {
  editingKey.value = p.key
  draft.value = p.content
  opened.value = new Set([...opened.value, p.key])
}

function cancelEdit() {
  editingKey.value = null
  draft.value = ''
}

/** 保存覆盖：提交后立即生效（后端写配置中心，下次问答即读取） */
async function save(p: ReactPromptItem) {
  const content = draft.value.trim()
  if (!content) {
    ElMessage.warning('内容为空；若想恢复默认请点「恢复默认」')
    return
  }
  saving.value = true
  try {
    await saveReactPrompts([{ key: p.key, content }])
    ElMessage.success(`「${p.name}」已保存，立即生效`)
    editingKey.value = null
    await load()
  } catch (e: any) {
    ElMessage.error(`保存失败：${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

/** 恢复默认：提交空串清除覆盖（后端语义），二次确认防误触 */
async function resetToDefault(p: ReactPromptItem) {
  try {
    await ElMessageBox.confirm(
      `将清除「${p.name}」的自定义内容，恢复为系统内置默认提示词。是否继续？`,
      '恢复默认',
      { type: 'warning', confirmButtonText: '恢复默认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    await saveReactPrompts([{ key: p.key, content: '' }])
    ElMessage.success(`「${p.name}」已恢复默认`)
    if (editingKey.value === p.key) editingKey.value = null
    await load()
  } catch (e: any) {
    ElMessage.error(`恢复失败：${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

/** 来源标签：自定义 / 默认 / 只读逻辑 / 外部配置页维护 */
function sourceTag(p: ReactPromptItem): { text: string; type: 'warning' | 'info' | 'success' } {
  if (p.source === 'external') return { text: '只读·配置中心维护', type: 'success' }
  if (!p.editable) return { text: '只读', type: 'info' }
  return p.source === 'custom' ? { text: '已自定义', type: 'warning' } : { text: '默认', type: 'info' }
}
</script>

<template>
  <el-drawer v-model="visible" :title="channel === 'draft' ? '深度推理起草 · 提示词管理' : '深度推理问答 · 提示词管理'" size="760px">
    <div v-loading="loading" class="pp">
      <el-alert v-if="err" type="error" :closable="false" :title="err" show-icon />

      <template v-else-if="data">
        <div class="pp-meta">
          <el-tag size="small" type="info">{{ channel === 'draft' ? '公文起草通道' : '智能问答通道' }}</el-tag>
          <el-tag size="small" type="info">档位 {{ data.mode === 'deep' ? '深度' : '快速' }}</el-tag>
          <el-tag size="small" type="success">{{ roundsLabel }}</el-tag>
          <el-tag size="small" type="info">{{ budgetLabel }}</el-tag>
          <el-tag size="small" type="warning">
            收敛：连续 {{ data.no_progress_limit }} 轮无新材料自动收尾
          </el-tag>
        </div>
        <p class="pp-tip text-muted">
          以下为<strong>运行时实际生效</strong>的提示词（工具清单等变量已注入）。修改后点保存即写入配置中心，
          下一次{{ channel === 'draft' ? '起草' : '问答' }}立即生效；无需重启服务。
          <template v-if="channel === 'draft'">
            标注<strong>「只读·配置中心维护」</strong>的成稿提示词，请到「配置中心 → 提示词模板」编辑。
          </template>
        </p>

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
            <el-tag size="small" :type="sourceTag(p).type" class="src-tag">
              {{ sourceTag(p).text }}
            </el-tag>
            <span class="sec-stage text-muted">{{ p.stage }}</span>
            <div class="spacer" />
            <template v-if="p.editable">
              <el-button
                v-if="editingKey !== p.key" size="small" text type="primary"
                @click.stop="startEdit(p)"
              >
                编辑
              </el-button>
              <template v-else>
                <el-button size="small" text @click.stop="cancelEdit">取消</el-button>
                <el-button
                  size="small" text type="primary" :loading="saving"
                  @click.stop="save(p)"
                >
                  保存
                </el-button>
              </template>
              <el-button
                v-if="p.source === 'custom'" size="small" text
                @click.stop="resetToDefault(p)"
              >
                恢复默认
              </el-button>
            </template>
            <el-button size="small" text @click.stop="copy(p.content, p.name)">复制</el-button>
            <el-icon class="caret" :class="{ open: opened.has(p.key) }">
              <ArrowDown />
            </el-icon>
          </div>
          <div v-if="p.variables.length" class="sec-vars">
            运行时注入变量：
            <code v-for="v in p.variables" :key="v" class="var">{{ v }}</code>
          </div>

          <!-- 编辑态：草稿 textarea（不保存不生效） -->
          <div v-if="editingKey === p.key" class="sec-edit">
            <el-input
              v-model="draft" type="textarea" :rows="14" resize="vertical"
              placeholder="在此编辑提示词；可用上方列出的运行时变量占位符"
            />
            <div class="edit-foot text-muted">
              字符数 {{ draft.length }}；保存后立即生效。变量占位符（如
              <code v-if="p.variables.length">{{ p.variables[0] }}</code>）会由服务端在运行时替换。
            </div>
          </div>
          <pre v-else-if="opened.has(p.key)" class="sec-body">{{ p.content }}</pre>
        </section>
      </template>

      <el-empty v-else-if="!loading && !err" description="暂无提示词数据" />
    </div>
  </el-drawer>
</template>

<style scoped>
.pp { min-height: 200px; }
.pp-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; font-size: 12px; }
.pp-tip { margin: 0 0 14px; font-size: 12px; line-height: 1.7; }
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
.src-tag { transform: scale(0.92); }
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
.sec-edit { margin-top: 4px; }
.sec-edit :deep(textarea) {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px; line-height: 1.8;
}
.edit-foot { margin-top: 6px; font-size: 11.5px; line-height: 1.6; }
</style>
