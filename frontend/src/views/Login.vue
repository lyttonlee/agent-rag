<script setup lang="ts">
/**
 * 登录页（用户 2026-10-13 需求：添加用户认证；2026-10-14 升级为独立全屏页）。
 *
 * 独立页契约：路由 meta.public=true，由 App.vue 裸渲染（不带侧边栏/顶栏壳），
 * 本组件自行铺满视口（min-height:100vh，背景取全局 --bg-base）。
 *
 * 用户名 + 口令登录，成功后将 JWT 存入 localStorage 并回跳原页面；
 * 底部提供注册入口（/register），与登录页同风格。
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '@/api/client'

const router = useRouter()
const route = useRoute()
const username = ref('')
const password = ref('')
const loading = ref(false)

async function onSubmit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning('请输入用户名和口令')
    return
  }
  loading.value = true
  try {
    await login(username.value.trim(), password.value)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/overview'
    router.replace(redirect)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '登录失败，请检查用户名与口令')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 背景装饰：低透明度印章水印与网格，避免纯色空旷（纯 CSS，无图片依赖） -->
    <div class="bg-deco" aria-hidden="true" />

    <div class="login-panel panel">
      <div class="brand-bar">
        <div class="seal">公文</div>
        <div class="brand-text">
          <div class="name">公文知识库</div>
          <div class="sub">RAG 引擎 · 运维台</div>
        </div>
      </div>
      <div class="ttl serif">登录</div>
      <p class="hint text-muted">使用运维台账号登录；第三方系统请走 API Key 注册调用（X-API-Key）。</p>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="请输入用户名" autofocus @keyup.enter="onSubmit" />
        </el-form-item>
        <el-form-item label="口令">
          <el-input v-model="password" type="password" show-password placeholder="请输入口令" @keyup.enter="onSubmit" />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" round @click="onSubmit">登 录</el-button>
      </el-form>
      <div class="foot">
        <span class="text-muted">还没有账号？</span>
        <el-link type="primary" :underline="false" @click="router.push('/register')">注册新账号</el-link>
      </div>
    </div>

    <div class="page-foot text-muted">v1.2.0 · 内网部署 · 仅企业秘密级（非国家秘密）</div>
  </div>
</template>

<style scoped>
/* 独立全屏页：不依赖外壳的 .content 容器，自行撑满视口并居中 */
.login-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}
/* 背景装饰：径向渐变光晕 + 等距网格，营造纵深；pointer-events 避免挡表单 */
.bg-deco {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(600px 320px at 50% 18%, rgba(61, 126, 255, 0.14), transparent 70%),
    repeating-linear-gradient(0deg, transparent 0 56px, rgba(255, 255, 255, 0.025) 56px 57px),
    repeating-linear-gradient(90deg, transparent 0 56px, rgba(255, 255, 255, 0.025) 56px 57px);
}
.login-panel {
  width: 380px;
  padding: 32px 36px 28px;
  position: relative;
  z-index: 1;
}
.brand-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.seal {
  width: 40px; height: 40px; border-radius: 8px; background: var(--seal);
  color: #fff; font-family: var(--font-serif); font-weight: 700;
  display: flex; align-items: center; justify-content: center; font-size: 17px;
}
.brand-text .name { color: var(--text-strong); font-weight: 600; font-size: 15px; }
.brand-text .sub { color: var(--text-faint); font-size: 12px; }
.ttl { font-size: 22px; color: var(--text-strong); margin-bottom: 4px; }
.hint { font-size: 12px; margin: 0 0 18px; line-height: 1.6; }
.submit { width: 100%; margin-top: 6px; }
.foot {
  margin-top: 16px; padding-top: 14px;
  border-top: 1px solid var(--line);
  display: flex; align-items: center; justify-content: center; gap: 6px;
  font-size: 13px;
}
.page-foot {
  position: absolute;
  bottom: 18px;
  font-size: 12px;
}
</style>
