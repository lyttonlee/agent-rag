<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  deleteConfigItem,
  getConfigItems,
  saveConfigItem,
  type ConfigItem,
} from '@/api/client'

/** 配置中心四大类：每类都是"明确的项目"，用户填写结构化字段，不接触 JSON。 */
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
] as const

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
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openAdd() {
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
  <div class="panel pad">
    <div class="ttl serif">配置中心</div>
    <p class="text-muted desc">我方制定标准，第三方适配。以下为明确的可配置项，填写结构化字段即可，无需了解 JSON。</p>

    <el-tabs v-model="active" @tab-change="load">
      <el-tab-pane v-for="t in TABS" :key="t.cat" :label="t.name" :name="t.cat" />
    </el-tabs>

    <el-alert :title="currentDesc()" type="info" show-icon :closable="false" class="tip" />

    <div class="bar">
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
    <el-table v-else :data="rows" v-loading="loading" empty-text="暂无词条">
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
</style>
