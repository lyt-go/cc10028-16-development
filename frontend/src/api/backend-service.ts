import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  CellExtractResult,
  DocumentSummary,
  EntryRow,
  OverviewResult,
  PackageSummary,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function listRerunDocuments(): Promise<DocumentSummary[]> {
  const response = await request('/api/layout-rerun/documents')
  if (!response.ok) throw new Error('版式文档列表读取失败')
  return (await response.json()).documents as DocumentSummary[]
}

export async function createRerunPackage(
  docId: string,
  pages: number[] | null,
): Promise<PackageSummary> {
  const response = await request('/api/layout-rerun/packages', {
    method: 'POST',
    body: JSON.stringify({ doc_id: docId, pages }),
  })
  if (!response.ok) throw new Error('版式复跑包导出失败')
  return (await response.json()) as PackageSummary
}

export async function downloadRerunPackage(packageId: string): Promise<void> {
  const response = await request(`/api/layout-rerun/packages/${packageId}/download`)
  if (!response.ok) throw new Error('版式复跑包下载失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${packageId}.zip`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function extractPackageTables(
  packageId: string,
  pages: number[],
): Promise<CellExtractResult> {
  const query = pages.length ? `?pages=${pages.join(',')}` : ''
  const response = await request(`/api/layout-rerun/packages/${packageId}/table-extract${query}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('复跑包表格抽取失败')
  return (await response.json()) as CellExtractResult
}
