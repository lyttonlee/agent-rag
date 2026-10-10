<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client from '@/api/client'

interface Tpl { key: string; value: { name: string; layout: Record<string, unknown> } }
const rows = ref<Tpl[]>([])
const dialog = ref(false)
const form = reactive({ doc_type: '', name: '', layout: '{}' })

async function load() {
  try {
    const r = await client.get('/templates')
    rows.value = (r.data || []).map((x: any) => ({ key: x.key, value: x.value }))
  } catch { rows.value = [] }
}
onMounted(load)

async function submit() {
  await client.post('/templates', {
    category: 'template', key: form.doc_type,
    value: { name: form.name, layout: JSON.parse(form.layout || '{}') },
  })
  ElMessage.success('模板已保存')
  dialog.value = false
  load()
}
</script>

<template>
  <div class="panel pad">
    <div class="head"><div class="ttl serif">文种模板库</div>
      <span class="text-muted">我方维护；第三方可导入样式</span>
      <el-button type="primary" style="margin-left:auto" @click="dialog = true">新增模板</el-button></div>
    <el-table :data="rows" empty-text="暂无模板">
      <el-table-column prop="key" label="文种" width="120" />
      <el-table-column prop="value.name" label="名称" />
      <el-table-column label="版式字段" min-width="200">
        <template #default="{ row }">{{ Object.keys(row.value.layout || {}).join('、') || '—' }}</template>
      </el-table-column>
    </el-table>
  </div>

  <el-dialog v-model="dialog" title="新增文种模板" width="520px">
    <el-form :model="form" label-width="80px">
      <el-form-item label="文种"><el-input v-model="form.doc_type" /></el-form-item>
      <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
      <el-form-item label="版式(JSON)"><el-input v-model="form.layout" type="textarea" :rows="4" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.ttl { font-size: 18px; color: var(--text-strong); }
</style>
