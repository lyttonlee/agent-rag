<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  deleteConfigItem,
  getConfigItems,
  saveConfigItem,
  type ConfigItem,
} from '@/api/client'

/** 配置中心分类：每类都是"明确的项目"，用户填写结构化字段，不接触 JSON。 */
const TABS = [
  {
    cat: 'sensitive_word',
    name: '敏感词管理',
    desc: '自行输入敏感词词条，保存即生效：生成结果命中词条时自动脱敏（■ 掩码）并记录命中。',
  },
  {
    cat: 'format_adapter',
    name: '格式适配器',
    desc: '登记可接入的文件格式与优先级，上传接入时按扩展名自动选择解析适配器。',
  },
  {
    cat: 'model_profile',
    name: '模型 Profile',
    desc: '登记模型接入档案（本地算力 / 开发云端 / 部署方标准端点），作为运维备案与切换依据。',
  },
  {
    cat: 'dict_custom',
    name: '自定义词典',
    desc: '机构/人名/术语的标准化映射，用于检索与生成时的用语统一。',
  },
  {
    cat: 'agent',
    name: 'Agent 设置',
    desc: '智能体（Agent）运行参数：总开关、LLM 意图分类兜底、复杂任务步骤上限。关闭总开关后，问答与起草完全走原直答链路。',
  },
  {
    cat: 'retrieval',
    name: '检索参数',
    desc: '检索链路运行参数：返回条数 top_k、双路召回宽度、重排开关、相关度阈值（向量距离 / rerank 分数，低于阈值视为无效检索）。保存即时生效，无需重启。',
  },
  {
    cat: 'prompt',
    name: '提示词模板',
    desc: '生成链路使用的系统提示词（公文起草 / 智能问答 / 无材料兜底）。修改后下次生成即生效；「恢复默认」删除配置项，回退系统内置文案。',
  },
] as const

/** 检索参数默认值（与后端 app/services/runtime_config.py RetrievalConfig 保持一致） */
const RETRIEVAL_DEFAULTS = {
  final_top_n: 8,
  vector_top_n: 50,
  keyword_top_n: 50,
  rerank_enabled: true,
  vec_distance_threshold: 0.6,
  rerank_min_score: 0.35,
  // ---- 查询改写（《检索优化方案 V1.0》P1 规划项，功能上线前调整不生效）----
  rewrite_enabled: false,
  rewrite_max_rounds: 2,
  rewrite_timeout_ms: 8000,
}
const retrievalForm = reactive({ ...RETRIEVAL_DEFAULTS })

/**
 * 提示词模板默认值（与后端 app/services/generation_prompt.py 的 DEFAULT_* 常量一致；
 * 后端为唯一事实源，如调整文案请同步此处——保存后以配置中心值为准）。
 */
const PROMPT_DEFAULTS: Record<string, string> = {
  draft_system:
    '你是企业公文辅助写作引擎。请基于【检索材料】回答，并遵守：\n' +
    '1. 所有事实性陈述必须标注引用，格式 [n] 对应材料序号；\n' +
    '2. 检索材料未覆盖的内容，必须明确说明\'依据不足，建议核实\'，不得编造；\n' +
    '3. 用语符合公文规范，结构清晰。\n',
  no_materials_system:
    '你是企业公文辅助写作引擎。本次未检索到知识库参考材料，请基于你的' +
    '通用公文写作能力完成起草，并遵守：\n' +
    '1. 不得声称引用了知识库文档、内部规定或具体文号（没有材料可引）；\n' +
    '2. 涉及具体数据、时限、责任人等处使用占位符或说明\'需按实际情况填写\'；\n' +
    '3. 用语符合公文规范，结构清晰。\n',
  chat_system:
    '你是企业公文知识库的智能问答助手。请基于【检索材料】与【对话历史】回答，并遵守：\n' +
    '1. 所有事实性陈述必须标注引用，格式 [n] 对应本轮检索材料序号；\n' +
    '2. 检索材料未覆盖的内容，必须明确说明\'依据不足，建议核实\'，不得编造；\n' +
    '3. 结合对话历史理解用户指代（如\'它\'\'该文件\'\'上述要求\'），保持上下文连贯；\n' +
    '4. 用语简洁专业，结构清晰；与公文起草无关时不要套用公文格式。\n',
  chat_no_materials_system:
    '你是企业公文知识库的智能问答助手。本次未检索到知识库参考材料，' +
    '但用户的请求属于完成任务型（如起草、创作、整理），请基于你的' +
    '通用能力直接完成，并遵守：\n' +
    '1. 不得声称引用了知识库文档、内部规定或具体文号（没有材料可引）；\n' +
    '2. 涉及具体数据、时限、责任人等处使用占位符或说明\'需按实际情况填写\'；\n' +
    '3. 结合对话历史理解用户指代，保持上下文连贯；\n' +
    '4. 用语简洁专业，结构清晰。\n',
}
const PROMPT_LABELS: Record<string, string> = {
  draft_system: '公文起草（有材料）',
  no_materials_system: '公文起草（无材料兜底）',
  chat_system: '智能问答（有材料）',
  chat_no_materials_system: '智能问答（无材料兜底）',
}
const promptForm = reactive<Record<string, string>>({ ...PROMPT_DEFAULTS })

/** Agent 配置项（与后端 app/services/agent/config.py 的键一一对应） */
const AGENT_DEFAULTS = {
  enabled: true,
  llm_classify: true,
  llm_classify_timeout_ms: 3000,
  max_steps: 6,
}
const agentForm = reactive({ ...AGENT_DEFAULTS })

const active = ref<string>('sensitive_word')
const rows = ref<ConfigItem[]>([])
const loading = ref(false)
const dialog = ref(false)
const editing = ref(false)
/** 编辑时的原始键：若用户修改了键名（如敏感词词条本身），提交时需删除旧键 */
const originalKey = ref('')

/** 各分类的结构化表单（字段明确、类型明确，替代裸 JSON 输入） */
interface Forms {
  sensitive_word: { word: string; severity: string; note: string }
  format_adapter: { name: string; extensions: string; priority: number }
  model_profile: { name: string; type: string; endpoint: string; api_key: string; model: string }
  dict_custom: { term: string; mapping: string }
}
const emptyForms = (): Forms => ({
  sensitive_word: { word: '', severity: 'medium', note: '' },
  format_adapter: { name: '', extensions: '', priority: 1 },
  model_profile: { name: '', type: 'local', endpoint: '', api_key: '', model: '' },
  dict_custom: { term: '', mapping: '' },
})
const form = reactive<Forms>(emptyForms())

const SEVERITY: Record<string, { label: string; type: string }> = {
  high: { label: '高', type: 'danger' },
  medium: { label: '中', type: 'warning' },
  low: { label: '低', type: 'info' },
}
const PROVIDER_TYPES: Record<string, string> = {
  local: '本地算力（内网）',
  cloud: '开发云端',
  std: '部署方标准端点',
}

async function load() {
  loading.value = true
  try {
    rows.value = await getConfigItems(active.value)
    if (active.value === 'agent') fillAgentForm()
    if (active.value === 'retrieval') fillRetrievalForm()
    if (active.value === 'prompt') fillPromptForm()
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)

/** 行 → Agent 表单（缺项用默认值；is_active=false 视为未配置） */
function fillAgentForm() {
  Object.assign(agentForm, AGENT_DEFAULTS)
  for (const row of rows.value) {
    if (!row.is_active) continue
    const v = (row.value as Record<string, any>)?.value
    if (row.key in agentForm && v !== undefined && v !== null) {
      ;(agentForm as Record<string, any>)[row.key] = v
    }
  }
}

/** 保存 Agent 配置（逐项写入，保存即生效） */
async function saveAgent() {
  try {
    for (const key of Object.keys(AGENT_DEFAULTS) as (keyof typeof AGENT_DEFAULTS)[]) {
      await saveConfigItem({
        category: 'agent',
        key,
        value: { value: agentForm[key] },
        scope: null,
        is_active: true,
      })
    }
    ElMessage.success('Agent 配置已保存，即时生效')
    load()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.response?.data?.detail || e.message))
  }
}

/** 恢复代码默认值（删除配置项，后端回落默认） */
async function resetAgent() {
  try {
    for (const key of Object.keys(AGENT_DEFAULTS)) {
      await deleteConfigItem('agent', key).catch(() => undefined)
    }
    ElMessage.success('已恢复默认设置')
    load()
  } catch (e: any) {
    ElMessage.error('重置失败：' + (e?.response?.data?.detail || e.message))
  }
}

// ---- 检索参数 ----

/** 行 → 检索参数表单（缺项用默认值；is_active=false 视为未配置） */
function fillRetrievalForm() {
  Object.assign(retrievalForm, RETRIEVAL_DEFAULTS)
  for (const row of rows.value) {
    if (!row.is_active) continue
    const v = (row.value as Record<string, any>)?.value
    if (row.key in retrievalForm && v !== undefined && v !== null) {
      ;(retrievalForm as Record<string, any>)[row.key] = v
    }
  }
}

/** 保存检索参数（逐项写入，保存即生效） */
async function saveRetrieval() {
  try {
    for (const key of Object.keys(RETRIEVAL_DEFAULTS) as (keyof typeof RETRIEVAL_DEFAULTS)[]) {
      await saveConfigItem({
        category: 'retrieval',
        key,
        value: { value: retrievalForm[key] },
        scope: null,
        is_active: true,
      })
    }
    ElMessage.success('检索参数已保存，即时生效')
    load()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.response?.data?.detail || e.message))
  }
}

/** 恢复代码默认值（删除配置项，后端回落默认） */
async function resetRetrieval() {
  try {
    for (const key of Object.keys(RETRIEVAL_DEFAULTS)) {
      await deleteConfigItem('retrieval', key).catch(() => undefined)
    }
    ElMessage.success('已恢复默认参数')
    load()
  } catch (e: any) {
    ElMessage.error('重置失败：' + (e?.response?.data?.detail || e.message))
  }
}

// ---- 提示词模板 ----

/** 行 → 提示词表单（缺项用系统默认文案） */
function fillPromptForm() {
  Object.assign(promptForm, PROMPT_DEFAULTS)
  for (const row of rows.value) {
    if (!row.is_active) continue
    const v = (row.value as Record<string, any>)?.value
    if (row.key in promptForm && typeof v === 'string') {
      promptForm[row.key] = v
    }
  }
}

/** 保存提示词模板（逐项写入，保存即生效） */
async function savePrompt() {
  try {
    for (const key of Object.keys(PROMPT_DEFAULTS)) {
      await saveConfigItem({
        category: 'prompt',
        key,
        value: { value: promptForm[key] },
        scope: null,
        is_active: true,
      })
    }
    ElMessage.success('提示词已保存，下次生成即生效')
    load()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.response?.data?.detail || e.message))
  }
}

/** 恢复系统默认（删除配置项，后端回落内置文案） */
async function resetPrompt() {
  try {
    for (const key of Object.keys(PROMPT_DEFAULTS)) {
      await deleteConfigItem('prompt', key).catch(() => undefined)
    }
    ElMessage.success('已恢复系统默认文案')
    load()
  } catch (e: any) {
    ElMessage.error('重置失败：' + (e?.response?.data?.detail || e.message))
  }
}

function openAdd() {
  // Agent / 检索参数 / 提示词为开关表单（非条目表），不走新增对话框
  if (['agent', 'retrieval', 'prompt'].includes(active.value)) return
  Object.assign(form, emptyForms())
  editing.value = false
  dialog.value = true
}

/** 行 → 表单（编辑回显） */
function openEdit(row: ConfigItem) {
  Object.assign(form, emptyForms())
  originalKey.value = row.key
  const v = row.value as Record<string, any>
  if (active.value === 'sensitive_word') {
    form.sensitive_word.word = row.key
    form.sensitive_word.severity = v.severity || 'medium'
    form.sensitive_word.note = v.note || ''
  } else if (active.value === 'format_adapter') {
    form.format_adapter.name = row.key
    form.format_adapter.extensions = (v.extensions || []).join(', ')
    form.format_adapter.priority = v.priority ?? 1
  } else if (active.value === 'model_profile') {
    form.model_profile.name = row.key
    form.model_profile.type = v.type || 'local'
    form.model_profile.endpoint = v.endpoint || ''
    form.model_profile.api_key = v.api_key || ''
    form.model_profile.model = v.model || ''
  } else {
    form.dict_custom.term = row.key
    form.dict_custom.mapping = v.mapping || ''
  }
  editing.value = true
  dialog.value = true
}

/** 表单 → ConfigItem（按分类组装 value，前端完成结构化，后端只存 KV） */
function buildItem(): ConfigItem | null {
  if (active.value === 'sensitive_word') {
    const word = form.sensitive_word.word.trim()
    if (!word) { ElMessage.warning('请输入敏感词词条'); return null }
    return { category: 'sensitive_word', key: word, value: { severity: form.sensitive_word.severity, note: form.sensitive_word.note }, scope: null, is_active: true }
  }
  if (active.value === 'format_adapter') {
    const name = form.format_adapter.name.trim()
    if (!name) { ElMessage.warning('请输入适配器名称'); return null }
    const exts = form.format_adapter.extensions.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    if (!exts.length) { ElMessage.warning('请输入至少一个扩展名'); return null }
    return { category: 'format_adapter', key: name, value: { extensions: exts, priority: Number(form.format_adapter.priority) || 1 }, scope: null, is_active: true }
  }
  if (active.value === 'model_profile') {
    const name = form.model_profile.name.trim()
    if (!name) { ElMessage.warning('请输入 Profile 名称'); return null }
    if (!form.model_profile.endpoint.trim()) { ElMessage.warning('请输入接入端点'); return null }
    return { category: 'model_profile', key: name, value: { type: form.model_profile.type, endpoint: form.model_profile.endpoint.trim(), api_key: form.model_profile.api_key, model: form.model_profile.model }, scope: null, is_active: true }
  }
  const term = form.dict_custom.term.trim()
  if (!term) { ElMessage.warning('请输入词条'); return null }
  return { category: 'dict_custom', key: term, value: { mapping: form.dict_custom.mapping }, scope: null, is_active: true }
}

async function submit() {
  const item = buildItem()
  if (!item) return
  // 编辑时若键名发生变化（如敏感词词条本身被修改），先删除旧键，避免残留
  if (editing.value && originalKey.value && originalKey.value !== item.key) {
    await deleteConfigItem(active.value, originalKey.value).catch(() => undefined)
  }
  await saveConfigItem(item)
  ElMessage.success(editing.value ? '已更新' : '已保存，配置即时生效')
  dialog.value = false
  load()
}

async function toggle(row: ConfigItem) {
  await saveConfigItem({ ...row, is_active: !row.is_active })
  ElMessage.success(row.is_active ? '已停用' : '已启用')
  load()
}

async function remove(row: ConfigItem) {
  await ElMessageBox.confirm(`确认删除「${row.key}」？删除后即时生效。`, '删除确认', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消',
  })
  await deleteConfigItem(active.value, row.key)
  ElMessage.success('已删除')
  load()
}

function currentDesc() {
  return TABS.find((t) => t.cat === active.value)?.desc ?? ''
}
</script>

<template>
  <div class="panel pad page-fill">
    <div class="ttl serif">配置中心</div>
    <p class="text-muted desc">我方制定标准，第三方适配。以下为明确的可配置项，填写结构化字段即可，无需了解 JSON。</p>

    <el-tabs v-model="active" @tab-change="load">
      <el-tab-pane v-for="t in TABS" :key="t.cat" :label="t.name" :name="t.cat" />
    </el-tabs>

    <el-alert :title="currentDesc()" type="info" show-icon :closable="false" class="tip" />

    <!-- Agent 设置：开关表单（非条目表），保存即生效 -->
    <div v-if="active === 'agent'" class="agent-settings panel" v-loading="loading">
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">Agent 总开关</div>
          <div class="agent-sub">开启后，问答与起草先做意图路由再分发策略（复杂任务分步执行、过程可见）；关闭后完全走原直答链路</div>
        </div>
        <el-switch v-model="agentForm.enabled" active-text="开" inactive-text="关" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">LLM 意图分类兜底</div>
          <div class="agent-sub">规则通道无法判断意图时，用生成模型做一次轻量分类（3 秒超时）；关闭后未命中规则一律按「确切回答」处理</div>
        </div>
        <el-switch v-model="agentForm.llm_classify" :disabled="!agentForm.enabled" active-text="开" inactive-text="关" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">LLM 分类超时（毫秒）</div>
          <div class="agent-sub">超时未返回则放弃分类，按「确切回答」处理，保证问答可用</div>
        </div>
        <el-input-number v-model="agentForm.llm_classify_timeout_ms" :min="500" :max="30000" :step="500" :disabled="!agentForm.enabled || !agentForm.llm_classify" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">复杂任务步骤上限</div>
          <div class="agent-sub">单次复杂任务最多拆解为几步（超出限制的任务直接综合直答）</div>
        </div>
        <el-input-number v-model="agentForm.max_steps" :min="1" :max="12" :disabled="!agentForm.enabled" />
      </div>
      <div class="agent-actions">
        <el-button type="primary" size="small" @click="saveAgent">保存设置</el-button>
        <el-button size="small" @click="resetAgent">恢复默认</el-button>
        <span class="text-muted">修改即时生效，无需重启</span>
      </div>
    </div>

    <!-- 检索参数：数值/开关表单（非条目表），保存即生效 -->
    <div v-if="active === 'retrieval'" class="agent-settings panel" v-loading="loading">
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">最终返回条数 top_k</div>
          <div class="agent-sub">检索结果最终返回多少条进入提示词（原默认 8）</div>
        </div>
        <el-input-number v-model="retrievalForm.final_top_n" :min="1" :max="100" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">向量路召回宽度</div>
          <div class="agent-sub">pgvector 向量召回候选数（原默认 50）</div>
        </div>
        <el-input-number v-model="retrievalForm.vector_top_n" :min="1" :max="500" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">关键词路召回宽度</div>
          <div class="agent-sub">zhparser 分词 + BM25 关键词召回候选数（原默认 50）</div>
        </div>
        <el-input-number v-model="retrievalForm.keyword_top_n" :min="1" :max="500" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">重排总开关</div>
          <div class="agent-sub">Reranker 重排（提升引用质量）；关闭后按 RRF 融合分排序，请求级 rerank=false 同样可关</div>
        </div>
        <el-switch v-model="retrievalForm.rerank_enabled" active-text="开" inactive-text="关" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">向量距离阈值（相关度）</div>
          <div class="agent-sub">top1 余弦距离超过该值且关键词零命中 → 判定「无有用信息」（拒答/兜底）。标定参考：主题相关 ≈0.28，无关 ≈0.64~0.70；调大更易放行、调小更严格</div>
        </div>
        <el-input-number v-model="retrievalForm.vec_distance_threshold" :min="0" :max="2" :step="0.05" :precision="2" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">Rerank 分数阈值（有效检索线）</div>
          <div class="agent-sub">rerank top1 低于该分数视为无效检索——查询改写的触发线之一（《检索优化方案 V1.0》P1 上线后参与判定，待评测台标定）</div>
        </div>
        <el-input-number v-model="retrievalForm.rerank_min_score" :min="0" :max="1" :step="0.05" :precision="2" />
      </div>
      <div class="agent-row">
        <div class="agent-info">
          <div class="agent-name">查询改写（P1 规划）</div>
          <div class="agent-sub">检索不到时改写 query 重检（同义扩展 / HyDE / 子问题拆解）。功能尚未上线，提前配置备用；上线后在此启用</div>
        </div>
        <el-switch v-model="retrievalForm.rewrite_enabled" active-text="开" inactive-text="关" disabled />
      </div>
      <div class="agent-actions">
        <el-button type="primary" size="small" @click="saveRetrieval">保存参数</el-button>
        <el-button size="small" @click="resetRetrieval">恢复默认</el-button>
        <span class="text-muted">修改即时生效，无需重启</span>
      </div>
    </div>

    <!-- 提示词模板：多行文本表单，保存即生效 -->
    <div v-if="active === 'prompt'" class="agent-settings panel" v-loading="loading">
      <div class="agent-row" style="display:block" v-for="(label, key) in PROMPT_LABELS" :key="key">
        <div class="agent-info" style="margin-bottom:6px">
          <div class="agent-name">{{ label }}</div>
          <div class="agent-sub">键名 <span class="mono">{{ key }}</span>；当前为系统默认时保存即覆盖</div>
        </div>
        <el-input
          v-model="promptForm[key]"
          type="textarea"
          :rows="6"
          :placeholder="PROMPT_DEFAULTS[key]"
        />
      </div>
      <div class="agent-actions">
        <el-button type="primary" size="small" @click="savePrompt">保存模板</el-button>
        <el-button size="small" @click="resetPrompt">恢复默认</el-button>
        <span class="text-muted">保存后下次生成即生效</span>
      </div>
    </div>

    <div v-if="!['agent', 'retrieval', 'prompt'].includes(active)" class="bar">
      <el-button type="primary" size="small" @click="openAdd">
        + 新增{{ active === 'sensitive_word' ? '敏感词' : active === 'format_adapter' ? '适配器' : active === 'model_profile' ? 'Profile' : '词条' }}
      </el-button>
      <span class="text-muted">共 {{ rows.length }} 条</span>
    </div>

    <!-- 敏感词表 -->
    <el-table v-if="active === 'sensitive_word'" :data="rows" v-loading="loading" empty-text="暂无敏感词，点击上方按钮自行输入">
      <el-table-column prop="key" label="敏感词" min-width="180">
        <template #default="{ row }"><b class="word">{{ row.key }}</b></template>
      </el-table-column>
      <el-table-column label="严重度" width="100">
        <template #default="{ row }">
          <el-tag :type="(SEVERITY[(row.value as any).severity]?.type || 'info') as any" size="small">
            {{ SEVERITY[(row.value as any).severity]?.label || '中' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="200">
        <template #default="{ row }">{{ (row.value as any).note || '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <span class="st" :class="{ ok: row.is_active }">{{ row.is_active ? '生效中' : '已停用' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link :type="row.is_active ? 'warning' : 'success'" @click="toggle(row)">
            {{ row.is_active ? '停用' : '启用' }}
          </el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 格式适配器表 -->
    <el-table v-else-if="active === 'format_adapter'" :data="rows" v-loading="loading" empty-text="暂无适配器">
      <el-table-column prop="key" label="适配器名称" min-width="160" />
      <el-table-column label="支持扩展名" min-width="220">
        <template #default="{ row }">
          <el-tag v-for="e in (row.value as any).extensions || []" :key="e" size="small" style="margin-right: 4px">.{{ e }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="优先级" width="90">
        <template #default="{ row }">{{ (row.value as any).priority }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 模型 Profile 表 -->
    <el-table v-else-if="active === 'model_profile'" :data="rows" v-loading="loading" empty-text="暂无 Profile">
      <el-table-column prop="key" label="名称" min-width="140" />
      <el-table-column label="类型" width="150">
        <template #default="{ row }">{{ PROVIDER_TYPES[(row.value as any).type] || (row.value as any).type }}</template>
      </el-table-column>
      <el-table-column label="模型" min-width="140">
        <template #default="{ row }">{{ (row.value as any).model || '—' }}</template>
      </el-table-column>
      <el-table-column label="端点" min-width="220" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ (row.value as any).endpoint }}</span></template>
      </el-table-column>
      <el-table-column label="密钥" width="90">
        <template #default="{ row }">{{ (row.value as any).api_key ? '••••••' : '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 自定义词典表 -->
    <el-table v-else-if="active === 'dict_custom'" :data="rows" v-loading="loading" empty-text="暂无词条">
      <el-table-column prop="key" label="词条" min-width="180" />
      <el-table-column label="标准映射" min-width="260">
        <template #default="{ row }">{{ (row.value as any).mapping || '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <!-- 敏感词对话框：用户自行输入词条 -->
  <el-dialog v-model="dialog" :title="editing ? '编辑敏感词' : '新增敏感词'" width="480px">
    <el-form v-if="active === 'sensitive_word'" :model="form.sensitive_word" label-width="80px">
      <el-form-item label="敏感词" required>
        <el-input v-model="form.sensitive_word.word" placeholder="输入敏感词词条，如：内部资料" maxlength="50" />
      </el-form-item>
      <el-form-item label="严重度">
        <el-radio-group v-model="form.sensitive_word.severity">
          <el-radio value="high">高</el-radio>
          <el-radio value="medium">中</el-radio>
          <el-radio value="low">低</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.sensitive_word.note" placeholder="选填，如：仅限办公室传阅" />
      </el-form-item>
    </el-form>

    <el-form v-else-if="active === 'format_adapter'" :model="form.format_adapter" label-width="80px">
      <el-form-item label="名称" required>
        <el-input v-model="form.format_adapter.name" placeholder="如：PDF 解析器" />
      </el-form-item>
      <el-form-item label="扩展名" required>
        <el-input v-model="form.format_adapter.extensions" placeholder="多个用逗号分隔，如：pdf,ofd" />
      </el-form-item>
      <el-form-item label="优先级">
        <el-input-number v-model="form.format_adapter.priority" :min="1" :max="99" />
      </el-form-item>
    </el-form>

    <el-form v-else-if="active === 'model_profile'" :model="form.model_profile" label-width="80px">
      <el-form-item label="名称" required>
        <el-input v-model="form.model_profile.name" placeholder="如：内网 Qwen3 生成" />
      </el-form-item>
      <el-form-item label="类型" required>
        <el-select v-model="form.model_profile.type">
          <el-option v-for="(label, val) in PROVIDER_TYPES" :key="val" :label="label" :value="val" />
        </el-select>
      </el-form-item>
      <el-form-item label="接入端点" required>
        <el-input v-model="form.model_profile.endpoint" placeholder="OpenAI 兼容端点，如 http://192.168.6.49:8000/v1" />
      </el-form-item>
      <el-form-item label="模型名">
        <el-input v-model="form.model_profile.model" placeholder="如：qwen3-32b-instruct" />
      </el-form-item>
      <el-form-item label="API Key">
        <el-input v-model="form.model_profile.api_key" placeholder="选填" show-password />
      </el-form-item>
    </el-form>

    <el-form v-else :model="form.dict_custom" label-width="80px">
      <el-form-item label="词条" required>
        <el-input v-model="form.dict_custom.term" placeholder="输入机构/人名/术语" />
      </el-form-item>
      <el-form-item label="标准映射">
        <el-input v-model="form.dict_custom.mapping" placeholder="统一后的标准表述" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pad { padding: 16px; }
.ttl { font-size: 18px; color: var(--text-strong); margin-bottom: 6px; }
.desc { font-size: 13px; margin-bottom: 4px; }
.tip { margin: 10px 0; }
.bar { display: flex; align-items: center; gap: 12px; margin: 10px 0 14px; }
.word { color: var(--text-strong); }
.st { color: var(--text-muted); font-size: 12px; }
.st.ok { color: var(--c-success); }
.mono { font-family: var(--font-mono, monospace); font-size: 12px; }

/* Agent 设置卡片 */
.agent-settings { padding: 8px 18px 14px; margin-bottom: 14px; }
.agent-row {
  display: flex; align-items: center; gap: 16px;
  padding: 12px 0; border-bottom: 1px solid var(--line);
}
.agent-row:last-of-type { border-bottom: none; }
.agent-info { flex: 1; min-width: 0; }
.agent-name { color: var(--text-strong); font-size: 14px; font-weight: 600; }
.agent-sub { color: var(--text-faint); font-size: 12px; line-height: 1.6; margin-top: 2px; }
.agent-actions { display: flex; align-items: center; gap: 10px; padding-top: 12px; }
.agent-actions .text-muted { font-size: 12px; }
</style>
