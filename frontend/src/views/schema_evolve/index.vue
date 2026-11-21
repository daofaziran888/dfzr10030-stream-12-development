<template>
  <section class="page" data-module="schema_evolve">
    <header class="page-head">
      <div>
        <h2>Schema演进管理</h2>
        <p class="page-desc">维护流式数据Schema的兼容性变更管理、版本追踪和回滚控制。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记结构变更</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出Schema演进清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无Schema演进数据，可先登记结构变更</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记结构变更</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="compat-panel" data-testid="compat-panel">
      <header class="compat-head">
        <div>
          <h3>结构变更期兼容转换</h3>
          <p class="page-desc">读取结构变更记录生成转换兼容数据批次；旧版读旧列、新版读新列，冲突行进入待核对区。</p>
        </div>
        <button class="btn" type="button" data-testid="compat-refresh" @click="loadCompat">刷新批次</button>
      </header>

      <form class="filter-bar" @submit.prevent="runCompat">
        <label class="filter-item">
          <span>结构变更</span>
          <select v-model="compatForm.changeId" data-testid="compat-change">
            <option value="" disabled>选择可转换的改名变更</option>
            <option v-for="item in candidates" :key="String(item.id)" :value="String(item.id)">
              {{ item.变更编号 }}（{{ item.旧字段 }} → {{ item.新字段 }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>批次号（续做时填写）</span>
          <input v-model="compatForm.batchNo" data-testid="compat-batch-no" placeholder="留空则新建批次" />
        </label>
        <label class="filter-item">
          <span>生效边界位点</span>
          <input v-model="compatForm.boundary" data-testid="compat-boundary" type="number" placeholder="如 500" />
        </label>
        <label class="filter-item inline-check">
          <span>旧流水线仍在补算</span>
          <input v-model="compatForm.backfill" data-testid="compat-backfill" type="checkbox" />
        </label>
        <button class="btn primary" type="submit" data-testid="compat-run" :disabled="compatRunning">
          {{ compatRunning ? '转换中…' : '生成兼容批次' }}
        </button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>批次号</th><th>旧字段 → 新字段</th><th>状态</th><th>转换行数</th>
            <th>纯旧列 / 纯新列 / 一致</th><th>待核对</th><th>边界冲突</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.id" :data-testid="`compat-batch-${batch.id}`">
            <td>{{ batch.批次号 }}</td>
            <td>{{ batch.旧字段 }} → {{ batch.新字段 }}（{{ batch.旧版本 }} / {{ batch.新版本 }}）</td>
            <td>{{ batch.状态 }}</td>
            <td>{{ batch.转换行数 }}</td>
            <td>{{ batch.纯旧列 }} / {{ batch.纯新列 }} / {{ batch.两列一致 }}</td>
            <td>{{ batch.待核对行数 }}</td>
            <td>{{ batch.生效边界冲突行数 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`compat-detail-${batch.id}`" @click="openBatch(batch)">查看明细</button>
              <button class="link" type="button" :data-testid="`compat-rollback-${batch.id}`" @click="openRollback(batch)">回滚追溯</button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="8" class="empty-state">尚未生成兼容批次，先选择一条字段改名变更执行转换</td>
          </tr>
        </tbody>
      </table>

      <div v-if="activeBatch" class="compat-detail" :data-testid="`compat-detail-panel-${activeBatch.id}`">
        <div class="compat-block">
          <h4>字段版本依赖</h4>
          <ul>
            <li v-for="dep in activeBatch.字段版本依赖" :key="dep.下游版本" :data-testid="`compat-dep-${dep.下游版本}`">
              {{ dep.下游版本 }}：读 {{ dep.读取字段 }}，可读 {{ dep.可读行数 }} 行，跳过争议 {{ dep.跳过争议行数 }} 行
            </li>
          </ul>
        </div>
        <div class="compat-block">
          <h4>数据库实际写入范围</h4>
          <p>{{ activeBatch.数据库写入范围.关联流 }} · 行键 {{ activeBatch.数据库写入范围.行键 }}</p>
          <p>写入字段：{{ (activeBatch.数据库写入范围.写入字段 || []).join('、') }} · 写入 {{ activeBatch.数据库写入范围.写入行数 }} 行 · 位点 {{ (activeBatch.数据库写入范围.位点范围 || []).join(' ~ ') }}</p>
        </div>
        <div class="compat-block">
          <h4>生效边界</h4>
          <p>{{ activeBatch.生效边界.边界状态 }}<span v-if="activeBatch.生效边界.边界值 != null">（边界值 {{ activeBatch.生效边界.边界值 }}）</span></p>
          <ul v-if="activeBatch.生效边界.冲突明细?.length">
            <li v-for="row in activeBatch.生效边界.冲突明细" :key="row.原行标识">{{ row.原行标识 }}：{{ row.说明 }}</li>
          </ul>
        </div>
        <div class="compat-block">
          <h4>逐行兼容视图（已写 {{ activeBatch.records.length }} 行）</h4>
          <table class="data-table compact">
            <thead><tr><th>原行标识</th><th>输入形态</th><th>旧版 {{ activeBatch.旧字段 }}</th><th>新版 {{ activeBatch.新字段 }}</th><th>写入次数</th></tr></thead>
            <tbody>
              <tr v-for="record in activeBatch.records" :key="record.原行标识">
                <td>{{ record.原行标识 }}</td>
                <td>{{ record.输入形态 }}</td>
                <td>{{ record.旧版视图.金额 }}（{{ record.旧版视图.字段版本 }}）</td>
                <td>{{ record.新版视图.金额 }}（{{ record.新版视图.字段版本 }}）</td>
                <td>{{ record.写入次数 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="compat-block">
          <h4>待核对区（按原行标识）</h4>
          <table class="data-table compact">
            <thead><tr><th>原行标识</th><th>旧值</th><th>新值</th><th>差额</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in reviews" :key="String(row.id)" :data-testid="`compat-review-${row.原行标识}`">
                <td>{{ row.原行标识 }}</td>
                <td>{{ row.旧值 }}</td>
                <td>{{ row.新值 }}</td>
                <td>{{ row.差额 }}</td>
                <td>{{ row.状态 }}</td>
              </tr>
              <tr v-if="!reviews.length"><td colspan="5" class="empty-state">没有争议行</td></tr>
            </tbody>
          </table>
        </div>
        <div class="compat-block">
          <h4>结构核对清单</h4>
          <ul>
            <li v-for="item in checks" :key="item.核对项" :data-testid="`compat-check-${item.核对项}`">
              {{ item.核对项 }}：{{ item.结果 }} · {{ item.说明 }}
            </li>
          </ul>
        </div>
      </div>

      <div v-if="rollbackTrace" class="compat-detail" data-testid="compat-rollback-panel">
        <div class="compat-block">
          <h4>回滚追溯（{{ rollbackTrace.批次号 }}）</h4>
          <p>{{ rollbackTrace.说明 }}</p>
          <p>回滚后读取字段：{{ rollbackTrace.回滚映射.回滚后读取字段 }}（{{ rollbackTrace.回滚映射.旧版版本 }}）</p>
          <table class="data-table compact">
            <thead><tr><th>原行标识</th><th>原列</th><th>原值</th><th>回滚写入</th></tr></thead>
            <tbody>
              <tr v-for="row in rollbackTrace.行追溯" :key="row.原行标识">
                <td>{{ row.原行标识 }}</td>
                <td>{{ row.原列 }}</td>
                <td>{{ row.原值 }}</td>
                <td>{{ row.回滚写入[rollbackTrace.回滚映射.回滚后读取字段] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条Schema演进记录</span>
      <span v-if="compatMessage" class="compat-message" data-testid="compat-message">{{ compatMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request, fetchJson } from '@/api/client'

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = (await response.json()) as T
  if (!response.ok) throw new Error('兼容转换接口返回失败')
  return payload
}

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/schema_evolve'
const columns = ["变更编号", "关联流", "变更类型", "新增字段", "删除字段", "兼容级别", "生效版本", "回滚标记"]
const actions = ["执行变更", "标记生效", "回滚变更"]
const statuses = ["草稿", "待执行", "已生效", "已回滚"]
const metricNames = ["变更总数", "已生效变更", "待执行变更"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'schema_evolve.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('Schema演进动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Schema演进操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('结构变更列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Schema演进列表读取失败'
  }
}

type Candidate = {
  id: number
  变更编号: string
  旧字段: string
  新字段: string
  生效版本: string | null
  status: string
  已回滚: boolean
}
type CompatBatch = {
  id: number
  批次号: string
  旧字段: string
  新字段: string
  旧版本: string
  新版本: string
  状态: string
  转换行数: number
  纯旧列: number
  纯新列: number
  两列一致: number
  待核对行数: number
  生效边界冲突行数: number
}
type CompatReview = { id: number; 原行标识: string; 旧值: number | null; 新值: number | null; 差额: number | null; 状态: string }
type CompatCheckItem = { 核对项: string; 结果: string; 说明: string }
type CompatCheck = { 校验项: CompatCheckItem[] }
type BatchDetail = CompatBatch & {
  生效边界: { 边界值: number | null; 边界状态: string; 冲突明细: { 原行标识: string; 说明: string }[] }
  字段版本依赖: { 下游版本: string; 读取字段: string; 可读行数: number; 跳过争议行数: number }[]
  数据库写入范围: { 关联流: string; 行键: string; 写入字段: string[]; 写入行数: number; 位点范围: (number | null)[] }
  records: { 原行标识: string; 输入形态: string; 旧版视图: { 金额: number; 字段版本: string }; 新版视图: { 金额: number; 字段版本: string }; 写入次数: number }[]
}
type RollbackTrace = {
  批次号: string
  说明: string
  回滚映射: { 回滚后读取字段: string; 旧版版本: string }
  行追溯: { 原行标识: string; 原列: string; 原值: number | null; 回滚写入: Record<string, number | null> }[]
}

const candidates = ref<Candidate[]>([])
const batches = ref<CompatBatch[]>([])
const activeBatch = ref<BatchDetail | null>(null)
const reviews = ref<CompatReview[]>([])
const checks = ref<CompatCheckItem[]>([])
const rollbackTrace = ref<RollbackTrace | null>(null)
const compatMessage = ref('')
const compatRunning = ref(false)
const compatForm = ref<{ changeId: string; batchNo: string; boundary: string | number; backfill: boolean }>(
  { changeId: '', batchNo: '', boundary: '', backfill: false },
)

async function loadCompat() {
  try {
    const [candidatePayload, batchPayload] = await Promise.all([
      fetchJson<{ items: Candidate[] }>(`${ENDPOINT}/compatibility/candidates`),
      fetchJson<{ items: CompatBatch[] }>(`${ENDPOINT}/compatibility/batches`),
    ])
    candidates.value = candidatePayload.items ?? []
    batches.value = batchPayload.items ?? []
    if (!compatForm.value.changeId && candidates.value.length) {
      compatForm.value.changeId = String(candidates.value[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '兼容批次读取失败'
  }
}

async function runCompat() {
  if (!compatForm.value.changeId) {
    errorMessage.value = '请先选择一条字段改名变更'
    return
  }
  compatRunning.value = true
  compatMessage.value = ''
  errorMessage.value = ''
  try {
    const values: Record<string, unknown> = { change_id: compatForm.value.changeId }
    if (String(compatForm.value.batchNo).trim()) values.batch_no = String(compatForm.value.batchNo).trim()
    const boundary = String(compatForm.value.boundary).trim()
    if (boundary !== '') values.effective_boundary = Number(boundary)
    if (compatForm.value.backfill) values.old_pipeline_backfill = true
    const payload = await postJson<{ ok: boolean; message: string; entry: BatchDetail | null }>(
      `${ENDPOINT}/compatibility/run`, { values })
    compatMessage.value = payload.message
    if (!payload.ok) throw new Error(payload.message)
    await loadCompat()
    await reload()
    if (payload.entry) await openBatch(payload.entry)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '兼容转换失败'
  } finally {
    compatRunning.value = false
  }
}

async function openBatch(batch: CompatBatch) {
  try {
    const [detail, reviewPayload, checkPayload] = await Promise.all([
      fetchJson<BatchDetail>(`/api/compatibility/batches/${batch.id}`),
      fetchJson<{ items: CompatReview[] }>(`${ENDPOINT}/reviews?batch_no=${encodeURIComponent(batch.批次号)}`),
      fetchJson<{ items: CompatCheck[] }>(`${ENDPOINT}/checks?batch_no=${encodeURIComponent(batch.批次号)}`),
    ])
    activeBatch.value = detail
    reviews.value = reviewPayload.items ?? []
    checks.value = checkPayload.items[0]?.校验项 ?? []
    rollbackTrace.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '兼容批次明细读取失败'
  }
}

async function openRollback(batch: CompatBatch) {
  try {
    const payload = await fetchJson<{ trace: RollbackTrace }>(`/api/compatibility/batches/${batch.id}/rollback-trace`)
    rollbackTrace.value = payload.trace
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚追溯读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadCompat()])
})
</script>
