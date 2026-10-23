# astrbot_plugin_voice_split

把一次 LLM 输出按 AstrBot「分段回复」的规则拆成多条，并对**每一段独立掷骰**决定
发语音还是发文字。

> 🤖 **AI 制作/修改说明**：本项目的代码与文档由 AI 生成并持续修改，代码和注释里可能残留 AI 风格的痕迹。Issue 可以提，但不保证全部都能及时处理。

- 忙 / 通勤 / 专注：`virtual_life` 提供 `p=0.7`
- 空闲：`virtual_life` 提供 `p=0.2`
- 没有 `virtual_life` 时：用插件配置的 `fallback_probability`（默认 0，全部文字）

## 依赖

- `astrbot_plugin_voice_file`：提供 `synthesize_voice(text) -> wav`
- `astrbot_plugin_virtual_life`（可选）：写入 `virtual_life_voice_probability` extra

## 与自带分段的关系

AstrBot 的「分段回复」开关同时决定 `RespondStage` 是否**逐条发送**（带 1~2.5s 间隔）。
关掉它会导致整条 chain 被合并发送，语音段还会被提前单独发出，顺序会乱。

因此：**保持自带「分段回复」开启**。本插件在钩子里先按同一套规则拆分并做语音决策，
核心随后对剩余的 `Plain` 段再跑一次分段——因为每段已经是单句，第二次拆分是幂等的，
不会改变结果；真正的逐条发送和间隔仍由核心负责。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 启用分段语音 |
| `fallback_probability` | `0.0` | 没有 virtual_life 概率时的兜底 |
| `max_concurrent_tts` | `3` | 语音合成并发数 |
