<script setup lang="ts">
/**
 * 注册页（用户 2026-10-14 需求：登录注册功能）。
 *
 * 独立全屏页契约同 Login.vue：路由 meta.public=true，App.vue 裸渲染。
 * 运维台自然人账号自助注册：用户名 + 口令 + 显示名；
 * 后端注册成功直接下发 JWT（注册即登录），本页落登录态后回跳原页面。
 *
 * 权限说明（与后端约定）：自助注册角色固定为 viewer（最低权限），
 * 提权/禁用由管理员在 DB 侧维护；账号重名返回 409，原样展示后端 detail。
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { register } from '@/api/client'

const router = useRouter()
const route = useRoute()
const username = ref('')
const displayName = ref('')
const password = ref('')
const password2 = ref('')
const loading = ref(false)

async function onSubmit() {
  // 前端预校验：与后端 schema 约束对齐（用户名 ≥2、口令 ≥6），
  // 提前拦截减少无效请求；确认口令一致性仅前端管（后端无此概念）。
  const u = username.value.trim()
  if (u.length < 2) {
    ElMessage.warning('用户名至少 2 个字符')
    return
  }
  if (password.value.length < 6) {
    ElMessage.warning('口令至少 6 位')
    return
  }
  if (password.value !== password2.value) {
    ElMessage.warning('两次输入的口令不一致')
    return
  }
  loading.value = true
  try {
    await register(u, password.value, displayName.value.trim())
    ElMessage.success('注册成功，已自动登录')
    const redirect = (route.query.redirect as string) || '/overview'
    router.replace(redirect)
  } catch (e: any) {
    // 409=用户名重名；其余按后端 detail 原样提示（如 422 参数校验）
    ElMessage.error(e?.response?.data?.detail || '注册失败，请稍后重试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="register-page">
    <div class="bg-deco" aria-hidden="true" />

    <div class="register-panel panel">
      <div class="brand-bar">
        <div class="seal">公文</div>
        <div class="brand-text">
          <div class="name">公文知识库</div>
          <div class="sub">RAG 引擎 · 运维台</div>
        </div>
      </div>
      <div class="ttl serif">注册</div>
      <p class="hint text-muted">注册运维台账号，成功后自动登录；新账号为只读权限（viewer）。</p>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名" required>
          <el-input v-model="username" placeholder="请输入用户名（登录用，唯一）" autofocus @keyup.enter="onSubmit" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="displayName" placeholder="选填，不填则与用户名相同" @keyup.enter="onSubmit" />
        </el-form-item>
        <el-form-item label="口令" required>
          <el-input v-model="password" type="password" show-password placeholder="至少 6 位" @keyup.enter="onSubmit" />
        </el-form-item>
        <el-form-item label="确认口令" required>
          <el-input v-model="password2" type="password" show-password placeholder="再输入一次口令" @keyup.enter="onSubmit" />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" round @click="onSubmit">注 册</el-button>
      </el-form>
      <div class="foot">
        <span class="text-muted">已有账号？</span>
        <el-link type="primary" :underline="false" @click="router.push('/login')">返回登录</el-link>
      </div>
    </div>

    <div class="page-foot text-muted">v1.2.0 · 内网部署 · 仅企业秘密级（非国家秘密）</div>
  </div>
</template>

<style scoped>
/* 与 Login.vue 同一套独立全屏页样式（壳外渲染，自行撑满视口并居中） */
.register-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}
.bg-deco {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(600px 320px at 50% 18%, rgba(61, 126, 255, 0.14), transparent 70%),
    repeating-linear-gradient(0deg, transparent 0 56px, rgba(255, 255, 255, 0.025) 56px 57px),
    repeating-linear-gradient(90deg, transparent 0 56px, rgba(255, 255, 255, 0.025) 56px 57px);
}
.register-panel {
  width: 400px;
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
