<script setup lang="ts">
/**
 * 第三方应用注册页（用户 2026-10-14 需求：第三方应用注册页面）。
 *
 * 与「认证管理」(admin 侧管理/禁用) 的分工：本页是**自助注册入口**——
 * 第三方系统/接入方在此登记应用，立即拿到 client_id + api_key（仅展示一次），
 * 之后在请求头携带 X-API-Key 调用业务接口。
 *
 * 后端端点 POST /v1/auth/clients/register 本身开放（无需登录即可注册，符合
 * "注册调用"流程）；前端包在登录壳内只为统一体验，端点鉴权仍以服务端为准。
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { registerApiClient, SECURITY_LEVEL_OPTIONS, type ApiClientItem } from '@/api/client'

const form = ref({
  name: '',
  scopes: ['chat', 'retrieve'] as string[],
  max_clearance: 'INTERNAL' as ApiClientItem['max_clearance'],
})
const submitting = ref(false)
const newKey = ref<{ client_id: string; api_key: string } | null>(null)

const SCOPE_OPTIONS = [
  'chat', 'retrieve', 'documents', 'spaces',
  'conversations', 'templates', 'config', 'audit', 'ops',
]

async function submit() {
  if (!form.value.name.trim()) {
    ElMessage.warning('请填写应用名称')
    return
  }
  if (!form.value.scopes.length) {
    ElMessage.warning('请至少选择一个能力域')
    return
  }
  submitting.value = true
  try {
    const created = await registerApiClient({
      name: form.value.name.trim(),
      scopes: form.value.scopes,
      max_clearance: form.value.max_clearance,
    })
    newKey.value = { client_id: created.client_id, api_key: created.api_key }
    ElMessage.success('注册成功，请立即保存 API Key（仅展示这一次）')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '注册失败')
  } finally {
    submitting.value = false
  }
}

async function copyKey() {
  if (!newKey.value) return
  try {
    await navigator.clipboard.writeText(newKey.value.api_key)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

function reset() {
  newKey.value = null
  form.value = { name: '', scopes: ['chat', 'retrieve'], max_clearance: 'INTERNAL' }
}
</script>

<template>
  <div class="panel pad page-fill client-reg">
    <div class="head">
      <div class="ttl serif">第三方应用注册</div>
      <span class="badge INTERNAL">应用接入 · API Key 认证</span>
    </div>

    <p class="desc text-muted">
      第三方系统在此自助登记应用，注册后获得 <b>client_id</b> 与 <b>api_key</b>（仅返回一次，请妥善保存）。
      之后在请求头携带 <b>X-API-Key: sk-xxxx</b> 调用业务接口。能力域（scopes）与最高可检索密级（max_clearance）
      在注册时限定，服务端强制执行。
    </p>
    <p class="desc text-muted">
      <b>空间所有权：</b>通过本应用 Key 创建的文档空间<b>仅本应用可见、可上传、可检索</b>（其他应用与运维用户均不可见）；
      <b>默认空间</b>为公共空间，所有应用可见、可上传。上传文档时指定 <code>space_id</code> 即可归入本人空间。
    </p>

    <!-- 注册表单 -->
    <div v-if="!newKey" class="reg-card panel">
      <el-form label-position="top" class="reg-form">
        <el-form-item label="应用名称" required>
          <el-input v-model="form.name" placeholder="建议填写接入系统名，便于审计核对" />
        </el-form-item>
        <el-form-item label="能力域（scopes）">
          <el-select v-model="form.scopes" multiple style="width: 100%">
            <el-option v-for="s in SCOPE_OPTIONS" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="最高可检索密级（max_clearance）">
          <el-select v-model="form.max_clearance" style="width: 100%">
            <el-option
              v-for="lv in SECURITY_LEVEL_OPTIONS"
              :key="lv.value"
              :label="`${lv.value}（${lv.label}）`"
              :value="lv.value"
            />
          </el-select>
        </el-form-item>
        <el-button type="primary" round :loading="submitting" @click="submit">注册并生成 Key</el-button>
      </el-form>
    </div>

    <!-- 注册成功：一次性展示 api_key -->
    <div v-else class="key-panel">
      <div class="key-ttl"><span class="ok-dot" />注册成功 · API Key 仅此一次展示</div>
      <div class="key-row">
        <code class="mono">{{ newKey.api_key }}</code>
        <el-button size="small" round @click="copyKey">复制</el-button>
      </div>
      <div class="key-sub text-muted">
        Client ID：<span class="mono">{{ newKey.client_id }}</span>；服务端仅存哈希，遗失需重新注册。
      </div>
      <div class="key-ops">
        <el-button type="primary" round @click="reset">再注册一个</el-button>
      </div>
      <p class="warn text-muted">
        ⚠ 请立即将 API Key 保存到安全位置。关闭本页面后将无法再次查看，只能重新注册。
      </p>
    </div>
  </div>
</template>

<style scoped>
.client-reg { max-width: 720px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.ttl { font-size: 18px; color: var(--text-strong); }
.desc { font-size: 13px; line-height: 1.7; margin: 0 0 16px; }
.desc b { color: var(--text); }
.reg-card { padding: 18px 22px; max-width: 520px; }
.reg-form { margin-top: 4px; }
.key-panel {
  padding: 16px 20px; border: 1px dashed var(--brand-line);
  border-radius: var(--radius-sm); background: var(--brand-soft); max-width: 560px;
}
.key-ttl { color: var(--brand); font-weight: 600; font-size: 14px; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
.ok-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c-success); }
.key-row { display: flex; align-items: center; gap: 10px; }
.key-row code { flex: 1; font-size: 12px; color: var(--text-strong); word-break: break-all; }
.key-sub { font-size: 12px; margin-top: 10px; }
.key-ops { margin-top: 14px; }
.warn { font-size: 12px; margin-top: 12px; line-height: 1.6; }
</style>
