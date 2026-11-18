<template>
  <section class="page" data-module="data_publish">
    <header class="page-head">
      <div>
        <h2>数据发布管理</h2>
        <p class="page-desc">维护发布版本的输入、执行约束与结果记录，为数据发布提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记发布版本</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据发布清单</button>
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
          <th>发布快照版本</th>
          <th>快照行数</th>
          <th>转换批次</th>
          <th>待发布批次</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['发布快照版本'] || '—' }}</td>
          <td :data-testid="`snapshot-count-${row.id}`">{{ row['发布快照行数'] ?? '—' }}</td>
          <td>{{ row['转换批次'] || '—' }}</td>
          <td :data-testid="`pending-batch-${row.id}`">{{ row['待发布转换批次'] || '—' }}</td>
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
            <button
              v-if="row['发布快照版本']"
              class="link"
              type="button"
              :data-testid="`versions-${row.id}`"
              @click="openVersions(row)"
            >
              快照版本
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 5" class="empty-state">暂无数据发布数据，可先登记发布版本</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记发布版本</h3>
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

    <div v-if="showVersions" class="modal-backdrop" data-testid="versions-modal" @click.self="showVersions = false">
      <section class="modal">
        <header class="page-head">
          <h3>发布快照版本 · {{ activeRow?.['版本编号'] }}</h3>
          <div class="page-actions">
            <RouterLink
              v-if="activeRow?.['来源转换节点']"
              class="link"
              data-testid="goto-transform"
              :to="{ path: '/transform_dag', query: { node: String(activeRow['来源转换节点']) } }"
            >
              去转换编排核对
            </RouterLink>
            <button class="link" type="button" data-testid="close-versions" @click="showVersions = false">关闭</button>
          </div>
        </header>
        <p class="page-desc">发布快照版本冻结发布当时的完整批次行数与查询字段，转换再次执行不会改写历史版本。</p>
        <table class="data-table">
          <thead>
            <tr>
              <th>版本</th><th>批次</th><th>行数</th><th>查询字段</th><th>冻结时间</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="version in versions" :key="version.version_no" :data-testid="`version-${version.version_no}`">
              <td>{{ version.version }}</td>
              <td>{{ version.batch_no }}</td>
              <td>{{ version.row_count }}</td>
              <td>{{ version.fields.join(',') }}</td>
              <td>{{ version.frozen_at }}</td>
              <td class="row-actions">
                <button class="link" type="button" :data-testid="`export-${version.version_no}`" @click="downloadVersion(version, 'export')">导出</button>
                <button class="link" type="button" :data-testid="`package-${version.version_no}`" @click="downloadVersion(version, 'package')">打包下载</button>
                <button class="link" type="button" :data-testid="`bookmark-${version.version_no}`" @click="saveBookmark(version)">书签</button>
                <button class="link" type="button" :data-testid="`handoff-${version.version_no}`" @click="loadHandoff(version)">交接</button>
              </td>
            </tr>
            <tr v-if="!versions.length">
              <td colspan="6" class="empty-state">该发布版本还没有发布快照版本</td>
            </tr>
          </tbody>
        </table>
        <div v-if="bookmarkUrl" class="timeline-log" data-testid="bookmark-log">
          下载书签已保存：<a :href="bookmarkUrl">{{ bookmarkUrl }}</a>
        </div>
        <div v-if="handoffInfo" class="timeline-log" data-testid="handoff-log">
          <div>调用链节点：{{ handoffInfo['调用链']?.['节点编号'] }} → {{ handoffInfo['调用链']?.['输出表名'] }}</div>
          <div>定位版本：{{ handoffInfo['定位版本'] }}</div>
          <div v-for="item in handoffInfo['完整版本'] || []" :key="item.version">
            {{ item.version }}（批次 {{ item.batch_no }}）{{ item.row_count }}行 · {{ item.complete ? '完整版本' : '半成品' }}
          </div>
        </div>
      </section>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据发布记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/data_publish'
const columns = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
const actions = ["提交校验", "确认发布", "撤回版本"]
const statuses = ["草稿", "待校验", "已发布", "已撤回"]
const metricNames = ["发布版本总数", "待校验版本", "已发布版本"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showVersions = ref(false)
const activeRow = ref<Row | null>(null)
const versions = ref<Array<Record<string, any>>>([])
const bookmarkUrl = ref('')
const handoffInfo = ref<Record<string, any> | null>(null)
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
    link.download = 'data_publish.csv'
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
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '数据发布动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据发布操作失败'
  }
}

async function openVersions(row: Row) {
  activeRow.value = row
  bookmarkUrl.value = ''
  handoffInfo.value = null
  showVersions.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}/versions`)
    if (!response.ok) throw new Error('发布快照版本读取失败')
    const payload = await response.json()
    versions.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布快照版本读取失败'
  }
}

async function downloadVersion(version: Record<string, any>, mode: 'export' | 'package') {
  if (!activeRow.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/versions/${version.version_no}/${mode}`)
    if (!response.ok) throw new Error(mode === 'export' ? '导出失败' : '打包下载失败')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = mode === 'export' ? `${version.entry_code || 'publish'}-${version.version}.csv`
      : `${version.entry_code || 'publish'}-${version.version}.zip`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载失败'
  }
}

async function saveBookmark(version: Record<string, any>) {
  if (!activeRow.value) return
  errorMessage.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/${activeRow.value.id}/versions/${version.version_no}/bookmark?kind=package`,
      { method: 'POST' },
    )
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '书签保存失败')
    bookmarkUrl.value = payload.url
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '书签保存失败'
  }
}

async function loadHandoff(version: Record<string, any>) {
  if (!activeRow.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/versions/${version.version_no}/handoff`)
    if (!response.ok) throw new Error('文件交接信息读取失败')
    handoffInfo.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文件交接信息读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('发布版本列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据发布列表读取失败'
  }
}

onMounted(reload)
</script>
