<script setup lang="ts">
import { ref } from 'vue'
import client, { caller } from '@/api/client'

const docType = ref('通知')
const topic = ref('关于开展第三季度安全生产大检查的通知')
const loading = ref(false)
const answer = ref('')
const citations = ref<any[]>([])
const hits = ref<string[]>([])

async function generate() {
  loading.value = true
  try {
    const r = await client.post('/generate', {
      query: topic.value, template_doc_type: docType.value, ...caller,
    })
    answer.value = r.data.answer
    citations.value = r.data.citations || []
    hits.value = r.data.sensitive_hits || []
  } finally { loading.value = false }
}

async function exportDocx() {
  // 直接触发文件下载（后端 /v1/export/docx 返回 FileResponse）
  const r = await client.post('/export/docx', {
    query: topic.value, template_doc_type: docType.value, ...caller,
  }, { responseType: 'blob' })
  const url = URL.createObjectURL(r.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${topic.value.slice(0, 20)}.docx`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="panel pad">
    <div class="head"><div class="ttl serif">智能起草</div>
      <span class="badge INTERNAL">按文种模板 · 引用溯源</span></div>
    <div class="bar">
      <el-select v-model="docType" style="width: 140px">
        <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
      </el-select>
      <el-input v-model="topic" placeholder="起草主题" style="flex: 1" @keyup.enter="generate" />
      <el-button type="primary" :loading="loading" @click="generate">起草</el-button>
      <el-button @click="exportDocx">导出 docx</el-button>
    </div>

    <div class="paper panel">
      <div class="red">{{ docType }}</div>
      <p class="body">{{ answer || '在上方输入主题并点击「起草」，系统将基于知识库检索材料生成带引用的公文草稿。' }}</p>
      <div v-if="citations.length" class="refs">
        <div class="ttl">参考资料</div>
        <div v-for="(c, i) in citations" :key="c.chunk_id">[{{ i + 1 }}] 《{{ c.title }}》</div>
      </div>
      <div v-if="hits.length" class="warn-chip">敏感词命中：{{ hits.join('、') }}（已脱敏）</div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.ttl { font-size: 18px; color: var(--text-strong); }
.bar { display: flex; gap: 10px; margin-bottom: 14px; }
.paper { padding: 28px; max-width: 820px; margin: 0 auto; background: #f7f4ec; color: #1c1c1c; }
.red { color: #c0392b; font-family: var(--font-serif); font-size: 22px; text-align: center; font-weight: 700; margin-bottom: 16px; }
.body { line-height: 1.9; white-space: pre-wrap; min-height: 200px; }
.refs { margin-top: 20px; border-top: 1px solid #d8d2c4; padding-top: 10px; font-size: 13px; color: #444; }
.refs .ttl { font-weight: 600; margin-bottom: 6px; }
.warn-chip { margin-top: 12px; color: #c0392b; font-size: 13px; }
</style>
