import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/overview' },
  { path: '/overview', name: 'overview', component: () => import('@/views/Overview.vue'), meta: { title: '概览' } },
  { path: '/docs', name: 'docs', component: () => import('@/views/DocLibrary.vue'), meta: { title: '文档库' } },
  { path: '/retrieve', name: 'retrieve', component: () => import('@/views/Retrieve.vue'), meta: { title: '智能检索问答' } },
  { path: '/draft', name: 'draft', component: () => import('@/views/Draft.vue'), meta: { title: '智能起草' } },
  { path: '/templates', name: 'templates', component: () => import('@/views/Templates.vue'), meta: { title: '模板库' } },
  { path: '/config', name: 'config', component: () => import('@/views/Config.vue'), meta: { title: '配置中心' } },
  { path: '/ops', name: 'ops', component: () => import('@/views/Ops.vue'), meta: { title: '运维台' } },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
