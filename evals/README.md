# Evals

`run_deepseek.py` 在两个临时合成仓库中跑 8 个单表达式编辑，每个重复 3 次。模型只收到对应任务和旧函数。检查由脚本内的隐藏断言完成，补丁会经过语法白名单和 worktree 路径守卫。

`--csv` 保存每次试跑的验收、token 和峰值单价估算。`--replay` 导出 Vue 页面可读取的脱敏事件和合成代码 diff。脚本使用官方 DeepSeek 端点，运行前从本机进程环境读取 `DEEPSEEK_API_KEY`。
