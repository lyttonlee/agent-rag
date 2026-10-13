<script setup lang="ts">
/**
 * Markdown 渲染组件：把模型生成的 Markdown 文本渲染为富文本。
 *
 * 背景（用户 2026-10-13 反馈）：问答/起草生成的内容大多是 Markdown 格式
 * （标题、列表、表格、加粗等），此前按纯文本直接插值显示，"格式标记"
 * 原样暴露、排版缺失。本组件统一解决该问题。
 *
 * 技术选型（成熟库，不造轮子）：
 * - markdown-it（15.x）：生态最主流的 Markdown 解析器，插件体系完善，
 *   默认关闭原始 HTML（html:false），从解析层杜绝脚本注入；
 * - DOMPurify（3.x）：OWASP 推荐的 HTML 消毒库，对解析结果二次净化，
 *   双保险防 XSS（模型输出不可信）。
 *
 * 交互细节：
 * - linkify：自动识别裸 URL；链接统一新窗口打开并加 rel=noopener；
 * - breaks：保留单换行（公文/问答场景换行语义重要）；
 * - 流式场景下每 token 触发重解析，markdown-it 对 KB 级文本为亚毫秒级，
 *   无需额外节流；未闭合标记（如流式半途的 ```）由解析器容错处理。
 */
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const props = defineProps<{
  /** Markdown 源文本 */
  text: string
}>()

// 模块级单例：解析器无状态，全应用复用同一实例
const md = new MarkdownIt({
  html: false, // 禁用原始 HTML 输入（防 XSS 第一层）
  linkify: true, // 自动识别 URL
  breaks: true, // 单换行渲染为 <br>（问答/公文场景需要）
  typographer: false, // 不做智能引号等替换，保持公文原文
})

// 链接统一新窗口打开（安全：补 noopener 防 tabnabbing）
const defaultLinkOpen =
  md.renderer.rules.link_open ||
  ((tokens: any, idx: number, options: any, _self: any, renderer: any) =>
    renderer.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, self, renderer) => {
  const token = tokens[idx]
  token.attrSet('target', '_blank')
  token.attrSet('rel', 'noopener')
  return defaultLinkOpen(tokens, idx, options, self, renderer)
}

/** 解析 + 消毒后的安全 HTML（computed 缓存，文本不变不重复解析） */
const html = computed(() => {
  const raw = props.text || ''
  if (!raw) return ''
  const parsed = md.render(raw)
  // DOMPurify 二次净化（防 XSS 第二层）；浏览器环境下全局可用
  return DOMPurify.sanitize(parsed, { USE_PROFILES: { html: true } })
})
</script>

<template>
  <!-- v-html 内容已过 markdown-it(html:false) + DOMPurify 双重净化 -->
  <div class="md-body" v-html="html" />
</template>

<style scoped>
/* 结构样式优先继承父级文字色（currentColor 体系），
 * 使组件同时适配深色主题（问答页）与浅色"纸面"（起草页）。 */
.md-body { line-height: 1.75; word-break: break-word; }
.md-body > :first-child { margin-top: 0; }
.md-body > :last-child { margin-bottom: 0; }

.md-body :deep(p) { margin: 0 0 10px; }
.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4) {
  margin: 16px 0 8px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--text-strong);
}
.md-body :deep(h1) { font-size: 19px; }
.md-body :deep(h2) { font-size: 17px; }
.md-body :deep(h3) { font-size: 15px; }
.md-body :deep(h4) { font-size: 14px; }

.md-body :deep(ul),
.md-body :deep(ol) { margin: 0 0 10px; padding-left: 1.5em; }
.md-body :deep(li) { margin: 2px 0; }
.md-body :deep(li)::marker { color: var(--text-muted); }

.md-body :deep(blockquote) {
  margin: 0 0 10px;
  padding: 2px 12px;
  border-left: 3px solid var(--brand-line);
  color: var(--text-muted);
  background: var(--brand-soft);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.md-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.92em;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(127, 127, 127, 0.18);
}
.md-body :deep(pre) {
  margin: 0 0 10px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: rgba(127, 127, 127, 0.14);
  overflow: auto;
}
.md-body :deep(pre code) { background: transparent; padding: 0; }

.md-body :deep(table) {
  border-collapse: collapse;
  margin: 0 0 10px;
  font-size: 0.95em;
}
.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid var(--line-strong);
  padding: 5px 10px;
  text-align: left;
}
.md-body :deep(th) { font-weight: 600; color: var(--text-strong); }
.md-body :deep(tr:nth-child(even) td) { background: rgba(127, 127, 127, 0.08); }

.md-body :deep(hr) { border: none; border-top: 1px solid var(--line-strong); margin: 14px 0; }
.md-body :deep(a) { color: var(--brand); text-decoration: none; }
.md-body :deep(a):hover { text-decoration: underline; }
.md-body :deep(strong) { color: var(--text-strong); font-weight: 600; }
</style>
