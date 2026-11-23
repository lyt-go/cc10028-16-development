<template>
  <section class="page" data-testid="layout-rerun">
    <header class="page-head">
      <div>
        <h2>版式复跑包</h2>
        <p class="page-desc">
          按页导出可离线复跑的版式区域包；导出前先看哪些页已确认，下载后可用包内 rerun.py 校验、复跑并核对单元格。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-documents" type="button" @click="loadDocuments">
          刷新文档
        </button>
      </div>
    </header>

    <div class="rerun-layout">
      <aside class="rerun-docs">
        <h3>版式文档</h3>
        <button
          v-for="doc in documents"
          :key="doc.doc_id"
          class="rerun-doc"
          :class="{ active: doc.doc_id === selectedDoc }"
          :data-testid="`doc-${doc.doc_id}`"
          type="button"
          @click="selectDocument(doc)"
        >
          <strong>{{ doc.doc_id }}</strong>
          <span>共 {{ doc.total_pages }} 页 · 已确认 {{ doc.ready_pages.length }} 页</span>
          <span class="rerun-doc-pages">
            <em
              v-for="page in doc.pages"
              :key="page.page"
              :class="page.page_status === '已确认' ? 'ok' : 'todo'"
            >P{{ page.page }} R{{ page.page_revision }}</em>
          </span>
        </button>
        <p v-if="!documents.length" class="empty-state">暂无版式修订文档，可先从版式还原产物建档。</p>
      </aside>

      <div class="rerun-main">
        <section v-if="selected" class="rerun-panel">
          <h3>导出页选择</h3>
          <p class="page-desc">勾选要放进包的页；未勾选的页不会出现在包内，也不会被当成整份文档。</p>
          <div class="rerun-page-list">
            <label v-for="page in selected.pages" :key="page.page" class="rerun-page">
              <input
                v-model="selectedPages"
                type="checkbox"
                :value="page.page"
                :data-testid="`pick-page-${page.page}`"
              />
              <span>第 {{ page.page }} 页</span>
              <span :class="page.page_status === '已确认' ? 'tag ok' : 'tag todo'">
                {{ page.page_status }}
              </span>
              <span class="muted">修订 {{ page.page_revision }} · 表格 {{ page.table_count }}</span>
            </label>
          </div>
          <div class="page-actions">
            <button class="btn" type="button" @click="selectAllPages">全选</button>
            <button class="btn ghost" type="button" @click="selectReadyPages">仅选已确认页</button>
            <button
              class="btn primary"
              data-testid="create-package"
              type="button"
              :disabled="!selectedPages.length || working"
              @click="exportPackage"
            >
              {{ working ? '导出中…' : '导出复跑包' }}
            </button>
          </div>
        </section>

        <section v-if="result" class="rerun-panel" data-testid="package-result">
          <h3>本次导出：{{ result.package_id }}</h3>
          <div class="stat-row">
            <article class="stat-card">
              <span class="stat-label">可立即复跑页</span>
              <strong class="stat-value">{{ result.readiness.ready_pages.join('、') || '无' }}</strong>
            </article>
            <article class="stat-card">
              <span class="stat-label">未确认页</span>
              <strong class="stat-value">{{ blockedLabel }}</strong>
            </article>
            <article class="stat-card">
              <span class="stat-label">包内表格数</span>
              <strong class="stat-value">{{ result.batch_table_count }}</strong>
            </article>
          </div>
          <p class="rerun-note" data-testid="readiness-note">{{ result.readiness.note }}</p>
          <ul class="rerun-checks">
            <li>整份可复跑：{{ result.readiness.can_rerun_whole_document ? '是' : '否' }}</li>
            <li>覆盖整份页：{{ result.readiness.covers_whole_document ? '是' : '否' }}</li>
            <li>可用于核对批次表格数：{{ result.readiness.can_check_batch_tables ? '是' : '否' }}</li>
          </ul>
          <div class="page-actions">
            <button class="btn primary" data-testid="download-package" type="button" @click="downloadPackage">
              下载 ZIP
            </button>
            <button class="btn" data-testid="extract-tables" type="button" @click="extractTables">
              抽取包内表格
            </button>
          </div>
          <p v-if="cells" class="rerun-note" data-testid="cells-result">
            已写出 {{ cells.cells_written }} 个单元格、{{ cells.tables_extracted }} 张表，覆盖第
            {{ cells.pages.join('、') }} 页 → {{ cells.output }}
          </p>
        </section>

        <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createRerunPackage,
  downloadRerunPackage,
  extractPackageTables,
  listRerunDocuments,
} from '@/api/backend-service'
import type { CellExtractResult, DocumentSummary, PackageSummary } from '@/data/types'

const documents = ref<DocumentSummary[]>([])
const selected = ref<DocumentSummary | null>(null)
const selectedPages = ref<number[]>([])
const result = ref<PackageSummary | null>(null)
const cells = ref<CellExtractResult | null>(null)
const errorMessage = ref('')
const working = ref(false)

const selectedDoc = computed(() => selected.value?.doc_id ?? '')
const blockedLabel = computed(() => {
  if (!result.value) return '—'
  const blocked = result.value.readiness.blocked_pages.map((item) => `P${item.page}`)
  return blocked.length ? blocked.join('、') : '无'
})

function selectDocument(doc: DocumentSummary) {
  selected.value = doc
  selectedPages.value = doc.pages.map((page) => page.page)
  result.value = null
  cells.value = null
  errorMessage.value = ''
}

function selectAllPages() {
  if (!selected.value) return
  selectedPages.value = selected.value.pages.map((page) => page.page)
}

function selectReadyPages() {
  if (!selected.value) return
  selectedPages.value = selected.value.pages
    .filter((page) => page.page_status === '已确认')
    .map((page) => page.page)
}

async function loadDocuments() {
  errorMessage.value = ''
  try {
    documents.value = await listRerunDocuments()
    const current = documents.value.find((doc) => doc.doc_id === selectedDoc.value)
    if (current) selectDocument(current)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版式文档列表读取失败'
  }
}

async function exportPackage() {
  if (!selected.value) return
  working.value = true
  errorMessage.value = ''
  cells.value = null
  try {
    result.value = await createRerunPackage(
      selected.value.doc_id,
      selectedPages.value.length === selected.value.pages.length ? null : [...selectedPages.value].sort((a, b) => a - b),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复跑包导出失败'
  } finally {
    working.value = false
  }
}

async function downloadPackage() {
  if (!result.value) return
  errorMessage.value = ''
  try {
    await downloadRerunPackage(result.value.package_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复跑包下载失败'
  }
}

async function extractTables() {
  if (!result.value) return
  errorMessage.value = ''
  try {
    cells.value = await extractPackageTables(result.value.package_id, result.value.selected_pages)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复跑包表格抽取失败'
  }
}

onMounted(loadDocuments)
</script>

<style scoped>
.rerun-layout { display: grid; grid-template-columns: 260px 1fr; gap: 16px; }
.rerun-docs { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.rerun-docs h3 { margin: 0 0 10px; font-size: 14px; }
.rerun-doc { display: flex; flex-direction: column; gap: 4px; width: 100%; text-align: left; background: none; border: 1px solid transparent; border-radius: 6px; padding: 8px; cursor: pointer; color: inherit; }
.rerun-doc.active { border-color: var(--brand); background: #f3f7ff; }
.rerun-doc span { font-size: 12px; color: var(--muted); }
.rerun-doc-pages { display: flex; gap: 4px; flex-wrap: wrap; }
.rerun-doc-pages em { font-style: normal; font-size: 11px; padding: 1px 5px; border-radius: 4px; }
.rerun-doc-pages .ok { background: #e7f6ec; color: #157347; }
.rerun-doc-pages .todo { background: #fdf1e7; color: #b45309; }
.rerun-main { display: flex; flex-direction: column; gap: 16px; }
.rerun-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.rerun-panel h3 { margin: 0 0 6px; font-size: 14px; }
.rerun-page-list { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
.rerun-page { display: flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; }
.rerun-page .tag { font-size: 11px; padding: 1px 6px; border-radius: 4px; }
.rerun-page .tag.ok { background: #e7f6ec; color: #157347; }
.rerun-page .tag.todo { background: #fdf1e7; color: #b45309; }
.rerun-page .muted { color: var(--muted); font-size: 12px; }
.rerun-note { color: var(--muted); font-size: 13px; }
.rerun-checks { margin: 8px 0; padding-left: 18px; font-size: 13px; color: #344054; }
.page-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
</style>
