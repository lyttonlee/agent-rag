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

// ============================ API 封装 ============================

export async function getStats(): Promise<DocStats> {
  const r = await client.get<DocStats>('/documents/stats')
  return r.data
}

export interface ListQuery {
  doc_type?: string
  security_level?: string
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
  opts: { top_n?: number; caller_scope?: string[]; max_clearance?: SecurityLevel; rerank?: boolean },
) {
  const r = await client.post('/retrieve', {
    query,
    top_n: opts.top_n ?? 8,
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
    caller_scope?: string[]
    max_clearance?: SecurityLevel
    rerank?: boolean
    top_n?: number
  } = {},
) {
  const r = await client.post('/generate', {
    query,
    template_doc_type: opts.template_doc_type,
    caller_scope: opts.caller_scope ?? [],
    max_clearance: opts.max_clearance ?? 'INTERNAL',
    rerank: opts.rerank ?? false,
    top_n: opts.top_n ?? 8,
  })
  return r.data
}
