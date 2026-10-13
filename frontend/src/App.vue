<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { caller } from '@/api/client'

const route = useRoute()
const title = computed(() => (route.meta.title as string) || '企业公文知识库')
const env = import.meta.env.MODE
</script>

<template>
  <div class="layout">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="brand-bar">
        <div class="seal">公文</div>
        <div class="brand-text">
          <div class="name">公文知识库</div>
          <div class="sub">RAG 引擎 · 运维台</div>
        </div>
      </div>
      <el-menu class="menu" background-color="transparent" text-color="#c7d4e8"
               active-text-color="#3d7eff" :default-active="route.path" router>
        <el-menu-item index="/overview"><el-icon><DataLine /></el-icon>概览</el-menu-item>
        <el-menu-item index="/docs"><el-icon><Document /></el-icon>文档库</el-menu-item>
        <el-menu-item index="/retrieve"><el-icon><Search /></el-icon>智能检索问答</el-menu-item>
        <el-menu-item index="/chat"><el-icon><ChatDotRound /></el-icon>智能问答</el-menu-item>
        <el-menu-item index="/draft"><el-icon><EditPen /></el-icon>智能起草</el-menu-item>
        <el-menu-item index="/templates"><el-icon><Files /></el-icon>模板库</el-menu-item>
        <el-menu-item index="/config"><el-icon><Setting /></el-icon>配置中心</el-menu-item>
        <el-menu-item index="/audit"><el-icon><Notebook /></el-icon>审计日志</el-menu-item>
        <el-menu-item index="/ops"><el-icon><Monitor /></el-icon>运维台</el-menu-item>
      </el-menu>
      <div class="sidebar-foot text-muted">v1.2.0 · 内网部署</div>
    </aside>

    <!-- 主区 -->
    <div class="main">
      <header class="topbar">
        <div class="crumb serif">{{ title }}</div>
        <div class="spacer" />
        <span class="badge INTERNAL">密级：内部</span>
        <span class="env-chip">{{ env }}</span>
        <span class="scope mono text-muted">{{ caller.caller_scope.join(',') || 'no-scope' }}</span>
      </header>
      <main class="content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout { display: flex; height: 100%; }
.sidebar {
  width: 232px; background: var(--bg-shell); border-right: 1px solid var(--line);
  display: flex; flex-direction: column; padding: 14px 10px;
}
.brand-bar { display: flex; align-items: center; gap: 10px; padding: 6px 8px 16px; }
.seal {
  width: 38px; height: 38px; border-radius: 8px; background: var(--seal);
  color: #fff; font-family: var(--font-serif); font-weight: 700;
  display: flex; align-items: center; justify-content: center; font-size: 16px;
}
.brand-text .name { color: var(--text-strong); font-weight: 600; }
.brand-text .sub { color: var(--text-faint); font-size: 12px; }
.menu { border-right: none; flex: 1; }
.sidebar-foot { font-size: 12px; padding: 10px 8px 0; border-top: 1px solid var(--line); }
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar {
  height: 56px; display: flex; align-items: center; gap: 12px; padding: 0 20px;
  border-bottom: 1px solid var(--line); background: var(--bg-shell);
}
.crumb { font-size: 18px; color: var(--text-strong); }
.spacer { flex: 1; }
.env-chip {
  font-size: 12px; padding: 2px 8px; border-radius: 6px;
  background: var(--bg-surface-2); color: var(--text-muted);
}
.scope { font-size: 12px; }
/* 内容区：flex column 使页面根容器可通过 .page-fill 铺满视口 */
.content { flex: 1; overflow: auto; padding: 20px; display: flex; flex-direction: column; }
</style>
