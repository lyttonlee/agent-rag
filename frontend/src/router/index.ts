import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from '@/api/client'

const routes = [
  // 登录/注册为独立全屏页（无侧边栏壳，meta.public 由 App.vue 识别后裸渲染）
  { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { title: '登录', public: true } },
  { path: '/register', name: 'register', component: () => import('@/views/Register.vue'), meta: { title: '注册', public: true } },
  { path: '/', redirect: '/overview' },
  { path: '/overview', name: 'overview', component: () => import('@/views/Overview.vue'), meta: { title: '概览' } },
  { path: '/docs', name: 'docs', component: () => import('@/views/DocLibrary.vue'), meta: { title: '文档库' } },
  { path: '/retrieve', name: 'retrieve', component: () => import('@/views/Retrieve.vue'), meta: { title: '智能检索问答' } },
  { path: '/chat', name: 'chat', component: () => import('@/views/Chat.vue'), meta: { title: '智能问答' } },
  { path: '/draft', name: 'draft', component: () => import('@/views/Draft.vue'), meta: { title: '智能起草' } },
  { path: '/templates', name: 'templates', component: () => import('@/views/Templates.vue'), meta: { title: '模板库' } },
  { path: '/auth', name: 'auth', component: () => import('@/views/AuthManager.vue'), meta: { title: '认证管理' } },
  { path: '/config', name: 'config', component: () => import('@/views/Config.vue'), meta: { title: '配置中心' } },
  { path: '/audit', name: 'audit', component: () => import('@/views/AuditLog.vue'), meta: { title: '审计日志' } },
  { path: '/ops', name: 'ops', component: () => import('@/views/Ops.vue'), meta: { title: '运维台' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局前置守卫：
// - 未登录访问业务页 → 跳登录页（带上原路径，登录后回跳）；
// - 已登录访问登录/注册页 → 回主页（避免登录态下反复看到登录表单）。
router.beforeEach((to) => {
  if (to.meta.public) {
    if (getToken() && (to.path === '/login' || to.path === '/register')) {
      return { path: '/overview' }
    }
    return true
  }
  if (!getToken()) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
