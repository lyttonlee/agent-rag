import axios from 'axios'
import { ref } from 'vue'

// 统一 API 客户端：/v1 前缀（与后端 API_PREFIX 对齐），由 vite 代理转发到 8000。
const client = axios.create({
  baseURL: '/v1',
  timeout: 120000,
})

// 调用方上下文：上游网关下发的密级 / 权限，我方在检索/生成阶段强制执行过滤。
export interface CallerContext {
  caller_scope: string[]
  max_clearance: SecurityLevel
}

export const caller: CallerContext = {
  caller_scope: ['scope:dept:office'],
  max_clearance: 'INTERNAL',
}

// ============================ 认证（2026-10-13） ============================
// 登录态管理：JWT 存 localStorage；请求拦截器统一附加 Authorization 头；
// 响应拦截器捕获 401（未登录/令牌过期）→ 清令牌并跳转登录页。

const TOKEN_KEY = 'gov_doc_token'

export interface AuthUser {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'operator' | 'viewer'
  enabled: boolean
  last_login_at: string | null
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// 请求拦截：附加 JWT
client.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 响应拦截：401 → 清令牌跳登录（避免在拦截器里直接用 router 单例，懒取防初始化顺序问题）
client.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      setToken(null)
      const path = window.location.pathname
      if (path !== '/login') {
        window.location.assign(`/login?redirect=${encodeURIComponent(path)}`)
      }
    }
    return Promise.reject(error)
  },
)

/** 当前登录用户（跨组件共享：登录页写入，App 壳/菜单读取） */
export const currentUser = ref<AuthUser | null>(null)

export async function login(username: string, password: string): Promise<{ access_token: string; user: AuthUser }> {
  const { data } = await client.post('/auth/login', { username, password })
  setToken(data.access_token)
  currentUser.value = data.user
  return data
}

/**
 * 运维台用户自助注册（用户 2026-10-14 需求）。
 * 注册成功后端直接下发 JWT（注册即登录），前端落同样的登录态即可进主界面。
 * 用户名冲突返回 HTTP 409，detail 由调用方展示。
 */
export async function register(username: string, password: string, displayName: string): Promise<{ access_token: string; user: AuthUser }> {
  const { data } = await client.post('/auth/register', {
    username,
    password,
    display_name: displayName,
  })
  setToken(data.access_token)
  currentUser.value = data.user
  return data
}

export function logout(): void {
  setToken(null)
  currentUser.value = null
  window.location.assign('/login')
}

export async function fetchMe(): Promise<AuthUser> {
  const { data } = await client.get('/auth/me')
  currentUser.value = data
  return data
}

// ---- 第三方客户端管理 ----
export interface ApiClientItem {
  client_id: string
  name: string
  key_prefix: string
  scopes: string[]
  max_clearance: SecurityLevel
  enabled: boolean
  expires_at: string | null
  created_by: string
  last_used_at: string | null
  created_at: string | null
}

export async function listApiClients(): Promise<ApiClientItem[]> {
  const { data } = await client.get('/auth/clients')
  return data
}

export async function registerApiClient(payload: {
  name: string
  scopes: string[]
  max_clearance: SecurityLevel
}): Promise<ApiClientItem & { api_key: string }> {
  const { data } = await client.post('/auth/clients/register', payload)
  return data
}

export async function setClientEnabled(clientId: string, enabled: boolean): Promise<ApiClientItem> {
  const { data } = await client.post(`/auth/clients/${clientId}/${enabled ? 'enable' : 'disable'}`)
  return data
}

export default client

// ============================ 类型定义 ============================
/** 我方标准密级枚举（六级，2026-10-15 起，与后端 SECURITY_WEIGHT 对应） */
export type SecurityLevel =
  | 'PUBLIC'
  | 'INTERNAL'
  | 'COMMERCIAL'
  | 'CORE_COMMERCIAL'
  | 'CLASSIFIED'
  | 'TOP_SECRET'

/** 密级下拉选项（中文标签与后端 SECURITY_LEVEL_LABELS 一致，单一事实源） */
export const SECURITY_LEVEL_OPTIONS: { label: string; value: SecurityLevel }[] = [
  { label: '非密', value: 'PUBLIC' },
  { label: '内部', value: 'INTERNAL' },
  { label: '普通商密', value: 'COMMERCIAL' },
  { label: '核心商密', value: 'CORE_COMMERCIAL' },
  { label: '密级', value: 'CLASSIFIED' },
  { label: '机密', value: 'TOP_SECRET' },
]

/** 密级中文名映射（统计图表 / 徽章展示用） */
export const SECURITY_LEVEL_LABELS: Record<SecurityLevel, string> = {
  PUBLIC: '非密',
  INTERNAL: '内部',
  COMMERCIAL: '普通商密',
  CORE_COMMERCIAL: '核心商密',
  CLASSIFIED: '密级',
  TOP_SECRET: '机密',
}
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
  /** 所属文档空间 ID / 名称（名称由后端冗余返回） */
  space_id: string | null
  space_name: string | null
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
  /** 父空间 ID（层级结构）；null = 根级空间 */
  parent_id: string | null
  /** 所有者类型：system=公共（默认空间）/ user=运维用户 / client=第三方 */
  owner_type: 'system' | 'user' | 'client'
  /** 所有者 ID（user.id / client.client_id）；system 为 null */
  owner_id: string | null
  /** 所有者展示名（冗余存储） */
  owner_name: string | null
  /** 当前调用方是否为创建者（前端据此开放编辑/删除） */
  is_mine: boolean
  /** 当前调用方是否可向该空间上传文档 */
  can_upload: boolean
  doc_count: number
  chunk_count: number
  created_at: string | null
  updated_at: string | null
}

/** 意图中文标签（与后端 agent.intents 注册表一致） */
export const INTENT_LABELS: Record<string, string> = {
  direct_qa: '确切回答',
  complex_task: '复杂任务',
  draft: '公文起草',
  chitchat: '闲聊',
  out_of_scope: '超出范围',
  deep_reason: '深度推理',
}

export function intentLabelOf(intent: string | null | undefined): string {
  if (!intent) return ''
  return INTENT_LABELS[intent] || intent
}

export async function listSpaces(): Promise<DocSpace[]> {
  const r = await client.get<DocSpace[]>('/spaces')
  return r.data
}

export async function createSpace(payload: {
  name: string
  code: string
  description?: string
  parent_id?: string | null
}): Promise<DocSpace> {
  const r = await client.post<DocSpace>('/spaces', payload)
  return r.data
}

export async function updateSpace(
  id: string,
  payload: { name?: string; description?: string; parent_id?: string | null },
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
  doc_type: string | null
  space_ids: string[]
  message_count: number
  last_message_at: string | null
  created_at: string | null
  updated_at: string | null
  /** 搜索命中片段（keyword 命中消息内容时后端返回，供历史栏高亮） */
  match_snippet?: string | null
}

export interface ConversationMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: CitationPayload[]
  sensitive_hits: string[]
  model: string | null
  /** Agent 意图标识（assistant 消息；每轮独立留痕，不被后续轮次覆盖） */
  intent: string | null
  /** 模型思考链全文（回答结束后不丢失，可折叠展开查看） */
  reasoning: string | null
  created_at: string | null
}

export interface ConversationDetail extends Conversation {
  doc_type: string | null
  messages: ConversationMessage[]
}

/**
 * 会话列表（历史栏）。
 * opts.keyword：模糊搜索（标题 + 消息内容，命中消息返回 match_snippet）；
 * opts.id：按明确 ID 精确查找（不存在时后端 404）。
 */
export async function listConversations(
  convType?: ConvType,
  opts?: { keyword?: string; id?: string },
): Promise<Conversation[]> {
  const params: Record<string, string> = {}
  if (convType) params.conv_type = convType
  if (opts?.keyword) params.keyword = opts.keyword
  if (opts?.id) params.id = opts.id
  const r = await client.get<Conversation[]>('/conversations', { params })
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

/** 导出请求：按会话 + 消息精确定位已生成内容（不重新生成） */
export interface ExportDocxRequest {
  conversation_id: string
  message_id?: string | null
  template_code?: string | null
}

/** 导出某一轮已生成的起草内容为 docx（按 conversation_id + message_id 定位）。 */
export async function exportDocx(req: ExportDocxRequest): Promise<void> {
  const r = await client.post('/export/docx', req, { responseType: 'blob' })
  const url = URL.createObjectURL(r.data as Blob)
  const a = document.createElement('a')
  a.href = url
  // 后端 Content-Disposition 的 filename 已含中文名；前端兜底用会话导出名
  const disposition = (r.headers['content-disposition'] as string) || ''
  const m = disposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i)
  a.download = m ? decodeURIComponent(m[1]) : '公文导出.docx'
  a.click()
  URL.revokeObjectURL(url)
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
  /** @deprecated 双粒度模式下无效；切片尺寸改由配置中心 small/large token 管理。
   *  仅当配置中心关闭 dual_granularity 回退单粒度时才生效（字符单位）。 */
  chunk_size?: number
  /** @deprecated 同上，仅单粒度回退路径生效。 */
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
    debug?: boolean
  },
) {
  const r = await client.post('/retrieve', {
    query,
    top_n: opts.top_n ?? 8,
    space_ids: opts.space_ids ?? null,
    caller_scope: opts.caller_scope ?? [],
    max_clearance: opts.max_clearance ?? 'INTERNAL',
    rerank: opts.rerank ?? true,
    debug: opts.debug ?? false,
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

/** Agent 计划步骤（complex_task 拆步 / 起草阶段透出，过程对用户可见） */
export interface PlanStep {
  id: string
  action: 'retrieve' | 'generate' | 'redact'
  query: string
  status?: 'pending' | 'running' | 'done' | 'failed'
  /** 步骤结论摘要（step_end 事件带回，done/failed 时展示） */
  summary?: string
}

/** 流式 meta 事件中的 Agent 信息（问答/起草共用） */
export interface AgentMeta {
  conversation_id?: string
  model: string
  intent?: string | null
  intent_label?: string | null
  agent_mode?: boolean
  no_result?: boolean
  disclaimer?: boolean
  truncated_by_security: boolean
  citations: CitationPayload[]
}

/** 流式 done 事件中的 Agent 收尾信息 */
export interface DoneExtra {
  intent?: string | null
  no_result?: boolean
  disclaimer?: boolean
}

export interface StreamHandlers {
  /** 检索元数据（引用列表）到达；conversationId 为起草会话 ID；meta 携带 Agent 意图信息 */
  onMeta?: (model: string, citations: CitationPayload[], truncated: boolean, conversationId?: string, meta?: AgentMeta) => void
  /** 步骤计划到达（起草链路透出 理解需求/收集依据/成稿/脱敏 时间线，过程可见） */
  onPlan?: (steps: PlanStep[]) => void
  /** 某步骤开始执行 */
  onStepStart?: (step: PlanStep) => void
  /** 某步骤执行结束（ok=false 时 summary 为失败原因） */
  onStepEnd?: (stepId: string, ok: boolean, summary: string) => void
  /** 正文 token 逐片到达 */
  onToken?: (text: string) => void
  /** 思考链片段到达（推理模型） */
  onReasoning?: (text: string) => void
  // ---- 深度推理起草（ReAct，2026-10-19）：智能体自主收集依据的 TAO 时间线 ----
  /** ReAct 循环启动；channel=draft 表示起草通道（与问答共用引擎，文案不同） */
  onReactStart?: (info: {
    max_rounds: number
    unlimited_rounds?: boolean
    total_budget_ms: number
    tools: string[]
    channel?: 'qa' | 'draft'
  }) => void
  /** 起草需求理解结果（模型复述诉求 + 自述是否需要查库 + 候选方向） */
  onAnalysis?: (info: ReactAnalysis) => void
  /** 新一轮开始（时间线追加容器） */
  onReactRound?: (round: number, maxRounds: number) => void
  /**
   * 阶段播报：每个耗时动作前后推送，phase 取值
   * start|classify|understand|decide|tool|answer。用于消除"点了没反应"的
   * 静默期——模型每次调用要 1-4 秒，此前这段时间页面完全没有可见进展。
   */
  onPhase?: (info: { phase: string; label: string; round?: number; tool?: string }) => void
  /** 本轮思考（模型显式 Thought 文本） */
  onThought?: (round: number, text: string) => void
  /** 本轮行动（工具 + query；skipped=true 表示未真实执行） */
  onAction?: (round: number, tool: string, query: string, skipped?: boolean) => void
  /** 本轮观察（命中列表 + 质量信号 + 提示语 + 耗时） */
  onObservation?: (
    round: number,
    o: {
      tool: string
      query: string
      hits: ReactObsHit[]
      quality: Record<string, number | null>
      note: string
      elapsed_ms?: number
      hits_total?: number
    },
  ) => void
  /** 循环结束（轮数 / 停止原因 / 累计引用 / 总耗时） */
  onReactEnd?: (info: {
    rounds: number
    stop_reason: string
    stop_label?: string
    citations_total: number
    total_ms?: number
  }) => void
  /** 流式完成（含脱敏后完整答案与敏感词命中）；extra 携带意图/无材料/甄别声明标志 */
  onDone?: (answer: string, sensitiveHits: string[], model: string, extra?: DoneExtra) => void
  /** 出错（后端推送 error 事件或网络异常） */
  onError?: (message: string) => void
}

export interface StreamGenerateOpts {
  query: string
  template_doc_type?: string
  template_code?: string
  conversation_id?: string
  space_ids?: string[]
  caller_scope?: string[]
  max_clearance?: SecurityLevel
  rerank?: boolean
  top_n?: number
  agent_mode?: boolean
  /** 深度推理起草开关：null/undefined=跟随后端配置（默认开启）；false=切回单趟旧链路 */
  deep_reason?: boolean | null
  /** 深度推理档位（仅 deep_reason 生效时使用；轮数不限，档位只影响召回广度） */
  react_mode?: ReactMode | null
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
        headers: { 'Content-Type': 'application/json', ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}) },
        body: JSON.stringify({
          query: opts.query,
          template_doc_type: opts.template_doc_type,
          template_code: opts.template_code ?? null,
          conversation_id: opts.conversation_id,
          space_ids: opts.space_ids ?? null,
          caller_scope: opts.caller_scope ?? [],
          max_clearance: opts.max_clearance ?? 'INTERNAL',
          rerank: opts.rerank ?? false,
          top_n: opts.top_n ?? 8,
          agent_mode: opts.agent_mode ?? null,
          // 深度推理起草（2026-10-19）：缺省 null=跟随后端配置（默认开启），
          // false=单趟旧链路；档位仅影响召回广度（轮数默认不限）
          deep_reason: opts.deep_reason ?? null,
          react_mode: opts.react_mode ?? null,
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
                {
                  conversation_id: evt.conversation_id,
                  model: evt.model ?? '',
                  intent: evt.intent ?? null,
                  intent_label: evt.intent_label ?? null,
                  agent_mode: evt.agent_mode,
                  no_result: !!evt.no_result,
                  disclaimer: !!evt.disclaimer,
                  truncated_by_security: !!evt.truncated_by_security,
                  citations: evt.citations ?? [],
                },
              )
              break
            case 'plan':
              handlers.onPlan?.((evt.steps ?? []).map((s: any) => ({ ...s, status: s.status || 'pending' })))
              break
            case 'step_start':
              handlers.onStepStart?.({ id: evt.id, action: evt.action, query: evt.query, status: 'running' })
              break
            case 'step_end':
              handlers.onStepEnd?.(evt.id ?? '', !!evt.ok, evt.summary ?? '')
              break
            case 'token':
              handlers.onToken?.(evt.text ?? '')
              break
            case 'reasoning':
              handlers.onReasoning?.(evt.text ?? '')
              break
            // ---- 深度推理起草（ReAct，2026-10-19）事件：与问答同构 ----
            case 'react_start':
              handlers.onReactStart?.({
                max_rounds: evt.max_rounds ?? 0,
                unlimited_rounds: evt.unlimited_rounds ?? (evt.max_rounds ?? 0) <= 0,
                total_budget_ms: evt.total_budget_ms ?? 0,
                tools: evt.tools ?? [],
                channel: evt.channel ?? 'draft',
              })
              break
            case 'analysis':
              handlers.onAnalysis?.({
                restate: evt.restate ?? '',
                needs_knowledge: !!evt.needs_knowledge,
                needs_knowledge_reason: evt.needs_knowledge_reason ?? '',
                candidates: evt.candidates ?? [],
                ambiguities: evt.ambiguities ?? [],
              })
              break
            case 'react_round':
              handlers.onReactRound?.(evt.round ?? 1, evt.max_rounds ?? 0)
              break
            case 'phase':
              handlers.onPhase?.({
                phase: evt.phase ?? '',
                label: evt.label ?? '',
                round: evt.round,
                tool: evt.tool,
              })
              break
            case 'thought':
              handlers.onThought?.(evt.round ?? 1, evt.text ?? '')
              break
            case 'action':
              handlers.onAction?.(evt.round ?? 1, evt.tool ?? '', evt.query ?? '', !!evt.skipped)
              break
            case 'observation':
              handlers.onObservation?.(evt.round ?? 1, {
                tool: evt.tool ?? '',
                query: evt.query ?? '',
                hits: evt.hits ?? [],
                quality: evt.quality ?? {},
                note: evt.note ?? '',
                elapsed_ms: evt.elapsed_ms,
                hits_total: evt.hits_total,
              })
              break
            case 'react_end':
              handlers.onReactEnd?.({
                rounds: evt.rounds ?? 0,
                stop_reason: evt.stop_reason ?? 'final',
                stop_label: evt.stop_label,
                citations_total: evt.citations_total ?? 0,
                total_ms: evt.total_ms,
              })
              break
            case 'done':
              handlers.onDone?.(evt.answer ?? '', evt.sensitive_hits ?? [], evt.model ?? '', {
                intent: evt.intent ?? null,
                no_result: !!evt.no_result,
                disclaimer: !!evt.disclaimer,
              })
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

/**
 * 拉取深度推理各阶段提示词全文 + 工具清单（提示词管理面板数据源）。
 *
 * 返回的是**运行时实际使用的那份**（工具清单与轮数已按当前档位注入），
 * 因此面板内容与真实请求完全一致——改 prompt 不用翻代码。
 *
 * @param reactMode 档位 fast/deep；缺省用配置中心默认档
 * @param channel 通道：chat=问答（默认）/ draft=公文起草
 */
export async function getReactPrompts(
  reactMode?: ReactMode | null,
  channel?: 'chat' | 'draft' | null,
): Promise<ReactPromptsResp> {
  const params = new URLSearchParams()
  if (reactMode) params.set('react_mode', reactMode)
  if (channel) params.set('channel', channel)
  const q = params.toString() ? `?${params.toString()}` : ''
  const resp = await fetch(`/v1/chat/react/prompts${q}`, {
    headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
  })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return (await resp.json()) as ReactPromptsResp
}

/**
 * 保存 / 恢复深度推理提示词（admin 权限）。
 *
 * 语义与后端一致：``content`` 非空 = 保存覆盖（立即生效）；空串 = 恢复默认。
 * 后端整批校验后再落库，任何一段非法会整批 400——前端据此提示用户。
 */
export async function saveReactPrompts(
  prompts: { key: string; content: string }[],
): Promise<{ saved: { key: string; content: string; source: string }[]; message: string }> {
  const resp = await fetch('/v1/chat/react/prompts', {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    body: JSON.stringify({ prompts }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`)
  return data
}

// ============================ 智能问答（SSE，多轮会话） ============================
/** 深度推理档位：fast=快速模式（低延迟）/ deep=深度模式（质量优先） */
export type ReactMode = 'fast' | 'deep'

/** ReAct 一轮的观测命中（observation 事件） */
export interface ReactObsHit {
  n: number
  chunk_id?: string
  document_id?: string
  title: string
  doc_no?: string | null
  /** 截断后的摘要（注入模型决策上下文的版本） */
  snippet: string
  /** 完整片段（前端展示用，不截断） */
  full_snippet?: string
  score: number
}

/** ReAct 一轮的行动（action 事件） */
export interface ReactAction {
  tool: string
  query: string
  /** true=被去重/未知工具/缺参跳过，未真实执行 */
  skipped?: boolean
}

/** ReAct 一轮的完整记录（前端时间线数据单元） */
export interface ReactRound {
  round: number
  max_rounds?: number
  thought?: string
  action?: ReactAction
  hits?: ReactObsHit[]
  quality?: Record<string, number | null>
  note?: string
  /** 该轮耗时（毫秒，后端统计，含决策+工具执行） */
  elapsed_ms?: number
  /** 该轮工具返回的命中总数（可能大于注入决策上下文的条数） */
  hits_total?: number
  /** 该轮是否仍在运行（等待 observation 或尚未开始） */
  running?: boolean
}

/** 提问理解阶段结果（analysis 事件） */
export interface ReactAnalysis {
  /** 模型对用户诉求的复述 */
  restate: string
  /** 模型自述是否需要查知识库（仅供参考，不构成路由） */
  needs_knowledge: boolean
  needs_knowledge_reason: string
  /** 候选检索方向 */
  candidates: string[]
  /** 可能的歧义点 */
  ambiguities: string[]
}

/** 深度推理提示词（提示词管理面板） */
export interface ReactPromptItem {
  key: string
  name: string
  /** 触发时机说明（理解一次 / 每轮决策 / 收尾生成 / 只读模板） */
  stage: string
  /** 当前**生效**全文（已注入工具清单等运行时变量；自定义时为覆盖文本） */
  content: string
  /** 代码内置默认全文（"恢复默认"与 diff 对比用） */
  default_content: string
  variables: string[]
  /**
   * custom=已自定义 / default=内置默认 / builtin=只读（结构化逻辑）/
   * external=在其他配置页维护（起草成稿提示词 → 配置中心「提示词模板」）
   */
  source: 'custom' | 'default' | 'builtin' | 'external'
  editable: boolean
}

export interface ReactPromptsResp {
  mode: string
  /** 通道：chat=问答 / draft=公文起草（2026-10-19 双通道） */
  channel: 'chat' | 'draft'
  /** 0 = 不限轮数（2026-10-19 起默认） */
  max_rounds: number
  unlimited_rounds: boolean
  /** 0 = 不限时长 */
  total_budget_ms: number
  /** 连续 N 轮无新引用即强制收尾（取消轮数上限后的收敛护栏） */
  no_progress_limit: number
  retrieve_top_n: number
  obs_hits: number
  /** 起草通道总开关（仅 channel=draft 返回） */
  draft_enabled?: boolean
  tools: { name: string; description: string }[]
  prompts: ReactPromptItem[]
}

export interface StreamChatOpts {
  query: string
  conversation_id?: string
  space_ids?: string[]
  doc_types?: string[]
  caller_scope?: string[]
  max_clearance?: SecurityLevel
  rerank?: boolean
  top_n?: number
  agent_mode?: boolean
  /** 深度推理开关：null/undefined=跟随后端配置（默认全开）；false=切回普通问答 */
  deep_reason?: boolean | null
  /** 深度推理档位（仅 deep_reason 生效时使用） */
  react_mode?: ReactMode | null
}

export interface ChatHandlers {
  /** 会话与检索元数据到达（conversation_id 供前端后续轮次复用；meta 携带意图/无材料标志） */
  onMeta?: (conversationId: string, model: string, citations: CitationPayload[], truncated: boolean, meta?: AgentMeta) => void
  /** 复杂任务计划到达（步骤列表，过程可见） */
  onPlan?: (steps: PlanStep[]) => void
  /** 某步骤开始执行 */
  onStepStart?: (step: PlanStep) => void
  /** 某步骤执行结束（ok=false 时 summary 为失败原因） */
  onStepEnd?: (stepId: string, ok: boolean, summary: string) => void
  onToken?: (text: string) => void
  onReasoning?: (text: string) => void
  // ---- 深度推理（ReAct）事件：TAO 时间线数据源 ----
  /** ReAct 循环启动（档位参数与可用工具清单） */
  onReactStart?: (info: {
    max_rounds: number
    unlimited_rounds?: boolean
    total_budget_ms: number
    tools: string[]
  }) => void
  /** 提问理解结果（模型复述诉求 + 自述是否需要查库 + 候选方向） */
  onAnalysis?: (info: ReactAnalysis) => void
  /** 新一轮开始（时间线追加容器） */
  onReactRound?: (round: number, maxRounds: number) => void
  /**
   * 阶段播报（2026-10-16 新增）：每个耗时动作前后推送，phase 取值
   * start|classify|understand|decide|tool|answer。用于消除"点了没反应"的
   * 静默期——模型每次调用要 1-4 秒，此前这段时间页面完全没有可见进展。
   */
  onPhase?: (info: { phase: string; label: string; round?: number; tool?: string }) => void
  /** 本轮思考（模型显式 Thought 文本） */
  onThought?: (round: number, text: string) => void
  /** 本轮行动（工具 + query；skipped=true 表示未真实执行） */
  onAction?: (round: number, tool: string, query: string, skipped?: boolean) => void
  /** 本轮观察（命中列表 + 质量信号 + 提示语 + 耗时） */
  onObservation?: (
    round: number,
    o: {
      tool: string
      query: string
      hits: ReactObsHit[]
      quality: Record<string, number | null>
      note: string
      elapsed_ms?: number
      hits_total?: number
    },
  ) => void
  /** 循环结束（轮数 / 停止原因 / 累计引用 / 总耗时） */
  onReactEnd?: (info: {
    rounds: number
    stop_reason: string
    stop_label?: string
    citations_total: number
    total_ms?: number
  }) => void
  /** 流式完成（含脱敏后完整答案、敏感词命中、会话 ID、Agent 收尾信息） */
  onDone?: (answer: string, sensitiveHits: string[], model: string, conversationId: string, extra?: DoneExtra) => void
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
        headers: { 'Content-Type': 'application/json', ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}) },
        body: JSON.stringify({
          query: opts.query,
          conversation_id: opts.conversation_id,
          space_ids: opts.space_ids ?? null,
          doc_types: opts.doc_types ?? null,
          caller_scope: opts.caller_scope ?? [],
          max_clearance: opts.max_clearance ?? 'INTERNAL',
          rerank: opts.rerank ?? true,
          top_n: opts.top_n ?? 8,
          agent_mode: opts.agent_mode ?? null,
          deep_reason: opts.deep_reason ?? null,
          react_mode: opts.react_mode ?? null,
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
                {
                  conversation_id: evt.conversation_id,
                  model: evt.model ?? '',
                  intent: evt.intent ?? null,
                  intent_label: evt.intent_label ?? null,
                  agent_mode: evt.agent_mode,
                  no_result: !!evt.no_result,
                  disclaimer: !!evt.disclaimer,
                  truncated_by_security: !!evt.truncated_by_security,
                  citations: evt.citations ?? [],
                },
              )
              break
            case 'plan':
              // 复杂任务计划：步骤列表（老客户端无此分支，自动忽略，向前兼容）
              handlers.onPlan?.((evt.steps ?? []).map((s: any) => ({
                id: s.id, action: s.action, query: s.query, status: s.status ?? 'pending',
              })))
              break
            case 'step_start':
              handlers.onStepStart?.({
                id: evt.id, action: evt.action, query: evt.query, status: 'running',
              })
              break
            case 'step_end':
              handlers.onStepEnd?.(evt.id ?? '', !!evt.ok, evt.summary ?? '')
              break
            case 'token':
              handlers.onToken?.(evt.text ?? '')
              break
            case 'reasoning':
              handlers.onReasoning?.(evt.text ?? '')
              break
            // ---- 深度推理（ReAct）事件：老客户端无此分支，自动忽略 ----
            case 'react_start':
              handlers.onReactStart?.({
                max_rounds: evt.max_rounds ?? 0,
                // 0 = 不限轮数（后端默认，2026-10-19）；老后端无该字段时按 0 推断
                unlimited_rounds: evt.unlimited_rounds ?? (evt.max_rounds ?? 0) <= 0,
                total_budget_ms: evt.total_budget_ms ?? 0,
                tools: evt.tools ?? [],
              })
              break
            case 'analysis':
              handlers.onAnalysis?.({
                restate: evt.restate ?? '',
                needs_knowledge: !!evt.needs_knowledge,
                needs_knowledge_reason: evt.needs_knowledge_reason ?? '',
                candidates: evt.candidates ?? [],
                ambiguities: evt.ambiguities ?? [],
              })
              break
            case 'react_round':
              handlers.onReactRound?.(evt.round ?? 1, evt.max_rounds ?? 4)
              break
            case 'phase':
              handlers.onPhase?.({
                phase: evt.phase ?? '',
                label: evt.label ?? '',
                round: evt.round,
                tool: evt.tool,
              })
              break
            case 'thought':
              handlers.onThought?.(evt.round ?? 1, evt.text ?? '')
              break
            case 'action':
              handlers.onAction?.(evt.round ?? 1, evt.tool ?? '', evt.query ?? '', !!evt.skipped)
              break
            case 'observation':
              handlers.onObservation?.(evt.round ?? 1, {
                tool: evt.tool ?? '',
                query: evt.query ?? '',
                hits: evt.hits ?? [],
                quality: evt.quality ?? {},
                note: evt.note ?? '',
                elapsed_ms: evt.elapsed_ms,
                hits_total: evt.hits_total,
              })
              break
            case 'react_end':
              handlers.onReactEnd?.({
                rounds: evt.rounds ?? 0,
                stop_reason: evt.stop_reason ?? 'max_rounds',
                stop_label: evt.stop_label,
                citations_total: evt.citations_total ?? 0,
                total_ms: evt.total_ms,
              })
              break
            case 'done':
              handlers.onDone?.(
                evt.answer ?? '',
                evt.sensitive_hits ?? [],
                evt.model ?? '',
                evt.conversation_id ?? '',
                {
                  intent: evt.intent ?? null,
                  no_result: !!evt.no_result,
                  disclaimer: !!evt.disclaimer,
                },
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
  /** 模板唯一编码（同一文种可有多条，code 不同） */
  key: string
  value: {
    doc_type: string
    name: string
    layout: TemplateLayout
    content_ref?: string | null
    /** 所有者类型：system=系统标准模板（公共）/ user=运维用户 / client=第三方 */
    owner_type?: 'system' | 'user' | 'client'
    /** 所有者 ID（user.id / client.client_id）；system 为 null */
    owner_id?: string | null
    /** 所有者展示名（冗余存储） */
    owner_name?: string | null
    /** 当前调用方是否为创建者（前端据此开放编辑/删除） */
    is_mine?: boolean
    /** 当前调用方是否可维护（本人模板 / admin 管系统模板） */
    can_edit?: boolean
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
  code: string,
  value: { doc_type: string; name: string; layout: TemplateLayout },
): Promise<TemplateItem> {
  const r = await client.post<TemplateItem>('/templates', {
    category: 'template',
    key: code,
    value,
  })
  return r.data
}

export async function deleteTemplate(code: string): Promise<void> {
  await client.delete(`/templates/${encodeURIComponent(code)}`)
}

export async function seedTemplates(): Promise<{ added: number; message: string }> {
  const r = await client.post('/templates/seed')
  return r.data
}

export async function downloadTemplate(code: string): Promise<void> {
  const r = await client.get(`/templates/${encodeURIComponent(code)}/file`, {
    responseType: 'blob',
  })
  const url = URL.createObjectURL(r.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${code}_template`
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
