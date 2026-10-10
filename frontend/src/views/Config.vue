<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client from '@/api/client'

const tabs = [
  { cat: 'sensitive_word', name: '敏感词' },
  { cat: 'format_adapter', name: '格式适配器' },
  { cat: 'model_profile', name: '模型 Profile' },
  { cat: 'dict_custom', name: '自定义词典' },
]
const active = ref('sensitive_word')
const rows = ref<any[]>([])
const dialog = ref(false)
const form = reactive({ key: '', value: '{}' })

async function load() {
  try {
    const r = await client.get('/config/items', { params: { category: active.value } })
    rows.value = r.data || []
  } catch { rows.value = [] }
}
onMounted(load)

async function submit() {
  await client.put('/config/items', {
    category: active.value, key: form.key, value: JSON.parse(form.value || '{}'),
  })
  ElMessage.success('已保存配置')
  dialog.value = false
  load()
}
</script>

<template>
  <div class="panel pad">
    <div class="ttl serif">配置中心</div>
    <p class="text-muted">我方制定标准，第三方适配；以下为可配置项（敏感词 / 格式适配器 / 模型 Profile / 自定义词典）。</p>
    <el-tabs v-model="active" @tab-change="load">
      <el-tab-pane v-for="t in tabs" :key="t.cat" :label="t.name" :name="t.cat" />
    </el-tabs>
    <div class="bar">
      <el-button type="primary" size="small" @click="dialog = true">新增配置</el-button>
    </div>
    <el-table :data="rows" empty-text="暂无配置">
      <el-table-column prop="key" label="键" width="200" />
      <el-table-column label="值" min-width="260">
        <template #default="{ row }">{{ JSON.stringify(row.value) }}</template>
      </el-table-column>
      <el-table-column prop="scope" label="作用域" width="140" />
    </el-table>
  </div>

  <el-dialog v-model="dialog" title="新增配置项" width="520px">
    <el-form :model="form" label-width="80px">
      <el-form-item label="键"><el-input v-model="form.key" /></el-form-item>
      <el-form-item label="值(JSON)"><el-input v-model="form.value" type="textarea" :rows="4" /></el-form-item>
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
.bar { margin: 10px 0; }
</style>
