<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  patch: { type: String, required: true },
  filename: { type: String, required: true },
  downloadable: { type: Boolean, default: true },
})
const selectedFile = ref('all')

function patchFiles(patch) {
  const files = []
  let file
  let oldLine = null
  let newLine = null

  const lines = patch.replace(/\r\n/g, '\n').split('\n')
  if (lines.at(-1) === '') lines.pop()
  for (const line of lines) {
    if (!file || line.startsWith('diff --git ')) {
      file = { name: '未命名补丁', rows: [], added: 0, removed: 0 }
      files.push(file)
      oldLine = null
      newLine = null
    }
    const header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line)
    const row = { old: '', new: '', mark: '', text: line, type: 'meta' }
    if (header) {
      oldLine = Number(header[1])
      newLine = Number(header[2])
      row.type = 'hunk'
    } else if (oldLine !== null && [' ', '+', '-'].includes(line[0])) {
      row.mark = line[0]
      row.text = line.slice(1)
      if (row.mark !== '+') row.old = oldLine++
      if (row.mark !== '-') row.new = newLine++
      if (row.mark === '+') {
        row.type = 'addition'
        file.added++
      } else if (row.mark === '-') {
        row.type = 'deletion'
        file.removed++
      } else {
        row.type = 'context'
      }
    } else if (line.startsWith('+++ ') || (line.startsWith('--- ') && file.name === '未命名补丁')) {
      const raw = line.slice(4).split('\t', 1)[0].replace(/^"|"$/g, '')
      if (raw && raw !== '/dev/null') file.name = raw.replace(/^[ab]\//, '')
    }
    file.rows.push(row)
  }
  return files
}

const files = computed(() => props.patch.trim() ? patchFiles(props.patch) : [])
const shown = computed(() => selectedFile.value === 'all'
  ? files.value
  : files.value.filter((_, index) => String(index) === selectedFile.value))
watch(() => props.patch, () => { selectedFile.value = 'all' })

function download() {
  const url = URL.createObjectURL(new Blob([props.patch], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = props.filename.replace(/[^a-z0-9_.-]+/gi, '-').slice(0, 80) + '.patch'
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <p v-if="!files.length" class="panel-empty">这次试跑没有生成代码变更。</p>
  <div v-else class="diff-view">
    <div class="panel-toolbar">
      <label v-if="files.length > 1" class="file-select">
        <span class="sr-only">筛选变更文件</span>
        <select v-model="selectedFile">
          <option value="all">全部文件</option>
          <option v-for="(file, index) in files" :key="index" :value="String(index)">{{ file.name }}</option>
        </select>
      </label>
      <span v-else class="muted">1 个文件</span>
      <button v-if="downloadable" class="button secondary compact" @click="download">下载补丁</button>
    </div>
    <section v-for="(file, index) in shown" :key="index" class="diff-file">
      <div class="diff-heading">
        <h3>{{ file.name }}</h3>
        <span class="diff-counts" aria-label="变更行数">
          <span class="text-pass">+{{ file.added }}</span>
          <span class="text-fail">−{{ file.removed }}</span>
        </span>
      </div>
      <div class="diff-scroll" tabindex="0" :aria-label="file.name + ' 的补丁'">
        <div class="diff-grid">
          <div v-for="(row, rowIndex) in file.rows" :key="rowIndex" class="diff-row" :class="row.type">
            <span class="line-no" aria-hidden="true">{{ row.old }}</span>
            <span class="line-no" aria-hidden="true">{{ row.new }}</span>
            <span class="line-mark">{{ row.mark }}</span>
            <code>{{ row.text }}</code>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
