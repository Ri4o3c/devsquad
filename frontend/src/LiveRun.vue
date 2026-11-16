<script setup>
import { computed, onUnmounted, ref } from 'vue'
import ReplayDiff from './ReplayDiff.vue'
import { eventDetails, readEvents, statusLabel } from './replayEvents.js'

const enteredToken = ref('')
const sessionToken = ref('')
const repo = ref('')
const task = ref('')
const maxSteps = ref(8)
const budget = ref(0.05)
const run = ref(null)
const patch = ref(null)
const error = ref('')
const diffError = ref('')
const busy = ref(false)
const loadingDiff = ref(false)
const deciding = ref(false)
const streaming = ref(false)
const attachments = ref([])
const attachmentError = ref('')
const filePicker = ref(null)
const attachmentDrag = ref(false)
let nextAttachment = 0
let controller

const activeStatuses = new Set(['starting', 'running', 'waiting_approval'])
const active = computed(() => activeStatuses.has(run.value?.status))
const events = computed(() => readEvents(run.value?.events ?? []))
const textExtensions = ('txt md log py js ts jsx tsx vue css html json csv yaml yml toml ' +
  'ini sql sh ps1 java go rs c cpp h cs rb php').split(' ')
const attachmentExtensions = [...textExtensions, 'pdf', 'png', 'jpg', 'jpeg']
const attachmentAccept = attachmentExtensions.map(extension => '.' + extension).join(',')

function addAttachments(files) {
  if (busy.value || active.value) return
  attachmentError.value = ''
  for (const file of files) {
    const extension = file.name.toLowerCase().split('.').pop()
    const maxBytes = textExtensions.includes(extension) ? 6 * 1024 : 256 * 1024
    if (file.size < 1 || file.size > maxBytes) {
      attachmentError.value = '附件必须有内容，文本最多 6 KiB，PDF 和截图最多 256 KiB。'
      continue
    }
    if (!file.name.includes('.') || !attachmentExtensions.includes(extension)) {
      attachmentError.value = '请选择 UTF-8 代码或日志、PDF、PNG 或 JPEG。'
      continue
    }
    if (attachments.value.length >= 3) {
      attachmentError.value = '每项任务最多添加 3 个附件。'
      break
    }
    attachments.value.push({ id: ++nextAttachment, file })
  }
}

function pickAttachments(event) {
  addAttachments(event.target.files)
  event.target.value = ''
}

function pasteAttachments(event, allowText = false) {
  const clipboard = event.clipboardData
  if (clipboard?.files.length) {
    event.preventDefault()
    addAttachments(clipboard.files)
  } else if (allowText && clipboard) {
    const text = clipboard.getData('text/plain')
    if (text) {
      event.preventDefault()
      addAttachments([new File([text], '粘贴内容.txt', { type: 'text/plain' })])
    }
  }
}

async function encodeAttachment({ file }) {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let binary = ''
  for (let index = 0; index < bytes.length; index += 8192) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 8192))
  }
  return { name: file.name, data: btoa(binary) }
}

async function request(path, options = {}, token = sessionToken.value) {
  const response = await fetch('/api/runs' + path, {
    ...options, cache: 'no-store',
    headers: {
      'X-DevSquad-Token': token,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('本机访问口令无效，请检查后重新连接。')
    const problem = await response.json().catch(() => ({}))
    throw new Error(typeof problem.detail === 'string' ? problem.detail : '无法连接本机运行服务。')
  }
  return response
}

async function connect() {
  busy.value = true
  error.value = ''
  try {
    const candidate = enteredToken.value.trim()
    const response = await request('/current', {}, candidate)
    const latest = await response.json()
    sessionToken.value = candidate
    enteredToken.value = ''
    if (latest.id) follow(latest.id)
  } catch (failure) {
    error.value = failure.message
  } finally {
    busy.value = false
  }
}

function disconnect() {
  controller?.abort()
  controller = undefined
  sessionToken.value = ''
  streaming.value = false
  run.value = null
  patch.value = null
  error.value = ''
  diffError.value = ''
  attachments.value = []
  attachmentError.value = ''
}

async function start() {
  if (active.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const files = await Promise.all(attachments.value.map(encodeAttachment))
    const response = await request('', {
      method: 'POST',
      body: JSON.stringify({
        repo: repo.value, task: task.value, max_steps: maxSteps.value, max_cost_usd: budget.value,
        attachments: files,
      }),
    })
    const created = await response.json()
    attachments.value = []
    attachmentError.value = ''
    patch.value = null
    diffError.value = ''
    run.value = { id: created.id, status: 'starting', events: [] }
    follow(created.id)
  } catch (failure) {
    error.value = failure.message
  } finally {
    busy.value = false
  }
}

async function follow(id) {
  controller?.abort()
  const stream = new AbortController()
  controller = stream
  streaming.value = true
  error.value = ''
  try {
    const response = await request('/' + id + '/events', { signal: stream.signal })
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
    let buffer = ''
    let receivedTerminal = false
    try {
      while (!stream.signal.aborted) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += value
        let boundary
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          if (!frame.startsWith('data: ') || controller !== stream) continue
          const snapshot = JSON.parse(frame.slice(6))
          run.value = snapshot
          if (!activeStatuses.has(snapshot.status)) {
            receivedTerminal = true
            if (snapshot.worktree) refreshPatch()
          }
        }
      }
    } finally {
      reader.releaseLock()
    }
    if (!receivedTerminal && !stream.signal.aborted) {
      error.value = '轨迹连接中断，可以重新连接当前任务。'
    }
  } catch (failure) {
    if (!stream.signal.aborted) error.value = failure.message
  } finally {
    if (controller === stream) streaming.value = false
  }
}

async function decide(approve) {
  const id = run.value?.id
  const proposal = run.value?.approval?.id
  if (!id || !proposal || deciding.value) return
  deciding.value = true
  error.value = ''
  try {
    await request('/' + id + '/approvals/' + proposal, {
      method: 'POST', body: JSON.stringify({ approve }),
    })
    if (run.value?.approval?.id === proposal) {
      run.value = { ...run.value, approval: null, status: 'running' }
    }
  } catch (failure) {
    error.value = failure.message
  } finally {
    deciding.value = false
  }
}

async function refreshPatch() {
  const id = run.value?.id
  if (!id || loadingDiff.value) return
  loadingDiff.value = true
  diffError.value = ''
  try {
    const response = await request('/' + id + '/diff')
    const result = await response.json()
    if (run.value?.id === id) patch.value = result
  } catch (failure) {
    if (run.value?.id === id) diffError.value = failure.message
  } finally {
    loadingDiff.value = false
  }
}

onUnmounted(() => controller?.abort())
</script>

<template>
  <main class="live-page">
    <div class="page-heading">
      <div>
        <h1>实时任务</h1>
        <p>在隔离工作区运行需求，逐次确认写入和命令。</p>
      </div>
      <button v-if="sessionToken" class="button secondary" :disabled="busy"
        @click="disconnect">断开网页连接</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <form v-if="!sessionToken" class="live-card connect-form" @submit.prevent="connect">
      <h2>连接本机后端</h2>
      <label>本机访问口令
        <input v-model="enteredToken" type="password" required autocomplete="off"
          placeholder="DEVSQUAD_TOKEN" />
      </label>
      <button class="button live-action" :disabled="busy">{{ busy ? '连接中…' : '连接' }}</button>
    </form>

    <div v-else class="live-layout">
      <form class="live-card task-form" @submit.prevent="start" @paste="pasteAttachments($event)">
        <h2>新建任务</h2>
        <label>仓库绝对路径
          <input v-model.trim="repo" maxlength="2048" placeholder="/mnt/d/my-repo" required />
        </label>
        <label>需求
          <textarea v-model.trim="task" rows="5" maxlength="6000"
            placeholder="需要修改什么，以及如何验证" required></textarea>
        </label>
        <div class="attachment-field" :class="{ dragging: attachmentDrag }" tabindex="0"
          role="group" aria-label="任务附件"
          @paste.stop="pasteAttachments($event, true)"
          @dragover.prevent="attachmentDrag = true" @dragleave="attachmentDrag = false"
          @drop.stop.prevent="attachmentDrag = false; addAttachments($event.dataTransfer.files)">
          <div class="attachment-heading">
            <span>任务附件</span>
            <button type="button" class="button secondary compact" :disabled="busy || active"
              @click="filePicker.click()">选择文件</button>
            <input ref="filePicker" type="file" multiple :accept="attachmentAccept"
              aria-label="选择任务附件" hidden @change="pickAttachments" />
          </div>
          <p class="muted">拖入文件，或在此粘贴截图和文字。最多 3 个。</p>
          <ul v-if="attachments.length" class="attachment-list">
            <li v-for="item in attachments" :key="item.id">
              <span :title="item.file.name">{{ item.file.name }}</span>
              <button type="button" class="attachment-remove" :disabled="busy || active"
                :aria-label="'移除 ' + item.file.name"
                @click="attachments = attachments.filter(file => file.id !== item.id)">移除</button>
            </li>
          </ul>
          <p v-if="attachmentError" class="error" role="alert">{{ attachmentError }}</p>
        </div>
        <div class="form-limits">
          <label>最大步数
            <input v-model.number="maxSteps" type="number" min="1" max="24" required />
          </label>
          <label>预算上限（USD）
            <input v-model.number="budget" type="number" min="0.000001" step="any" required />
          </label>
        </div>
        <p class="muted">需求、所选附件中的参考内容和经过过滤的工具结果会发送给 DeepSeek。</p>
        <button class="button live-action" :disabled="busy || active">用 DeepSeek 运行</button>
      </form>

      <div class="live-output">
        <section v-if="!run" class="live-card live-empty">
          <h2>运行详情</h2>
          <p>提交需求后，执行事件和审批会显示在这里。</p>
        </section>
        <template v-else>
          <section class="live-card run-meta-card">
            <div class="live-card-heading">
              <h2>运行详情</h2>
              <span class="run-state" :class="run.status" role="status">{{ statusLabel(run.status) }}</span>
            </div>
            <dl class="run-facts">
              <dt>任务 ID</dt><dd>{{ run.id }}</dd>
              <template v-if="run.worktree">
                <dt>工作区</dt><dd>{{ run.worktree }}</dd>
              </template>
              <template v-if="run.steps !== null && run.steps !== undefined">
                <dt>步数与费用</dt><dd>{{ run.steps }} 步 · {{ run.estimated_cost_usd.toFixed(6) }} USD</dd>
              </template>
            </dl>
            <p v-if="run.detail" class="error">{{ run.detail }}</p>
            <p v-if="run.final_text" class="final-message">{{ run.final_text }}</p>
            <button v-if="active && !streaming" type="button" class="button secondary"
              @click="follow(run.id)">重新连接轨迹</button>
          </section>

          <section v-if="run.approval" class="live-card approval-card" aria-label="等待确认">
            <div class="live-card-heading"><h2>确认工具操作</h2><code>{{ run.approval.tool }}</code></div>
            <p class="muted">核对参数后批准或拒绝。超时将拒绝本次操作。</p>
            <pre class="approval-parameters" tabindex="0">{{ JSON.stringify(run.approval.arguments, null, 2) }}</pre>
            <div class="approval-actions">
              <button class="button secondary" :disabled="deciding" @click="decide(false)">拒绝</button>
              <button class="button live-action" :disabled="deciding" @click="decide(true)">批准一次</button>
            </div>
          </section>

          <section class="live-card">
            <div class="live-card-heading"><h2>运行轨迹</h2><span class="muted">{{ events.length }} 条事件</span></div>
            <p v-if="!events.length" class="muted">正在准备任务工作区…</p>
            <ol v-else class="timeline live-timeline">
              <li v-for="(event, index) in events" :key="index" :class="'kind-' + event.kind">
                <span class="dot" aria-hidden="true"></span>
                <div class="event-content">
                  <strong>{{ event.title }}</strong>
                  <p>{{ event.type }}<template v-if="eventDetails(event)"> · {{ eventDetails(event) }}</template></p>
                </div>
                <span v-if="Number.isFinite(event.meta.duration_ms)" class="event-time">{{ event.meta.duration_ms }} ms</span>
              </li>
            </ol>
          </section>

          <section v-if="run.worktree" class="live-card live-change">
            <div class="live-card-heading">
              <h2>当前代码变更</h2>
              <button class="button secondary compact" :disabled="loadingDiff"
                @click="refreshPatch">刷新 diff</button>
            </div>
            <p v-if="diffError" class="error" role="alert">{{ diffError }}</p>
            <p v-if="patch?.truncated" class="muted">变更预览已截断，完整 diff 可在本机工作区查看。</p>
            <ReplayDiff v-if="patch" :patch="patch.patch" :filename="'devsquad-' + run.id"
              :downloadable="!patch.truncated" />
            <p v-else-if="!diffError" class="muted">刷新以查看工作区当前差异。</p>
          </section>
        </template>
      </div>
    </div>
  </main>
</template>
