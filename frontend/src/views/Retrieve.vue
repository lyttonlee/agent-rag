<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import client, { caller } from '@/api/client'

const query = ref('请拟一份关于安全生产的通知，需包含隐患排查与应急值守要求')
const loading = ref(false)
const citations = ref<any[]>([])
const answer = ref('')

async function doRetrieve() {
  loading.value = true
  try {
    const r = await client.post('/retrieve', { query: query.value, top_n: 8, ...caller })
    citations.value = r.data.citations || []
    if (r.data.truncated_by_security) ElMessage.warning('部分结果因密级/权限被过滤')
  } finally { loading.value = false }
}
async function doGenerate() {
  loading.value = true
  try {
    const r = await client.post('/generate', { query: query.value, ...caller })
    answer.value = r.data.answer
    citations.value = r.data.citations || []
    if (r.data.sensitive_hits?.length) ElMessage.warning('敏感词命中：' + r.data.sensitive_hits.join('、'))
  } finally { loading.value = false }
}
doRetrieve()
</script>

<template>
  <div class="panel pad">
    <div class="head">
      <div class="ttl serif">智能检索问答</div>
      <span class="badge INTERNAL">强制引用溯源 · 服务端密级下推</span>
    </div>
    <div class="box">
      <el-input v-model="query" placeholder="输入检索或起草问题" @keyup.enter="doRetrieve">
        <template #append>
          <el-button @click="doRetrieve">检索</el-button>
          <el-button type="primary" @click="doGenerate">检索+生成</el-button>
        </template>
      </el-input>
    </div>

    <div v-if="answer" class="answer panel">
      <div class="ttl">生成结果</div>
      <p class="body">{{ answer }}</p>
    </div>

    <div class="ttl sub">引用材料（{{ citations.length }}）</div>
    <div v-for="(c, i) in citations" :key="c.chunk_id" class="cite panel">
      <div class="ch"><b>[{{ i + 1 }}]</b> 《{{ c.title }}》<small v-if="c.doc_no">{{ c.doc_no }}</small></div>
      <div class="snippet text-muted">{{ c.snippet }}</div>
      <div class="meta">相关度 {{ c.score?.toFixed?.(3) }}</div>
    </div>
  </div>
</template>

<style scoped>
.pad { padding: 16px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.ttl { font-size: 18px; color: var(--text-strong); }
.ttl.sub { margin: 16px 0 8px; }
.box { margin-bottom: 12px; }
.answer { padding: 14px; margin-bottom: 8px; }
.answer .body { color: var(--text); line-height: 1.7; white-space: pre-wrap; }
.cite { padding: 12px 14px; margin-bottom: 8px; }
.ch { color: var(--text-strong); }
.ch small { color: var(--text-faint); margin-left: 6px; }
.snippet { font-size: 13px; margin: 6px 0; line-height: 1.6; }
.meta { font-size: 12px; color: var(--brand); }
</style>
