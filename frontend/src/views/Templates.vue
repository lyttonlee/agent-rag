<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  deleteTemplate,
  downloadTemplate,
  listTemplates,
  saveTemplate,
  seedTemplates,
  uploadTemplate,
  type TemplateItem,
  type TemplateLayout,
  type TemplateSection,
} from '@/api/client'

const DOC_TYPES = ['通知', '通报', '报告', '请示', '函', '纪要', '决定', '意见']
const FONTS = ['仿宋_GB2312', '宋体', '黑体', '楷体', '方正小标宋简体']
const PAPERS = ['A4', 'A3', '16开', 'B5']

const rows = ref<TemplateItem[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    rows.value = await listTemplates()
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)

// ---- 上传模板文件 ----
const uploadDialog = ref(false)
const uploading = ref(false)
const uploadFile = ref<File | null>(null)
const uploadForm = reactive({ doc_type: '', name: '' })

function onUploadChange(file: any) {
  uploadFile.value = file.raw as File
  if (!uploadForm.name) uploadForm.name = file.name.replace(/\.[^.]+$/, '')
}

async function submitUpload() {
  if (!uploadFile.value) {
    ElMessage.warning('请先选择模板文件')
    return
  }
  uploading.value = true
  try {
    await uploadTemplate(uploadFile.value, {
      doc_type: uploadForm.doc_type || undefined,
      name: uploadForm.name || undefined,
    })
    ElMessage.success('模板文件已上传并解析入库')
    uploadDialog.value = false
    uploadFile.value = null
    uploadForm.doc_type = ''
    uploadForm.name = ''
    load()
  } catch (e: any) {
    ElMessage.error('上传失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    uploading.value = false
  }
}

// ---- 一键初始化标准模板 ----
async function seed() {
  const r = await seedTemplates()
  ElMessage.success(r.message)
  load()
}

// ---- 可视化版式编辑 ----
const editorDialog = ref(false)
const editingKey = ref('')
const layout = reactive<TemplateLayout>({
  red_head: true,
  font: '仿宋_GB2312',
  paper: 'A4',
  subtitle: '',
  tone: '',
  sections: [],
})

function openEditor(row: TemplateItem) {
  editingKey.value = row.key
  const l = row.value.layout
  layout.red_head = l.red_head ?? true
  layout.font = l.font || '仿宋_GB2312'
  layout.paper = l.paper || 'A4'
  layout.subtitle = l.subtitle || ''
  layout.tone = l.tone || ''
  layout.sections = (l.sections || []).map((s, i) => ({ ...s, key: s.key || `sec${i + 1}` }))
  if (!layout.sections.length) {
    layout.sections = [{ key: 'body', label: '正文', placeholder: '', required: true }]
  }
  editorDialog.value = true
}

function addSection() {
  layout.sections.push({
    key: `sec${layout.sections.length + 1}`,
    label: '',
    placeholder: '',
    required: false,
  })
}
function removeSection(i: number) {
  layout.sections.splice(i, 1)
}

async function submitEditor() {
  const name = rows.value.find((r) => r.key === editingKey.value)?.value.name || editingKey.value
  await saveTemplate(editingKey.value, {
    name,
    layout: JSON.parse(JSON.stringify(layout)),
  })
  ElMessage.success('版式已保存')
  editorDialog.value = false
  load()
}

async function remove(row: TemplateItem) {
  await ElMessageBox.confirm(`确认删除「${row.key}」模板？`, '删除确认', { type: 'warning' })
  await deleteTemplate(row.key)
  ElMessage.success('已删除')
  load()
}

async function download(row: TemplateItem) {
  try {
    await downloadTemplate(row.key)
  } catch {
    ElMessage.warning('该模板暂无上传的原始文件（标准模板仅含版式定义）')
  }
}
</script>

<template>
  <div class="panel pad page-fill">
    <div class="head">
      <div class="ttl serif">文种模板库</div>
      <span class="text-muted">上传模板文件自动解析版式，或直接编辑标准模板</span>
      <div class="spacer" />
      <el-button @click="seed">初始化标准模板</el-button>
      <el-button type="primary" @click="uploadDialog = true">上传模板文件</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" empty-text="暂无模板，点击「初始化标准模板」或「上传模板文件」">
      <el-table-column prop="key" label="文种" width="100" />
      <el-table-column prop="value.name" label="名称" min-width="200" show-overflow-tooltip />
      <el-table-column label="来源" width="110">
        <template #default="{ row }">
          <el-tag :type="row.value.content_ref ? 'success' : 'info'" size="small">
            {{ row.value.content_ref ? '上传文件' : '标准模板' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="版式要点" min-width="220">
        <template #default="{ row }">
          {{ row.value.layout.red_head ? '红头 · ' : '' }}{{ row.value.layout.font }} · {{ row.value.layout.paper }} ·
          {{ (row.value.layout.sections || []).length }} 个章节
        </template>
      </el-table-column>
      <el-table-column label="章节结构" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          {{ (row.value.layout.sections || []).map((s: TemplateSection) => s.label).join(' → ') || '—' }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEditor(row)">编辑版式</el-button>
          <el-button link type="primary" @click="download(row)">下载文件</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <!-- 上传模板文件对话框 -->
  <el-dialog v-model="uploadDialog" title="上传模板文件" width="560px">
    <el-upload
      drag :auto-upload="false" :show-file-list="true" accept=".docx,.doc,.pdf,.txt"
      :on-change="onUploadChange" :limit="1">
      <div class="up-tip">将模板文件（docx / pdf / txt）拖到此处，或点击选择</div>
    </el-upload>
    <el-form :model="uploadForm" label-width="92px" style="margin-top: 14px">
      <el-form-item label="文种">
        <el-select v-model="uploadForm.doc_type" placeholder="自动识别（留空）" clearable style="width: 200px">
          <el-option v-for="t in DOC_TYPES" :key="t" :label="t" :value="t" />
        </el-select>
      </el-form-item>
      <el-form-item label="模板名称">
        <el-input v-model="uploadForm.name" placeholder="默认取文件名" />
      </el-form-item>
    </el-form>
    <el-alert type="info" :closable="false" show-icon
      title="上传后自动解析正文，按编号标题（一、/（一）/1.）提取章节结构，可在「编辑版式」中调整。" />
    <template #footer>
      <el-button @click="uploadDialog = false">取消</el-button>
      <el-button type="primary" :loading="uploading" @click="submitUpload">上传并解析</el-button>
    </template>
  </el-dialog>

  <!-- 可视化版式编辑对话框 -->
  <el-dialog v-model="editorDialog" :title="`编辑版式：${editingKey}`" width="640px">
    <el-form :model="layout" label-width="92px">
      <el-form-item label="红头">
        <el-switch v-model="layout.red_head" />
      </el-form-item>
      <el-form-item label="正文字体">
        <el-select v-model="layout.font" style="width: 200px">
          <el-option v-for="f in FONTS" :key="f" :label="f" :value="f" />
        </el-select>
      </el-form-item>
      <el-form-item label="纸张">
        <el-select v-model="layout.paper" style="width: 140px">
          <el-option v-for="p in PAPERS" :key="p" :label="p" :value="p" />
        </el-select>
      </el-form-item>
      <el-form-item label="副标题">
        <el-input v-model="layout.subtitle" placeholder="如：发文机关 + 关于××的通知" />
      </el-form-item>
      <el-form-item label="语气">
        <el-input v-model="layout.tone" placeholder="如：正式、明了" />
      </el-form-item>
      <el-form-item label="章节结构">
        <div class="sections">
          <div v-for="(s, i) in layout.sections" :key="i" class="sec-row">
            <el-input v-model="s.label" placeholder="章节名，如：主送机关" style="width: 150px" />
            <el-input v-model="s.placeholder" placeholder="内容提示" style="flex: 1" />
            <el-checkbox v-model="s.required">必填</el-checkbox>
            <el-button link type="danger" @click="removeSection(i)">删除</el-button>
          </div>
          <el-button size="small" @click="addSection">+ 添加章节</el-button>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="editorDialog = false">取消</el-button>
      <el-button type="primary" @click="submitEditor">保存版式</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.ttl { font-size: 18px; color: var(--text-strong); }
.spacer { flex: 1; }
.up-tip { color: var(--text-muted); padding: 18px 0; }
.sections { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.sec-row { display: flex; align-items: center; gap: 8px; }
</style>
