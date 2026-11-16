const labels = {
  'run.started': '启动运行',
  'run.retrieval': '完成检索',
  'run.plan': '生成实施计划',
  'run.plan.error': '规划失败',
  'model.started': '调用模型',
  'model.completed': '收到模型回复',
  'model.error': '模型调用出错',
  'tool.approval': '人工确认',
  'tool.started': '分发工具',
  'tool.completed': '工具执行完成',
  'tool.rejected': '工具请求被拒',
  'tool.error': '工具执行出错',
  'loop.result': '运行结束',
}
const statuses = {
  starting: '准备工作区',
  running: '运行中',
  waiting_approval: '等待确认',
  completed: '正常结束',
  step_limit: '达到步数上限',
  cost_limit: '达到费用上限',
  denied: '工具请求被拒',
  ask_human: '确认中断',
  error: '执行出错',
}
export function statusLabel(value) {
  return Object.hasOwn(statuses, value) ? statuses[value] : value
}

export function readEvents(lines) {
  return lines.map(line => {
    const [, type = '未知事件', details = '{}'] = line.split('\t')
    let meta = {}
    try {
      const parsed = JSON.parse(details)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) meta = parsed
    } catch {
      // Keep partial recordings readable.
    }
    const kind = type.startsWith('model.') ? 'model' : type.startsWith('tool.') ? 'tool' : 'run'
    const title = Object.hasOwn(labels, type) ? labels[type] : type
    return { type, kind, title, meta }
  })
}

export function eventDetails({ meta }) {
  const fields = []
  if (Number.isFinite(meta.step)) fields.push('第 ' + meta.step + ' 步')
  if (Number.isFinite(meta.hit_count)) fields.push('命中 ' + meta.hit_count + ' 处')
  if (Number.isFinite(meta.input_tokens) && Number.isFinite(meta.output_tokens)) {
    fields.push('输入 ' + meta.input_tokens + ' / 输出 ' + meta.output_tokens + ' tokens')
  }
  if (typeof meta.approved === 'boolean') fields.push(meta.approved ? '已批准' : '已拒绝')
  if (typeof meta.status === 'string') fields.push(statusLabel(meta.status))
  if (Number.isInteger(meta.returncode)) fields.push('退出码 ' + meta.returncode)
  if (typeof meta.tool_name === 'string') fields.push(meta.tool_name)
  return fields.join(' · ')
}
