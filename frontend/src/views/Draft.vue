<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { caller, streamGenerate, type CitationPayload } from '@/api/client'

const docType = ref('通知')
const topic = ref('关于开展第三季度安全生产大检查的通知')
const loading = ref(false)
const answer = ref('')
const reasoning = ref('')
const showReasoning = ref(false)
const citations = ref<CitationPayload[]>([])
const hits = ref<string[]>([])
const modelName = ref('')
const elapsed = ref(0)
let timer: number | null = null
let controller: AbortController | null = null
let startedAt = 0

function startTimer() {
  startedAt = Date.now()
  elapsed.value = 0
  timer = window.setInterval(() => { elapsed.value = Math.floor((Date.now() - startedAt) / 100) / 10 }, 100)
}
function stopTimer() {
  if (timer) { clearInterval(timer); timer = null }
}

/** 流式生成：SSE 实时出字（meta → token* → done），思考链折叠展示。 */
async function generate() {
  if (!topic.value.trim()) {
    ElMessage.warning('请输入起草主题')
    return
  }
  loading.value = true
  answer.value = ''
  reasoning.value = ''
  citations.value = []
  hits.value = []
  showReasoning.value = false
  startTimer()

  controller = streamGenerate(
    {
      query: topic.value,
      template_doc_type: docType.value,
      caller_scope: caller.caller_scope,
      max_clearance: caller.max_clearance,
      rerank: false,
    },
    {
      onMeta: (model, cits) => {
        modelName.value = model
        citations.value = cits
      },
      onReasoning: (text) => {
        reasoning.value += text
        // 思考链进行中自动展开，让用户看到"正在推理"
        showReasoning.value = true
      },
      onToken: (text) => {
        answer.value += text
        // 正文开始输出后收起思考链，保持文稿区整洁
        showReasoning.value = false
      },
      onDone: (finalAnswer, sensitiveHits) => {
        // 以服务端脱敏后的完整答案为准（流式片段未脱敏）
        answer.value = finalAnswer
        hits.value = sensitiveHits
        loading.value = false
        stopTimer()
        if (sensitiveHits.length) {
          ElMessage.warning(`敏感词命中 ${sensitiveHits.length} 处，已脱敏`)
        }
      },
      onError: (message) => {
        ElMessage.error('生成失败：' + message)
        loading.value = false
        stopTimer()
      },
    },
  )
}

function stop() {
  controller?.abort()
  controller = null
  loading.value = false
  stopTimer()
  ElMessage.info('已停止生成')
}

onBeforeUnmount(() => {
  controller?.abort()
  stopTimer()
})

async function exportDocx() {
  // 直接触发文件下载（后端 /v1/export/docx 返回 FileResponse）
  const r = await fetch('/v1/export/docx', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: topic.value,
      template_doc_type: docType.value,
      caller_scope: caller.caller_scope,
      max_clearance: caller.max_clearance,
    }),
  })
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${topic.value.slice(0, 20)}.docx`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="panel pad">
    <div class="head">
      <div class="ttl serif">智能起草</div>
      <span class="badge INTERNAL">按文种模板 · 引用溯源 · 流式输出</span>
    </div>
    <div class="bar">
      <el-select v-model="docType" style="width: 140px">
        <el-option v-for="t in ['通知','通报','报告','请示','函','纪要','决定','意见']" :key="t" :label="t" :value="t" />
      </el-select>
      <el-input v-model="topic" placeholder="起草主题" style="flex: 1" @keyup.enter="generate" />
      <el-button type="primary" :loading="loading" @click="generate">{{ loading ? '生成中…' : '起草' }}</el-button>
      <el-button v-if="loading" @click="stop">停止</el-button>
      <el-button :disabled="!answer" @click="exportDocx">导出 docx</el-button>
    </div>

    <!-- 流式状态条：模型 / 耗时 / 思考链折叠 -->
    <div v-if="loading || reasoning" class="stream-bar">
      <span v-if="loading" class="live"><i class="dot" />流式生成中</span>
      <span v-if="loading" class="mono text-muted">{{ elapsed.toFixed(1) }}s</span>
      <span v-if="modelName" class="text-muted">模型 {{ modelName }}</span>
      <el-link v-if="reasoning" type="primary" @click="showReasoning = !showReasoning">
        {{ showReasoning ? '收起思考过程' : `查看思考过程（${reasoning.length} 字）` }}
      </el-link>
    </div>
    <div v-if="showReasoning && reasoning" class="reasoning">{{ reasoning }}<span v-if="loading" class="cursor" /></div>

    <div class="paper panel">
      <div class="red">{{ docType }}</div>
      <p class="body">{{ answer || '在上方输入主题并点击「起草」，系统将基于知识库检索材料生成带引用的公文草稿，正文实时流式输出。' }}<span v-if="loading" class="cursor" /></p>
      <div v-if="citations.length" class="refs">
        <div class="ttl">参考资料（{{ citations.length }}）</div>
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
.stream-bar { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; font-size: 13px; }
.live { display: inline-flex; align-items: center; gap: 6px; color: var(--c-success); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c-success); animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
.reasoning {
  max-height: 120px; overflow: auto; padding: 10px 12px; margin-bottom: 12px;
  border: 1px dashed var(--line); border-radius: 8px;
  color: var(--text-faint); font-size: 12px; line-height: 1.6; white-space: pre-wrap;
}
.cursor { display: inline-block; width: 2px; height: 1em; background: var(--brand); margin-left: 2px; vertical-align: text-bottom; animation: blink 0.8s infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.paper { padding: 28px; max-width: 820px; margin: 0 auto; background: #f7f4ec; color: #1c1c1c; }
.red { color: #c0392b; font-family: var(--font-serif); font-size: 22px; text-align: center; font-weight: 700; margin-bottom: 16px; }
.body { line-height: 1.9; white-space: pre-wrap; min-height: 200px; }
.refs { margin-top: 20px; border-top: 1px solid #d8d2c4; padding-top: 10px; font-size: 13px; color: #444; }
.refs .ttl { font-weight: 600; margin-bottom: 6px; }
.warn-chip { margin-top: 12px; color: #c0392b; font-size: 13px; }
</style>
