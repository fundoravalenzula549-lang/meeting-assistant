# 模型策略

## 固定推荐

### 实时转录 ASR

固定模型：`large-v3-turbo`

使用位置：

- `config.json` 的 `asr.local_model`
- `config.example.json` 的 `asr.local_model`
- 远端 GPU ASR 服务默认模型

推荐原因：

- 支持多语言自动语音识别，适合中文、英文、日文混合会议。
- 比完整 `large-v3` 更适合实时和准实时场景。
- 在个人/小团队工作台里，准确率、速度、资源占用更均衡。

不把 `small`、`medium` 作为默认，是因为第一版的主要价值是“可靠会议记录”，不是极限低资源运行。低配机器可以在 WebUI 临时改成 `medium`，但产品默认固定为 `large-v3-turbo`。

### 翻译和会后整理 LLM

当前跑通模型：`qwen3:4b`

使用位置：

- `config.json` 的 `llm.model`
- `config.example.json` 的 `llm.model`

推荐原因：

- 模型小，启动和推理成本低，适合先跑通完整流程。
- 可以覆盖基础翻译、校正和简短摘要。
- 对第一版验证链路更友好：先验证音频采集、ASR、字幕、导出、会后处理全链路，再换更大模型提质量。

当前 `config.json` 已把 `llm.enabled=true` 打开，用于跑通翻译和会后 AI。需要先拉取模型：

```bash
ollama pull qwen3:4b
```

如果 Ollama 没启动或模型没拉取，实时转录仍然会继续，翻译/会后整理会以 warning 形式失败。
会后整理最多等待 90 秒；如果模型生成太慢或失败，系统会先写出基础版 `meeting_notes.md`，保留逐字稿并说明失败原因。

## 后续可选档位

- 低配本地转录：`medium`
- 更高准确率离线转录：`large-v3`
- 远端 GPU 批处理：仍优先 `large-v3-turbo`，需要极致准确率再改 `large-v3`
- 更强翻译和会后整理：`qwen3:14b`
- 高质量长会议摘要：可把 LLM 切到远端 OpenAI-compatible 服务或更大本地模型
