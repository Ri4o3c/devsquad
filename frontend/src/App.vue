<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import LiveRun from './LiveRun.vue'
import ReplayDiff from './ReplayDiff.vue'
import { eventDetails, readEvents } from './replayEvents.js'

const liveAvailable = location.protocol === 'http:' &&
  ['127.0.0.1', 'localhost'].includes(location.hostname)
const page = ref('replay')
const report = ref(null)
const sample = ref(null)
const source = ref('')
const groupId = ref('')
const repeat = ref(1)
const activeTab = ref('events')
const query = ref('')
const failureOnly = ref(false)
const eventFilter = ref('all')
const error = ref('')
const loading = ref(true)
const dragDepth = ref(0)
const fileInput = ref(null)
let importVersion = 0

const phases = [
  { id: 'all', title: '全部事件' },
  { id: 'model', title: '模型' },
  { id: 'tool', title: '工具与审批' },
  { id: 'run', title: '运行与检索' },
]

const runs = computed(() => report.value?.runs ?? [])
const groups = computed(() => {
  const result = new Map()
  for (const run of runs.value) {
    const key = JSON.stringify([run.repo, run.case])
    if (!result.has(key)) {
      result.set(key, { key, name: run.case, repo: run.repo, runs: [], passed: 0 })
    }
    const group = result.get(key)
    group.runs.push(run)
    group.passed += run.passed
  }
  for (const group of result.values()) group.runs.sort((a, b) => a.repeat - b.repeat)
  return [...result.values()]
})
const visibleGroups = computed(() => {
  const search = query.value.trim().toLowerCase()
  return groups.value.filter(group =>
    (!failureOnly.value || group.passed < group.runs.length) &&
    (group.name.toLowerCase().includes(search) || group.repo.toLowerCase().includes(search)),
  )
})
const failedCases = computed(() => groups.value.filter(group => group.passed < group.runs.length).length)
const chosenGroup = computed(() => groups.value.find(group => group.key === groupId.value))
const chosen = computed(() => chosenGroup.value?.runs.find(run => run.repeat === repeat.value))
const totals = computed(() => runs.value.reduce((sum, run) => {
  sum.passed += run.passed
  sum.input += run.input_tokens
  sum.output += run.output_tokens
  sum.cost += Number(run.peak_cost_cny)
  sum.repos.add(run.repo)
  return sum
}, { passed: 0, input: 0, output: 0, cost: 0, repos: new Set() }))
const passRate = computed(() => runs.value.length ? totals.value.passed * 100 / runs.value.length : 0)

const events = computed(() => readEvents(chosen.value?.events ?? []))
const shownEvents = computed(() => eventFilter.value === 'all'
  ? events.value
  : events.value.filter(event => event.kind === eventFilter.value))

function formatCount(value) {
  return value.toLocaleString('zh-CN')
}

function validReport(payload) {
  if (typeof payload?.model !== 'string' || !payload.model.trim() ||
      !Array.isArray(payload.runs) || !payload.runs.length || payload.runs.length > 300) {
    throw new Error('请选择 DevSquad 评估脚本导出的 JSON 回放文件。')
  }

  const identifiers = new Set()
  for (const run of payload.runs) {
    if (!run || typeof run.case !== 'string' || !run.case.trim() ||
        typeof run.repo !== 'string' || !run.repo.trim() ||
        !Number.isSafeInteger(run.repeat) || run.repeat < 1 ||
        (run.passed !== 0 && run.passed !== 1) ||
        !['input_tokens', 'output_tokens', 'model_ms'].every(
          key => Number.isFinite(run[key]) && run[key] >= 0,
        ) || !['number', 'string'].includes(typeof run.peak_cost_cny) ||
        String(run.peak_cost_cny).trim() === '' ||
        !Number.isFinite(Number(run.peak_cost_cny)) || Number(run.peak_cost_cny) < 0 ||
        typeof run.requirement !== 'string' || typeof run.diff !== 'string' ||
        !Array.isArray(run.events) || !run.events.every(event => typeof event === 'string')) {
      throw new Error('回放缺少可用的用例、试跑指标或事件。')
    }
    const identity = JSON.stringify([run.repo, run.case, run.repeat])
    if (identifiers.has(identity)) throw new Error('回放中存在重复的试跑编号。')
    identifiers.add(identity)
  }
  return payload
}

function selectGroup(group) {
  groupId.value = group.key
  repeat.value = group.runs[0].repeat
  activeTab.value = 'events'
  eventFilter.value = 'all'
}

function activateReport(payload, label) {
  report.value = payload
  source.value = label
  error.value = ''
  query.value = ''
  failureOnly.value = false
  selectGroup(groups.value[0])
}

function restoreSample() {
  if (sample.value) {
    importVersion++
    activateReport(sample.value, '')
  }
}

async function openReport(file) {
  if (!file) return
  const version = ++importVersion
  if (file.size > 1024 * 1024) {
    error.value = '回放文件不能大于 1 MB。'
    return
  }
  try {
    const payload = validReport(JSON.parse(await file.text()))
    if (version === importVersion) activateReport(payload, file.name)
  } catch (failure) {
    if (version === importVersion) {
      error.value = failure instanceof SyntaxError ? '文件内容不是有效的 JSON。' : failure.message
    }
  }
}

function onFileChange(event) {
  openReport(event.target.files[0])
  event.target.value = ''
}

function onPaste(event) {
  if (page.value !== 'replay') return
  const file = event.clipboardData?.files[0]
  if (file) {
    event.preventDefault()
    openReport(file)
  }
}

function onDragEnter(event) {
  if (page.value === 'replay' && event.dataTransfer?.types.includes('Files')) dragDepth.value++
}

function onDragLeave(event) {
  if (event.dataTransfer?.types.includes('Files')) dragDepth.value = Math.max(0, dragDepth.value - 1)
}

function onDrop(event) {
  dragDepth.value = 0
  if (page.value === 'replay') openReport(event.dataTransfer?.files[0])
}

onMounted(async () => {
  window.addEventListener('paste', onPaste)
  try {
    const embedded = document.getElementById('embedded-report')
    if (embedded) {
      sample.value = validReport(JSON.parse(embedded.textContent))
    } else {
      const response = await fetch(import.meta.env.BASE_URL + 'benchmark.json')
      if (response.ok) sample.value = validReport(await response.json())
    }
    if (sample.value && importVersion === 0) activateReport(sample.value, '')
  } catch {
    // The import action remains available when the sample cannot be read.
  } finally {
    loading.value = false
  }
})
onUnmounted(() => window.removeEventListener('paste', onPaste))
</script>

<template>
  <div class="app" @dragover.prevent @dragenter.prevent="onDragEnter"
    @dragleave="onDragLeave" @drop.prevent="onDrop">
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand"><span class="mark" aria-hidden="true">&gt;_</span>DevSquad</div>
        <nav v-if="liveAvailable" class="page-nav" aria-label="工作台视图">
          <button :aria-pressed="page === 'replay'" @click="page = 'replay'">运行回放</button>
          <button :aria-pressed="page === 'live'" @click="page = 'live'">实时任务</button>
        </nav>
        <div class="topbar-actions">
          <button v-if="page === 'replay' && sample && source" class="button ghost" @click="restoreSample">查看示例</button>
          <button v-if="page === 'replay'" class="button primary" @click="fileInput.click()">导入 JSON</button>
          <input ref="fileInput" type="file" accept=".json,application/json" hidden
            @change="onFileChange" />
        </div>
      </div>
    </header>

    <div v-if="page === 'replay' && dragDepth" class="drop-overlay" aria-hidden="true">
      <div><strong>放开即可导入</strong><span>DevSquad JSON 回放文件</span></div>
    </div>

    <LiveRun v-if="liveAvailable" v-show="page === 'live'" />
    <main v-show="page === 'replay'">
      <div class="page-heading">
        <div>
          <h1>运行回放</h1>
          <p>查看评估结果，按用例回放执行轨迹和代码变更。</p>
        </div>
        <div v-if="report" class="report-meta">
          <span class="report-source" :title="source">{{ source || '内置评估样例' }}</span>
          <span class="model">{{ report.model }}</span>
        </div>
      </div>

      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div v-if="!report" class="empty">
        <p v-if="loading" role="status">正在读取示例…</p>
        <template v-else>
          <h2>打开评估回放</h2>
          <p>选择、拖放或粘贴评估脚本导出的 JSON 文件。</p>
          <button class="button secondary" @click="fileInput.click()">选择文件</button>
          <button v-if="sample" class="button secondary" @click="restoreSample">查看示例</button>
        </template>
      </div>

      <template v-else>
        <dl class="metrics">
          <div class="metric">
            <dt>评估通过率</dt><dd>{{ Number(passRate.toFixed(1)) }}<small>%</small></dd>
            <dd class="metric-detail">
              <span>{{ totals.passed }} / {{ runs.length }} 次通过</span>
              <span class="rate-track" aria-hidden="true"><span :style="{ width: passRate + '%' }"></span></span>
            </dd>
          </div>
          <div class="metric">
            <dt>评估用例</dt><dd>{{ groups.length }}</dd>
            <dd class="metric-detail">{{ totals.repos.size }} 个仓库 · {{ runs.length }} 次试跑</dd>
          </div>
          <div class="metric">
            <dt>总 Token</dt><dd>{{ formatCount(totals.input + totals.output) }}</dd>
            <dd class="metric-detail">输入 {{ formatCount(totals.input) }} · 输出 {{ formatCount(totals.output) }}</dd>
          </div>
          <div class="metric">
            <dt>预估费用</dt><dd><small>¥</small>{{ totals.cost.toFixed(4) }}</dd>
            <dd class="metric-detail">按高峰单价估算</dd>
          </div>
        </dl>

        <div class="workspace">
          <aside class="case-panel" aria-label="用例列表">
            <div class="case-panel-heading">
              <h2>评估用例</h2><span class="counter">{{ visibleGroups.length }} / {{ groups.length }}</span>
            </div>
            <div class="case-controls">
              <label>
                <span class="sr-only">搜索用例</span>
                <input v-model="query" type="search" placeholder="搜索用例或仓库" />
              </label>
              <div class="segmented" aria-label="按结果筛选">
                <button :aria-pressed="!failureOnly" @click="failureOnly = false">全部</button>
                <button :aria-pressed="failureOnly" @click="failureOnly = true">含失败 <small>{{ failedCases }}</small></button>
              </div>
            </div>
            <div class="case-list">
              <p v-if="!visibleGroups.length" class="list-empty">没有匹配的用例。</p>
              <button v-for="group in visibleGroups" :key="group.key" class="case"
                :class="{ selected: group.key === groupId }" :aria-pressed="group.key === groupId"
                @click="selectGroup(group)">
                <span class="case-name"><strong>{{ group.name }}</strong><small>{{ group.repo }}</small></span>
                <span class="case-score" :class="group.passed === group.runs.length ? 'text-pass' : 'text-fail'">
                  {{ group.passed }}/{{ group.runs.length }}
                </span>
              </button>
            </div>
          </aside>

          <section v-if="chosen" class="detail" aria-label="运行详情">
            <div class="case-heading">
              <div>
                <div class="detail-repo">{{ chosen.repo }}</div>
                <h2>{{ chosen.case }}</h2>
                <p>{{ chosen.requirement }}</p>
              </div>
              <span class="status" :class="chosen.passed ? 'pass' : 'fail'">
                <span aria-hidden="true"></span>{{ chosen.passed ? '通过' : '未通过' }}
              </span>
            </div>

            <div class="run-strip">
              <div class="run-options" aria-label="选择试跑">
                <button v-for="run in chosenGroup.runs" :key="run.repeat"
                  :aria-pressed="run.repeat === repeat" @click="repeat = run.repeat">
                  <span class="run-dot" :class="{ bad: !run.passed }" aria-hidden="true"></span>
                  第 {{ run.repeat }} 次
                </button>
              </div>
              <div class="run-usage">
                <span>{{ (chosen.model_ms / 1000).toFixed(2) }} 秒</span>
                <span>{{ formatCount(chosen.input_tokens + chosen.output_tokens) }} tokens</span>
              </div>
            </div>

            <div class="tabs" aria-label="切换详情">
              <button :aria-pressed="activeTab === 'events'" @click="activeTab = 'events'">运行轨迹</button>
              <button :aria-pressed="activeTab === 'diff'" @click="activeTab = 'diff'">代码变更</button>
            </div>

            <div v-if="activeTab === 'events'" class="event-panel">
              <div class="panel-toolbar">
                <label class="event-select">
                  <span class="sr-only">筛选事件类型</span>
                  <select v-model="eventFilter">
                    <option v-for="phase in phases" :key="phase.id" :value="phase.id">{{ phase.title }}</option>
                  </select>
                </label>
                <span class="muted">{{ shownEvents.length }} 条事件</span>
              </div>
              <p v-if="!shownEvents.length" class="panel-empty">没有匹配的轨迹事件。</p>
              <ol v-else class="timeline">
                <li v-for="(event, index) in shownEvents" :key="index" :class="'kind-' + event.kind">
                  <span class="dot" aria-hidden="true"></span>
                  <div class="event-content">
                    <strong>{{ event.title }}</strong>
                    <p>{{ event.type }}<template v-if="eventDetails(event)"> · {{ eventDetails(event) }}</template></p>
                  </div>
                  <span v-if="Number.isFinite(event.meta.duration_ms)" class="event-time">{{ event.meta.duration_ms }} ms</span>
                </li>
              </ol>
            </div>
            <ReplayDiff v-else :patch="chosen.diff"
              :filename="chosen.case + '-run-' + chosen.repeat" />
          </section>
        </div>
      </template>
    </main>
  </div>
</template>
