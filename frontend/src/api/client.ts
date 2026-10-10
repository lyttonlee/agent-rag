import axios from 'axios'

// 统一 API 客户端：/v1 前缀（与后端 API_PREFIX 对齐），由 vite 代理转发到 8000。
const client = axios.create({
  baseURL: '/v1',
  timeout: 60000,
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
