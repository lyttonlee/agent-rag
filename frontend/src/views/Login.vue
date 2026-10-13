<script setup lang="ts">
/**
 * 登录页（用户 2026-10-13 需求：添加用户认证）。
 *
 * 用户名 + 口令登录，成功后将 JWT 存入 localStorage 并回跳原页面。
 * 设计语言与全站一致：深墨蓝底、智蓝主色、公文红印章标识。
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
  <div class="login-wrap">
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
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-panel {
  width: 380px;
  padding: 32px 36px 28px;
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
</style>
