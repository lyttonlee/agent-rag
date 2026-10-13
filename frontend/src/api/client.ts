import axios from 'axios'

// 统一 API 客户端：/v1 前缀（与后端 API_PREFIX 对齐），由 vite 代理转发到 8000。
const client = axios.create({
  baseURL: '/v1',
  timeout: 120000,
})

// 调用方上下文：上游网关下发的密级 / 权限，我方在检索/生成阶段强制执行过滤。
export interface CallerContext {
  caller_scope: string[]
  max_clearance: 'PUBLIC' | 'INTERNAL' | 'RESTRICTED' | 'SECRET'
}

export const caller: CallerContext = {
  caller_scope: ['scope:dept:office'],
  max_clearance: 'INTERNAL',
}

export default client

// ============================ 类型定义 ============================
export type SecurityLevel = 'PUBLIC' | 'INTERNAL' | 'RESTRICTED' | 'SECRET'
export type ChunkStrategy = 'paragraph' | 'fixed' | 'sentence' | 'section'
export type JobStatus = 'PENDING' | 'PARSING' | 'CHUNKING' | 'EMBEDDING' | 'INDEXED' | 'FAILED'

export interface DocSummary {
  id: string
  external_id: string
  title: string
  doc_type: string
  issuer: string | null
  doc_no: string | null
  security_level: SecurityLevel
  permission_tags: string[]
  status: string
  chunk_count: number
  created_at: string | null
}

export interface ChunkInfo {
  seq: number
  content: string
  security_level: SecurityLevel
  tokens: number
}

export interface DocDetail extends DocSummary {
  publish_date: string | null
  owner_org: string | null
  source_system: string | null
  chunks: ChunkInfo[]
}

export interface DocStats {
  total_documents: number
  total_chunks: number
  by_status: Record<string, number>
  by_security: Record<string, number>
}

export interface IngestionJob {
  id: string
  filename: string
  status: JobStatus
  progress: number
  total_chunks: number
  processed_chunks: number
  error_message: string | null
  document_id: string | null
  strategy: ChunkStrategy
  chunk_size: number
  chunk_overlap: number
  security_level: SecurityLevel
  created_at: string | null
  updated_at: string | null
}

// ============================ 文档空间 ============================

export interface DocSpace {
  id: string
  name: string
  code: string
  description: string | null
  is_default: boolean
  doc_count: number
  chunk_count: number
  created_at: string | null
  updated_at: string | null
}

export async function listSpaces(): Promise<DocSpace[]> {
  const r = await client.get<DocSpace[]>('/spaces')
  return r.data
}

export async function createSpace(payload: {
  name: string
  code: string
  description?: string
}): Promise<DocSpace> {
  const r = await client.post<DocSpace>('/spaces', payload)
  return r.data
}

export async function updateSpace(
  id: string,
  payload: { name?: string; description?: string },
): Promise<DocSpace> {
  const r = await client.put<DocSpace>(`/spaces/${id}`, payload)
  return r.data
}

export async function deleteSpace(id: string): Promise<void> {
  await client.delete(`/spaces/${id}`)
}

// ============================ 会话（问答/起草共用） ============================

export type ConvType = 'chat' | 'draft'

export interface Conversation {
  id: string
  title: string
  conv_type: ConvType
  space_ids: string[]
  message_count: number
  last_message_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ConversationMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: CitationPayload[]
  sensitive_hits: string[]
  model: string | null
  created_at: string | null
}

export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[]
}

export async function listConversations(convType?: ConvType): Promise<Conversation[]> {
  const r = await client.get<Conversation[]>('/conversations', {
    params: convType ? { conv_type: convType } : {},
  })
  return r.data
}

export async function createConversation(payload: {
  conv_type: ConvType
  title?: string
  space_ids?: string[]
}): Promise<Conversation> {
  const r = await client.post<Conversation>('/conversations', payload)
  return r.data
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const r = await client.get<ConversationDetail>(`/conversations/${id}`)
  return r.data
}

export async function updateConversation(
  id: string,
  payload: { title?: string; space_ids?: string[] },
): Promise<Conversation> {
  const r = await client.put<Conversation>(`/conversations/${id}`, payload)
  return r.data
}

export async function deleteConversation(id: string): Promise<void> {
  await client.delete(`/conversations/${id}`)
}

export async function appendMessage(
  id: string,
  payload: {
    role: 'user' | 'assistant'
    content: string
    citations?: CitationPayload[]
    sensitive_hits?: string[]
    model?: string
  },
): Promise<ConversationMessage> {
  const r = await client.post<ConversationMessage>(`/conversations/${id}/messages`, payload)
  return r.data
}

// ============================ API 封装 ============================

export async function getStats(spaceId?: string): Promise<DocStats> {
  const r = await client.get<DocStats>('/documents/stats', {
    params: spaceId ? { space_id: spaceId } : {},
  })
  return r.data
}

export interface ListQuery {
  doc_type?: string
  security_level?: string
  space_id?: string
  status?: string
  q?: string
  page?: number
  size?: number
}

export async function listDocuments(q: ListQuery = {}): Promise<DocSummary[]> {
  const r = await client.get<DocSummary[]>('/documents', { params: q })
  return r.data
}

export async function getDocument(id: string): Promise<DocDetail> {
  const r = await client.get<DocDetail>(`/documents/${id}`)
  return r.data
}

export interface UploadMeta {
  title?: string
  doc_type?: string
  issuer?: string
  doc_no?: string
  publish_date?: string
  security_level?: SecurityLevel
  permission_tags?: string
  space_id?: string
  chunk_strategy?: ChunkStrategy
  chunk_size?: number
  chunk_overlap?: number
}

export async function uploadDocument(file: File, meta: UploadMeta): Promise<IngestionJob> {
  const form = new FormData()
  form.append('file', file)
  Object.entries(meta).forEach(([k, v]) => {
    if (v !== undefined && v !== null) form.append(k, String(v))
  })
  const r = await client.post<IngestionJob>('/documents/upload', form)
  return r.data
}

export async function getJob(jobId: string): Promise<IngestionJob> {
  const r = await client.get<IngestionJob>(`/documents/jobs/${jobId}`)
  return r.data
}

export async function listJobs(limit = 20): Promise<IngestionJob[]> {
  const r = await client.get<IngestionJob[]>('/documents/jobs', { params: { limit } })
  return r.data
}

export async function retrieve(
  query: string,
  opts: {
    top_n?: number
    space_ids?: string[]
    caller_scope?: string[]
    max_clearance?: SecurityLevel
    rerank?: boolean
  },
) {
  const r = await client.post('/retrieve', {
    query,
    top_n: opts.top_n ?? 8,
    space_ids: opts.space_ids ?? null,
    caller_scope: opts.caller_scope ?? [],
    max_clearance: opts.max_clearance ?? 'INTERNAL',
    rerank: opts.rerank ?? true,
  })
  return r.data
}

export async function generate(
  query: string,
  opts: {
    template_doc_type?: string
    conversation_id?: string
    space_ids?: string[]
    caller_scope?: string[]
    max_clearance?: SecurityLevel
    rerank?: boolean
    top_n?: number
  } = {},
) {
  const r = await client.post('/generate', {
    query,
    template_doc_type: opts.template_doc_type,
    conversation_id: opts.conversation_id,
    space_ids: opts.space_ids ?? null,
    caller_scope: opts.caller_scope ?? [],
    max_clearance: opts.max_clearance ?? 'INTERNAL',
    rerank: opts.rerank ?? false,
    top_n: opts.top_n ?? 8,
  })
  return r.data
}

// ============================ 流式生成（SSE） ============================

export interface CitationPayload {
  chunk_id: string
  document_id: string
  title: string
  doc_no: string | null
  snippet: string
  score: number
}

export interface StreamHandlers {
  /** 检索元数据（引用列表）到达；conversationId 为起草会话 ID（首轮后端创建并回传） */
  onMeta?: (model: string, citations: CitationPayload[], truncated: boolean, conversationId?: string) => void
  /** 正文 token 逐片到达 */
  onToken?: (text: string) => void
  /** 思考链片段到达（推理模型） */
  onReasoning?: (text: string) => void
  /** 流式完成（含脱敏后完整答案与敏感词命中） */
  onDone?: (answer: string, sensitiveHits: string[], model: string) => void
  /** 出错（后端推送 error 事件或网络异常） */
  onError?: (message: string) => void
}

export interface StreamGenerateOpts {
  query: string
  template_doc_type?: string
  conversation_id?: string
  space_ids?: string[]
  caller_scope?: string[]
  max_clearance?: SecurityLevel
  rerank?: boolean
  top_n?: number
}

/**
 * 流式生成：fetch 消费 POST /v1/generate/stream（text/event-stream）。
 *
 * 之所以用原生 fetch 而非 axios：axios 不支持流式读取响应体，
 * 必须逐段解析 SSE（data: {...}\n\n）才能实现"实时出字"体验。
 * 返回 AbortController，调用方可随时中止（用户点「停止」）。
 */
export function streamGenerate(opts: StreamGenerateOpts, handlers: StreamHandlers): AbortController {
  const controller = new AbortController()

  const run = async () => {
    let resp: Response
    try {
      resp = await fetch('/v1/generate/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: opts.query,
          template_doc_type: opts.template_doc_type,
          conversation_id: opts.conversation_id,
          space_ids: opts.space_ids ?? null,
          caller_scope: opts.caller_scope ?? [],
          max_clearance: opts.max_clearance ?? 'INTERNAL',
          rerank: opts.rerank ?? false,
          top_n: opts.top_n ?? 8,
        }),
        signal: controller.signal,
      })
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      handlers.onError?.(`网络错误：${e?.message || e}`)
      return
    }
    if (!resp.ok || !resp.body) {
      handlers.onError?.(`服务端错误 HTTP ${resp.status}`)
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE 事件以空行分隔；逐事件解析，做到"到达即渲染"
        let idx: number
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          const line = raw.trim()
          if (!line.startsWith('data:')) continue
          let evt: any
          try {
            evt = JSON.parse(line.slice(5).trim())
          } catch {
            continue
          }
          switch (evt.type) {
            case 'meta':
              handlers.onMeta?.(
                evt.model ?? '',
                evt.citations ?? [],
                !!evt.truncated_by_security,
                evt.conversation_id ?? undefined,
              )
              break
            case 'token':
              handlers.onToken?.(evt.text ?? '')
              break
            case 'reasoning':
              handlers.onReasoning?.(evt.text ?? '')
              break
            case 'done':
              handlers.onDone?.(evt.answer ?? '', evt.sensitive_hits ?? [], evt.model ?? '')
              break
            case 'error':
              handlers.onError?.(evt.message ?? '生成失败')
              break
          }
        }
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      handlers.onError?.(`流式读取中断：${e?.message || e}`)
    }
  }
  void run()
  return controller
}

// ============================ 智能问答（SSE，多轮会话） ============================

export interface StreamChatOpts {
  query: string
  conversation_id?: string
  space_ids?: string[]
  doc_types?: string[]
  caller_scope?: string[]
  max_clearance?: SecurityLevel
  rerank?: boolean
  top_n?: number
}

export interface ChatHandlers {
  /** 会话与检索元数据到达（conversation_id 供前端后续轮次复用） */
  onMeta?: (conversationId: string, model: string, citations: CitationPayload[], truncated: boolean) => void
  onToken?: (text: string) => void
  onReasoning?: (text: string) => void
  /** 流式完成（含脱敏后完整答案、敏感词命中、会话 ID） */
  onDone?: (answer: string, sensitiveHits: string[], model: string, conversationId: string) => void
  onError?: (message: string) => void
}

/**
 * 智能问答流式：fetch 消费 POST /v1/chat/stream（text/event-stream）。
 *
 * 与 streamGenerate 的差异：meta/done 事件携带 conversation_id——
 * 首轮问答后端自动创建会话并回传 ID，前端据此把同一会话的后续轮次
 * 挂在同一 conversation_id 上，实现多轮追问与历史记录。
 */
export function streamChat(opts: StreamChatOpts, handlers: ChatHandlers): AbortController {
  const controller = new AbortController()

  const run = async () => {
    let resp: Response
    try {
      resp = await fetch('/v1/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: opts.query,
          conversation_id: opts.conversation_id,
          space_ids: opts.space_ids ?? null,
          doc_types: opts.doc_types ?? null,
          caller_scope: opts.caller_scope ?? [],
          max_clearance: opts.max_clearance ?? 'INTERNAL',
          rerank: opts.rerank ?? true,
          top_n: opts.top_n ?? 8,
        }),
        signal: controller.signal,
      })
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      handlers.onError?.(`网络错误：${e?.message || e}`)
      return
    }
    if (!resp.ok || !resp.body) {
      handlers.onError?.(`服务端错误 HTTP ${resp.status}`)
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          const line = raw.trim()
          if (!line.startsWith('data:')) continue
          let evt: any
          try {
            evt = JSON.parse(line.slice(5).trim())
          } catch {
            continue
          }
          switch (evt.type) {
            case 'meta':
              handlers.onMeta?.(
                evt.conversation_id ?? '',
                evt.model ?? '',
                evt.citations ?? [],
                !!evt.truncated_by_security,
              )
              break
            case 'token':
              handlers.onToken?.(evt.text ?? '')
              break
            case 'reasoning':
              handlers.onReasoning?.(evt.text ?? '')
              break
            case 'done':
              handlers.onDone?.(
                evt.answer ?? '',
                evt.sensitive_hits ?? [],
                evt.model ?? '',
                evt.conversation_id ?? '',
              )
              break
            case 'error':
              handlers.onError?.(evt.message ?? '问答失败')
              break
          }
        }
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      handlers.onError?.(`流式读取中断：${e?.message || e}`)
    }
  }
  void run()
  return controller
}

// ============================ 配置中心 ============================

export interface ConfigItem {
  category: string
  key: string
  value: Record<string, unknown>
  scope: string | null
  is_active: boolean
}

export async function getConfigItems(category: string): Promise<ConfigItem[]> {
  const r = await client.get<ConfigItem[]>('/config/items', { params: { category } })
  return r.data
}

export async function saveConfigItem(item: ConfigItem): Promise<ConfigItem> {
  const r = await client.put<ConfigItem>('/config/items', item)
  return r.data
}

export async function deleteConfigItem(category: string, key: string): Promise<void> {
  await client.delete('/config/items', { params: { category, key } })
}

// ============================ 模板库 ============================

export interface TemplateSection {
  key: string
  label: string
  placeholder: string
  required: boolean
}

export interface TemplateLayout {
  red_head: boolean
  font: string
  paper: string
  subtitle: string
  tone: string
  sections: TemplateSection[]
}

export interface TemplateItem {
  key: string
  value: {
    name: string
    layout: TemplateLayout
    content_ref?: string | null
  }
}

export async function listTemplates(): Promise<TemplateItem[]> {
  const r = await client.get<TemplateItem[]>('/templates')
  return r.data
}

export async function uploadTemplate(
  file: File,
  meta: { doc_type?: string; name?: string },
): Promise<TemplateItem> {
  const form = new FormData()
  form.append('file', file)
  if (meta.doc_type) form.append('doc_type', meta.doc_type)
  if (meta.name) form.append('name', meta.name)
  const r = await client.post<TemplateItem>('/templates/upload', form)
  return r.data
}

export async function saveTemplate(
  docType: string,
  value: { name: string; layout: TemplateLayout },
): Promise<TemplateItem> {
  const r = await client.post<TemplateItem>('/templates', {
    category: 'template',
    key: docType,
    value,
  })
  return r.data
}

export async function deleteTemplate(docType: string): Promise<void> {
  await client.delete(`/templates/${encodeURIComponent(docType)}`)
}

export async function seedTemplates(): Promise<{ added: number; message: string }> {
  const r = await client.post('/templates/seed')
  return r.data
}

export async function downloadTemplate(docType: string): Promise<void> {
  const r = await client.get(`/templates/${encodeURIComponent(docType)}/file`, {
    responseType: 'blob',
  })
  const url = URL.createObjectURL(r.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${docType}_template`
  a.click()
  URL.revokeObjectURL(url)
}

// ============================ 审计日志 ============================

export interface AuditLog {
  id: string
  actor: string
  actor_ip: string | null
  action: string
  method: string | null
  endpoint: string | null
  subject_type: string | null
  subject_id: string | null
  object_type: string | null
  object_id: string | null
  result: string
  status_code: number | null
  detail: string
  duration_ms: number | null
  created_at: string | null
}

export interface AuditStats {
  total: number
  recent_24h: number
  by_action: Record<string, number>
  by_result: Record<string, number>
}

export interface AuditQuery {
  actor?: string
  action?: string
  object_type?: string
  result?: string
  page?: number
  size?: number
}

export async function listAuditLogs(q: AuditQuery = {}): Promise<AuditLog[]> {
  const r = await client.get<AuditLog[]>('/audit/logs', { params: q })
  return r.data
}

export async function getAuditStats(): Promise<AuditStats> {
  const r = await client.get<AuditStats>('/audit/stats')
  return r.data
}
