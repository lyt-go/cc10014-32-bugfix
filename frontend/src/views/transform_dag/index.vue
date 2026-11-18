<template>
  <section class="page" data-module="transform_dag">
    <header class="page-head">
      <div><h2>转换编排</h2><p class="page-desc">维护转换节点的输入、执行约束与结果记录，为转换编排提供可验证的数据工程流程。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记转换节点</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
        <button class="btn" :data-testid="`execute-${selectedNode.id}`" @click="executeNode(selectedNode)">执行转换</button>
        <button class="btn" :data-testid="`publish-versions-${selectedNode.id}`" @click="loadPublishVersions(selectedNode)">核对发布版本</button>
      </div>
      <section v-if="publishView" class="publish-view" data-testid="publish-view" style="grid-column:1/-1;">
        <div class="publish-view-head">发布快照版本（{{ publishView.output_table }}）</div>
        <table class="data-table">
          <thead><tr><th>发布版本</th><th>版本</th><th>批次</th><th>行数</th><th>查询字段</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="item in publishView.items" :key="`${item.发布版本id}-${item.version_no}`" :data-testid="`publish-version-${item.发布版本id}-${item.version_no}`">
              <td>{{ item.发布版本编号 }}</td>
              <td>{{ item.version }}</td>
              <td>{{ item.batch_no }}</td>
              <td>{{ item.row_count }}</td>
              <td>{{ item.fields.join(',') }}</td>
              <td>{{ item.发布状态 }}</td>
            </tr>
            <tr v-if="!publishView.items.length"><td colspan="6" class="empty-state">该节点还没有发布快照版本</td></tr>
          </tbody>
        </table>
      </section>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记转换节点</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot"><span>{{ graph.nodes.length }} 个节点</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { request } from '@/api/client'
const ENDPOINT = '/api/transform_dag'
const columns = ["节点编号", "输入表名", "输出表名", "转换脚本", "执行顺序", "依赖节点", "失败策略", "调度周期"]
const actions = ["提交校验", "标记生效", "停用节点"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const creating = ref(false); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const publishView = ref<any>(null)
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) { selectedNode.value = selectedNode.value?.id === node.id ? null : node; publishView.value = null }
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) { errorMessage.value = '加载失败' }
}
async function runAction(action: string, node: any) {
  try { await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) }); await reload() } catch(e) { errorMessage.value = '操作失败' }
}
async function executeNode(node: any) {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${node.id}/execute`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || payload.ok === false) throw new Error(payload.message || '转换执行失败')
    await loadPublishVersions(node)
  } catch(e: any) { errorMessage.value = e.message || '转换执行失败' }
}
async function loadPublishVersions(node: any) {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${node.id}/publish-versions`)
    const payload = await res.json()
    if (!res.ok) throw new Error(payload.detail || '发布版本读取失败')
    publishView.value = payload
  } catch(e: any) { errorMessage.value = e.message || '发布版本读取失败' }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
const route = useRoute()
onMounted(async () => {
  await reload()
  // 从发布快照版本看板跳转过来时，直接定位节点并核对发布版本结果。
  const nodeCode = String(route.query.node || '')
  if (!nodeCode) return
  const node = graph.nodes.find((item: any) => item.name === nodeCode)
  if (!node) return
  selectedNode.value = node
  await loadPublishVersions(node)
})
</script>
