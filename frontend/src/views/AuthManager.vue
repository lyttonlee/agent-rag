<script setup lang="ts">
/**
 * 认证管理页（用户 2026-10-13 需求：第三方注册调用的认证方式）。
 *
 * 面向 admin：查看/注册/禁用/启用第三方客户端。注册返回的 api_key
 * 仅出现一次（后端只存哈希），页面以醒目面板提示保存。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listApiClients,
  registerApiClient,
  setClientEnabled,
  type ApiClientItem,
} from '@/api/client'

const rows = ref<ApiClientItem[]>([])
const loading = ref(false)

// ---- 注册对话框 ----
const dialogVisible = ref(false)
const submitting = ref(false)
const form = ref({ name: '', scopes: ['chat', 'retrieve'], max_clearance: 'INTERNAL' as ApiClientItem['max_clearance'] })

// ---- 一次性 api_key 展示 ----
const newKey = ref<{ client_id: string; api_key: string } | null>(null)

const SCOPE_OPTIONS = ['chat', 'retrieve', 'documents', 'spaces', 'conversations', 'templates', 'config', 'audit', 'ops']

async function load() {
  loading.value = true
  try {
    rows.value = await listApiClients()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载客户端列表失败')
  } finally {
    loading.value = false
  }
}

function openRegister() {
  form.value = { name: '', scopes: ['chat', 'retrieve'], max_clearance: 'INTERNAL' }
  newKey.value = null
  dialogVisible.value = true
}

async function submitRegister() {
  if (!form.value.name.trim()) {
    ElMessage.warning('请填写客户端名称')
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
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '注册失败')
  } finally {
    submitting.value = false
  }
}

async function toggleEnabled(row: ApiClientItem) {
  const action = row.enabled ? '禁用' : '启用'
  try {
    await ElMessageBox.confirm(
      `确定${action}客户端「${row.name}」？${row.enabled ? '禁用后其 API Key 立即失效。' : ''}`,
      `${action}确认`,
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await setClientEnabled(row.client_id, !row.enabled)
    ElMessage.success(`已${action}`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || `${action}失败`)
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

onMounted(load)
</script>

<template>
  <div class="panel pad page-fill">
    <div class="head">
      <div class="ttl serif">认证管理</div>
      <span class="text-muted">第三方注册调用 · API Key 认证</span>
      <div class="spacer" />
      <el-button type="primary" round @click="openRegister">注册客户端</el-button>
    </div>

    <p class="desc text-muted">
      第三方系统通过 <b>POST /v1/auth/clients/register</b> 自助注册，获得 client_id 与 api_key（仅返回一次）；
      之后在请求头携带 <b>X-API-Key: sk-xxxx</b> 调用业务接口。能力域（scopes）与最高可检索密级
      （max_clearance）在注册时限定，服务端强制执行。
    </p>

    <el-table :data="rows" v-loading="loading" stripe class="table">
      <el-table-column prop="name" label="名称" min-width="180" />
      <el-table-column prop="client_id" label="Client ID" min-width="200">
        <template #default="{ row }"><span class="mono">{{ row.client_id }}</span></template>
      </el-table-column>
      <el-table-column label="API Key" min-width="120">
        <template #default="{ row }"><span class="mono text-muted">{{ row.key_prefix }}…</span></template>
      </el-table-column>
      <el-table-column label="能力域" min-width="200">
        <template #default="{ row }">
          <el-tag v-for="s in row.scopes" :key="s" size="small" class="scope-tag">{{ s }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="max_clearance" label="最高密级" width="110">
        <template #default="{ row }"><span class="badge" :class="row.max_clearance">{{ row.max_clearance }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <span class="st" :class="{ ok: row.enabled }">{{ row.enabled ? '启用' : '禁用' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="最近使用" width="170">
        <template #default="{ row }">
          <span class="text-muted">{{ row.last_used_at ? new Date(row.last_used_at).toLocaleString() : '从未' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <el-button size="small" round :type="row.enabled ? 'danger' : 'success'" @click="toggleEnabled(row)">
            {{ row.enabled ? '禁用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 注册对话框 -->
    <el-dialog v-model="dialogVisible" title="注册第三方客户端" width="520px">
      <el-form label-position="top">
        <el-form-item label="客户端名称" required>
          <el-input v-model="form.name" placeholder="建议填写接入系统名，便于审计核对" />
        </el-form-item>
        <el-form-item label="能力域（scopes）">
          <el-select v-model="form.scopes" multiple style="width: 100%">
            <el-option v-for="s in SCOPE_OPTIONS" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="最高可检索密级（max_clearance）">
          <el-select v-model="form.max_clearance" style="width: 100%">
            <el-option label="PUBLIC（公开）" value="PUBLIC" />
            <el-option label="INTERNAL（内部）" value="INTERNAL" />
            <el-option label="RESTRICTED（受限）" value="RESTRICTED" />
            <el-option label="SECRET（秘密）" value="SECRET" />
          </el-select>
        </el-form-item>
      </el-form>

      <!-- 注册成功：一次性展示 api_key -->
      <div v-if="newKey" class="key-panel">
        <div class="key-ttl">API Key（仅此一次展示，请立即保存）</div>
        <div class="key-row">
          <code class="mono">{{ newKey.api_key }}</code>
          <el-button size="small" round @click="copyKey">复制</el-button>
        </div>
        <div class="key-sub text-muted">Client ID：{{ newKey.client_id }}；服务端仅存哈希，遗失需重新注册。</div>
      </div>

      <template #footer>
        <el-button round @click="dialogVisible = false">关闭</el-button>
        <el-button v-if="!newKey" type="primary" round :loading="submitting" @click="submitRegister">注册并生成 Key</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
.ttl { font-size: 18px; color: var(--text-strong); }
.desc { font-size: 13px; line-height: 1.7; margin: 0 0 14px; }
.desc b { color: var(--text); }
.table { margin-top: 4px; }
.scope-tag { margin: 2px 4px 2px 0; }
.st { font-size: 12px; }
.st.ok { color: var(--c-success); }
.key-panel {
  margin-top: 14px; padding: 12px 14px; border: 1px dashed var(--brand-line);
  border-radius: var(--radius-sm); background: var(--brand-soft);
}
.key-ttl { color: var(--brand); font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.key-row { display: flex; align-items: center; gap: 10px; }
.key-row code { flex: 1; font-size: 12px; color: var(--text-strong); word-break: break-all; }
.key-sub { font-size: 12px; margin-top: 8px; }
</style>
