<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { generate, retrieve, SECURITY_LEVEL_OPTIONS, type SecurityLevel } from '@/api/client'

const query = ref('请拟一份关于安全生产的通知，需包含隐患排查与应急值守要求')
const loading = ref(false)
const citations = ref<any[]>([])
const answer = ref('')
const degraded = ref(false)
// 检索过程调试信息（debug=true 时后端返回 trace，用于排查相似度/rerank 质量）
const trace = ref<any>(null)

// 调用方上下文控制（演示密级 / 权限下推）
const ctx = reactive({
  max_clearance: 'TOP_SECRET' as SecurityLevel,
  caller_scope: 'scope:dept:office',
  rerank: false,
  debug: true,
})

async function doRetrieve() {
  loading.value = true
  try {
    const r = await retrieve(query.value, {
      top_n: 8,
      max_clearance: ctx.max_clearance,
      caller_scope: ctx.caller_scope ? ctx.caller_scope.split(',').map((s) => s.trim()).filter(Boolean) : [],
      rerank: ctx.rerank,
      debug: ctx.debug,
    })
    citations.value = r.citations || []
    trace.value = r.trace || null
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

// 向量距离着色：越小越相关（绿），越大越不相关（红）
function distClass(d: number | null | undefined): string {
  if (d == null) return ''
  return d < 0.3 ? 'd-good' : d < 0.6 ? 'd-mid' : 'd-bad'
}
// rerank 相关分着色：越高越相关
function scoreClass(s: number | null | undefined): string {
  if (s == null) return ''
  return s >= 0.5 ? 'r-high' : s >= 0.2 ? 'r-mid' : 'r-low'
}
doRetrieve()
</script>

<template>
  <div class="panel pad page-fill">
    <div class="head">
      <div class="ttl serif">智能检索问答</div>
      <span class="badge INTERNAL">强制引用溯源 · 服务端密级下推</span>
    </div>

    <div class="ctx panel">
      <span class="ctx-ttl text-muted">调用方上下文（上游网关下发）</span>
      <div class="ctx-row">
        <label>最大密级</label>
        <el-select v-model="ctx.max_clearance" style="width: 130px">
          <el-option v-for="lv in SECURITY_LEVEL_OPTIONS" :key="lv.value" :label="lv.label" :value="lv.value" />
        </el-select>
        <label>权限标签</label>
        <el-input v-model="ctx.caller_scope" placeholder="scope:dept:office" style="width: 220px" />
        <el-checkbox v-model="ctx.rerank" label="启用重排(超时自动降级)" />
        <el-checkbox v-model="ctx.debug" label="调试(返回检索过程)" />
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

    <!-- 检索过程调试面板：分阶段还原一次检索，重点看 rerank 模型返回的原始分 -->
    <div v-if="trace" class="trace panel">
      <div class="trace-head">
        <span class="ttl">检索过程 · 调试</span>
        <span class="text-muted mono">
          向量 {{ trace.timing_ms?.vector_ms ?? '-' }}ms · 关键词 {{ trace.timing_ms?.keyword_ms ?? '-' }}ms
          · rerank {{ trace.rerank?.latency_ms != null ? trace.rerank.latency_ms + 'ms' : '-' }}
        </span>
      </div>

      <div class="trace-sec">
        <div class="sec-ttl">① Query 向量</div>
        <div class="kv mono">{{ trace.embedding?.model }} · dim={{ trace.embedding?.dim }} · L2={{ trace.embedding?.l2_norm }} · {{ trace.embedding?.latency_ms }}ms</div>
      </div>

      <div class="trace-sec">
        <div class="sec-ttl">② 向量召回 <span class="text-muted">(cosine 距离，越小越相关)</span>
          <span class="tag" :class="distClass(trace.top_vector_distance)">top1={{ trace.top_vector_distance?.toFixed?.(3) ?? '-' }}</span>
        </div>
        <div class="trace-row" v-for="v in (trace.vector_recall || []).slice(0, 8)" :key="v.chunk_id">
          <span class="rn">#{{ v.rank }}</span>
          <span class="mono num" :class="distClass(v.dist)">{{ v.dist?.toFixed?.(3) }}</span>
          <span class="tsnip" :title="v.title">{{ v.snippet }}</span>
        </div>
      </div>

      <div class="trace-sec">
        <div class="sec-ttl">③ 关键词召回 <span class="text-muted">(ts_rank，越大越相关)</span>
          <span class="tag">命中={{ trace.keyword_hits }}</span>
        </div>
        <div class="trace-row" v-for="k in (trace.keyword_recall || []).slice(0, 8)" :key="k.chunk_id">
          <span class="rn">#{{ k.rank }}</span>
          <span class="mono num">{{ k.ts_rank?.toFixed?.(4) }}</span>
          <span class="tsnip" :title="k.title">{{ k.snippet }}</span>
        </div>
      </div>

      <div class="trace-sec">
        <div class="sec-ttl">④ RRF 融合 <span class="text-muted">(fused={{ trace.fused_count }})</span></div>
        <div class="trace-row" v-for="f in (trace.rrf_fusion || []).slice(0, 8)" :key="f.chunk_id">
          <span class="rn">#{{ f.rank }}</span>
          <span class="mono num">{{ f.rrf?.toFixed?.(5) }}</span>
          <span class="tsnip">{{ f.snippet }}</span>
        </div>
      </div>

      <div class="trace-sec rerank-sec">
        <div class="sec-ttl">⑤ rerank 模型返回 <span class="text-muted">(原始 relevance_score，排查重点)</span>
          <span class="tag" :class="trace.rerank?.degraded ? 'tag-err' : (trace.rerank?.enabled ? 'tag-ok' : '')">
            {{ trace.rerank?.degraded ? '已降级→RRF' : (trace.rerank?.enabled ? trace.rerank.model : '未启用') }}
          </span>
        </div>
        <div v-if="trace.rerank?.error" class="trace-err mono">{{ trace.rerank.error }}</div>
        <div v-else-if="trace.rerank?.note" class="kv mono">{{ trace.rerank.note }}</div>
        <div class="trace-row" v-for="(s, i) in (trace.rerank?.scores || [])" :key="i">
          <span class="rn">[{{ s.index }}]</span>
          <span class="mono num" :class="scoreClass(s.score)">{{ s.score?.toFixed?.(4) }}</span>
          <span class="tsnip">{{ s.snippet }}</span>
        </div>
      </div>

      <div class="trace-sec">
        <div class="sec-ttl">⑥ 最终排序 <span class="text-muted">({{ trace.rerank?.enabled ? 'rerank 分' : 'RRF 分' }})</span></div>
        <div class="trace-row" v-for="f in (trace.final || []).slice(0, 8)" :key="f.chunk_id">
          <span class="rn">#{{ f.rank }}</span>
          <span class="mono num" :class="scoreClass(f.score)">{{ f.score?.toFixed?.(4) }}</span>
          <span class="tsnip" :title="f.title">{{ f.title }}</span>
        </div>
      </div>

      <div class="trace-sec" v-if="trace.weak_signal || trace.rewrite || trace.parent_inject">
        <div class="sec-ttl">⑦ 增强判定</div>
        <div class="kv mono" v-if="trace.weak_signal">
          弱信号 is_weak={{ trace.weak_signal.is_weak }} · rerank_top1={{ trace.weak_signal.rerank_top1 }} · 注入阈值={{ trace.weak_signal.parent_inject_min_rerank }}
        </div>
        <div class="kv mono" v-if="trace.rewrite">
          query 改写 triggered={{ trace.rewrite.triggered }}
          <span v-if="trace.rewrite.triggered"> · strategy={{ trace.rewrite.strategy }} · variants={{ trace.rewrite.variants?.length }} · rounds={{ trace.rewrite.rounds }}</span>
        </div>
        <div class="kv mono" v-if="trace.parent_inject">
          父块注入 triggered={{ trace.parent_inject.triggered }}
          <span v-if="trace.parent_inject.triggered"> · 注入={{ trace.parent_inject.injected_count }}</span>
        </div>
      </div>
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

/* 检索过程调试面板 */
.mono { font-family: var(--font-mono, "SFMono-Regular", Consolas, "Liberation Mono", monospace); }
.num { display: inline-block; min-width: 58px; font-weight: 600; }
.trace { padding: 14px 16px; margin-bottom: 12px; border: 1px solid var(--border, #e5e7eb); }
.trace-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.trace-sec { margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px dashed var(--border, #e5e7eb); }
.trace-sec:last-child { border-bottom: none; margin-bottom: 0; }
.sec-ttl { font-size: 13px; font-weight: 600; color: var(--text-strong); margin-bottom: 6px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.kv { font-size: 12px; color: var(--text-muted); line-height: 1.7; }
.trace-row { display: flex; gap: 10px; align-items: baseline; font-size: 12px; padding: 2px 0; }
.rn { color: var(--text-faint); min-width: 30px; }
.tsnip { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.rerank-sec { background: var(--bg-subtle, rgba(0,0,0,0.02)); border-radius: 6px; padding: 8px 10px; }
.trace-err { color: #dc2626; font-size: 12px; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 4px; background: var(--bg-subtle, #f3f4f6); color: var(--text-muted); font-weight: 500; }
.tag-ok { background: rgba(22,163,74,0.12); color: #16a34a; }
.tag-err { background: rgba(220,38,38,0.12); color: #dc2626; }
.d-good, .r-high { color: #16a34a; }
.d-mid, .r-mid { color: #d97706; }
.d-bad, .r-low { color: #dc2626; }
</style>
