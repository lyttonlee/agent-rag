<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client, { caller } from '@/api/client'

interface DocRow { external_id: string; title: string; doc_type: string; security_level: string; permission_tags: string[]; status: string; chunk_count: number }

const rows = ref<DocRow[]>([])
const loading = ref(false)
const dialog = ref(false)
const form = reactive({
  external_id: '', title: '', doc_type: '通知', issuer: '', doc_no: '',
  security_level: 'INTERNAL', permission_tags: '', content: '',
})

async function load() {
  loading.value = true
  try {
    // 走检索接口演示（真实列表接口后续补全）
    const r = await client.post('/retrieve', { query: '公文', top_n: 20, ...caller })
    rows.value = (r.data.citations || []).map((c: any) => ({
      external_id: c.chunk_id, title: c.title, doc_type: '-', security_level: 'INTERNAL',
      permission_tags: [], status: 'INDEXED', chunk_count: 1,
    }))
  } finally { loading.value = false }
}
load()

async function submit() {
  const payload = {
    ...form,
    permission_tags: form.permission_tags ? form.permission_tags.split(',').map((s) => s.trim()) : [],
    ...caller,
  }
  await client.post('/documents/ingest', payload)
  ElMessage.success('已提交接入（演示）')
  dialog.value = false
}
</script>

<template>
  <div class="panel pad">
    <div class="head">
      <div class="ttl serif">文档库</div>
      <el-button type="primary" @click="dialog = true">接入文档</el-button>
    </div>
    <el-table :data="rows" v-loading="loading" style="width: 100%" empty-text="暂无数据（请先接入或检索）">
      <el-table-column prop="title" label="标题" min-width="220" />
      <el-table-column prop="doc_type" label="文种" width="100" />
      <el-table-column label="密级" width="110">
        <template #default="{ row }"><span class="badge" :class="row.security_level">{{ row.security_level }}</span></template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="110" />
      <el-table-column prop="chunk_count" label="切片数" width="90" />
      <el-table-column label="权限标签" min-width="180">
        <template #default="{ row }">{{ (row.permission_tags || []).join(', ') || '—' }}</template>
      </el-table-column>
    </el-table>
  </div>

  <el-dialog v-model="dialog" title="接入文档（我方标准元数据）" width="540px">
    <el-form :model="form" label-width="92px">
      <el-form-item label="业务主键"><el-input v-model="form.external_id" placeholder="第三方业务主键（幂等）" /></el-form-item>
      <el-form-item label="标题"><el-input v-model="form.title" /></el-form-item>
      <el-form-item label="文种"><el-input v-model="form.doc_type" /></el-form-item>
      <el-form-item label="发文机关"><el-input v-model="form.issuer" /></el-form-item>
      <el-form-item label="文号"><el-input v-model="form.doc_no" /></el-form-item>
      <el-form-item label="密级">
        <el-select v-model="form.security_level">
          <el-option label="公开" value="PUBLIC" /><el-option label="内部" value="INTERNAL" />
          <el-option label="受限" value="RESTRICTED" /><el-option label="秘密" value="SECRET" />
        </el-select>
      </el-form-item>
      <el-form-item label="权限标签"><el-input v-model="form.permission_tags" placeholder="scope:dept:xxx,scope:role:yyy" /></el-form-item>
      <el-form-item label="正文"><el-input v-model="form.content" type="textarea" :rows="4" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="submit">提交</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; margin-bottom: 14px; }
.ttl { font-size: 18px; color: var(--text-strong); flex: 1; }
</style>
