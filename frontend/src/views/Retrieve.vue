<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { generate, retrieve, type SecurityLevel } from '@/api/client'

const query = ref('请拟一份关于安全生产的通知，需包含隐患排查与应急值守要求')
const loading = ref(false)
const citations = ref<any[]>([])
const answer = ref('')
const degraded = ref(false)

// 调用方上下文控制（演示密级 / 权限下推）
const ctx = reactive({
  max_clearance: 'SECRET' as SecurityLevel,
  caller_scope: 'scope:dept:office',
  rerank: false,
})

async function doRetrieve() {
  loading.value = true
  try {
    const r = await retrieve(query.value, {
      top_n: 8,
      max_clearance: ctx.max_clearance,
      caller_scope: ctx.caller_scope ? ctx.caller_scope.split(',').map((s) => s.trim()).filter(Boolean) : [],
      rerank: ctx.rerank,
    })
    citations.value = r.citations || []
    degraded.value = !!r.truncated_by_security
    if (r.truncated_by_security) ElMessage.warning('部分结果因密级/权限被过滤')
  } finally { loading.value = false }
}

async function doGenerate() {
  loading.value = true
  try {
    const r = await generate(query.value, {
      max_clearance: ctx.max_clearance,
      caller_scope: ctx.caller_scope ? ctx.caller_scope.split(',').map((s) => s.trim()).filter(Boolean) : [],
      rerank: ctx.rerank,
    })
    answer.value = r.answer
    citations.value = r.citations || []
    if (r.sensitive_hits?.length) ElMessage.warning('敏感词命中：' + r.sensitive_hits.join('、'))
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

    <div class="ctx panel">
      <span class="ctx-ttl text-muted">调用方上下文（上游网关下发）</span>
      <div class="ctx-row">
        <label>最大密级</label>
        <el-select v-model="ctx.max_clearance" style="width: 130px">
          <el-option label="公开" value="PUBLIC" /><el-option label="内部" value="INTERNAL" />
          <el-option label="受限" value="RESTRICTED" /><el-option label="秘密" value="SECRET" />
        </el-select>
        <label>权限标签</label>
        <el-input v-model="ctx.caller_scope" placeholder="scope:dept:office" style="width: 220px" />
        <el-checkbox v-model="ctx.rerank" label="启用重排(超时自动降级)" />
      </div>
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
      <div class="ttl">生成结果（云端 LLM）</div>
      <p class="body">{{ answer }}</p>
    </div>

    <div class="ttl sub">引用材料（{{ citations.length }}）<span v-if="degraded" class="text-muted">· 已按密级/权限下推过滤</span></div>
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
.ctx { padding: 12px 14px; margin-bottom: 12px; }
.ctx-ttl { font-size: 12px; }
.ctx-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; flex-wrap: wrap; }
.ctx-row label { color: var(--text-muted); font-size: 13px; }
.box { margin-bottom: 12px; }
.answer { padding: 14px; margin-bottom: 8px; }
.answer .body { color: var(--text); line-height: 1.7; white-space: pre-wrap; }
.cite { padding: 12px 14px; margin-bottom: 8px; }
.ch { color: var(--text-strong); }
.ch small { color: var(--text-faint); margin-left: 6px; }
.snippet { font-size: 13px; margin: 6px 0; line-height: 1.6; }
.meta { font-size: 12px; color: var(--brand); }
</style>
