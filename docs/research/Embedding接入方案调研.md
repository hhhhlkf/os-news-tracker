# 新闻趋势总结 Embedding 接入方案调研

> 调研日期：2026-07-29  
> 目标：为新闻解释卡的事件核心文本提供稳定的向量生成能力，供候选召回与渐进式故事线聚类使用。

## 结论

当前不应复用已配置的内部 LLM 网关作为 OpenAI 兼容 Embedding 服务。对运行中的现有配置进行了不输出 URL、令牌、模型标识或响应正文的只读能力探测：

- `GET /v1/models` 返回 `200`，共发现 2 个模型，但没有模型标识包含 `embed`；
- `POST /v1/embeddings`（使用不存在的探测模型，避免生成真实向量）返回 `404`。

因此，**当前网关未暴露标准 OpenAI `POST /v1/embeddings` 接口，也未广告可用的 Embedding 模型**。这足以否定“直接按 OpenAI Embeddings API 接入当前网关”的方案；不能排除网关另有未配置的非标准向量服务，需由网关维护方另行确认。

本项目的本地回退模型确定为 **`Qwen/Qwen3-Embedding-0.6B`**。它是 0.6B、1024 维、32K 上下文的专用文本 Embedding 模型，支持中文、英文及跨语言文本，并明确覆盖文本聚类场景；对以中英文技术新闻解释卡做相似召回的需求足够匹配。[Qwen 官方仓库](https://github.com/QwenLM/Qwen3-Embedding)

`BAAI/bge-m3` 作为备用方案，不是第一选择。它同为 1024 维多语言模型、最大 8192 token，并同时支持稠密、稀疏和多向量检索；但本阶段只需要单个稠密向量做 pgvector 相似召回，额外的混合检索能力不构成直接收益。[BAAI 官方模型卡](https://huggingface.co/BAAI/bge-m3)

## 现有网关核查

### 本地证据

项目当前只配置了 `LLM_BASE_URL`、`LLM_API_KEY` 与 `LLM_MODEL`；现有 `LlmClient` 仅实现 `POST {LLM_BASE_URL}/chat/completions`，没有 Embedding 调用或独立的模型配置。示例配置也只声明该基础地址为 OpenAI 兼容 LLM API，并未声明向量能力。

探测仅发起了以下无状态请求：

1. 使用现有鉴权调用 `GET {LLM_BASE_URL}/models`；
2. 使用一个明确不存在的模型名调用 `POST {LLM_BASE_URL}/embeddings`。

第 2 步返回 `404`，说明当前路由上没有标准 Embeddings 资源；没有使用真实模型发起向量生成，不产生业务数据或写入。

### OpenAI 兼容契约

标准 OpenAI Embeddings API 是 `POST /v1/embeddings`：请求至少包含 `model` 与字符串或字符串数组形式的 `input`，响应在 `data[].embedding` 返回浮点向量，并带有模型标识与用量信息。[OpenAI Embeddings API 参考](https://developers.openai.com/api/reference/resources/embeddings/methods/create)

后续若网关团队声称提供支持，必须以该契约完成一次验证，而不是仅确认聊天模型可用：

1. 提供可调用的 Embedding 模型 ID 和接口基础地址；
2. 用该模型对两个短文本发起 `POST /v1/embeddings`；
3. 验证 HTTP 200、`data` 数量与输入数量一致、每个 `embedding` 为相同维度的浮点数组；
4. 将返回模型 ID、维度与网关版本登记为项目的当前 Embedding 版本；
5. 通过后才将该网关作为 `openai_compatible` 提供方启用。

## 本地模型对比与选型

| 选项 | 官方能力 | 对本项目的判断 |
| --- | --- | --- |
| `Qwen/Qwen3-Embedding-0.6B` | 0.6B 参数、1024 维、32K 序列长度、可自定义向量维度与任务指令；覆盖 100+ 语言，并明确列出文本聚类能力。[Qwen 官方仓库](https://github.com/QwenLM/Qwen3-Embedding) | **首选**。解释卡是短文本，1024 维适配 pgvector；中文技术新闻与英文原文混合时的多语言能力有直接价值。初版只生成归一化稠密向量。 |
| `BAAI/bge-m3` | 1024 维、8192 token、多语言；可输出稠密、稀疏与 ColBERT 多向量表示。[BAAI 官方模型卡](https://huggingface.co/BAAI/bge-m3) | **备选**。若后续需要混合检索或 Qwen 本地部署不稳定，再替换；本阶段不引入其稀疏/多向量结构。 |

选型不依据不同厂商自行报告的榜单分数作横向性能承诺；实际效果应在项目新闻卡数据上，以“候选簇被 Agent 接受的比例、错误合并率、处理延迟”进行灰度评估。

## 接入边界与实现契约

无论最终使用网关还是本地模型，业务代码只依赖一个提供方接口，不直接依赖某个 SDK：

| 项目 | 要求 |
| --- | --- |
| 输入 | `list[str]` 的事件核心文本：`新闻主角 + 动作 + 结果 + 形成原因 + 后续影响`。空字段不拼接。 |
| 输出 | 与输入同序、同数量的 L2 归一化稠密 `list[float]`；单个提供方/版本内维度固定。 |
| 元数据 | 每次生成同时返回或记录 `provider`、`model_id`、`model_version`、`dimension`、`normalized=true` 与生成日期（`YYYY-MM-DD`）。 |
| 批处理 | 支持按配置的批大小调用；单条失败不写入向量，保留为待重试，不能让错误向量进入聚类。 |
| 版本隔离 | 一次候选召回和聚类只能查询当前统一 Embedding 版本；模型、维度、归一化或事件核心文本模板变化时，更新版本并重建受影响向量。 |
| 相似度 | pgvector 使用与归一化向量匹配的余弦距离/相似度；不得混用不同版本或不同维度的向量。 |
| 文本长度 | 应配置输入 token 上限并在模型调用前稳定截断；解释卡字段均有短文本约束，初版无需把新闻全文送给 Embedding。 |

Qwen 官方示例使用末 token pooling 后进行 L2 归一化，并建议为“查询”添加任务指令、为“文档”保留原文本。[Qwen 官方使用说明](https://github.com/QwenLM/Qwen3-Embedding) 对本项目而言，卡片间聚类属于对称的文档—文档相似性任务：初版所有卡片仅使用同一种事件核心文本模板，不添加查询指令，以避免查询/文档不对称；若将来新增“新卡片查询旧故事线”的检索模式，再引入固定英文任务指令，并把它纳入向量版本。

## 推荐落地顺序

1. 新增独立 Embedding 配置：提供方类型、模型 ID、版本、批大小、输入上限与超时；不要复用聊天模型配置名。
2. 先实现 `Qwen/Qwen3-Embedding-0.6B` 的本地提供方，产生 1024 维归一化向量；服务形态可独立于现有聊天网关。
3. 按上述统一接口实现 `openai_compatible` 提供方，但保持禁用，直到网关满足验证步骤。
4. 在一批历史解释卡上离线生成向量，检查维度一致性、重复调用稳定性及候选簇质量，再启用增量生成与 pgvector 索引。
5. 将本次结论同步到趋势实现任务 02；任务 04 以前不得假定现有聊天网关具有 Embeddings 能力。

## 参考资料

- [OpenAI Embeddings API reference](https://developers.openai.com/api/reference/resources/embeddings/methods/create)
- [Qwen3-Embedding 官方仓库](https://github.com/QwenLM/Qwen3-Embedding)
- [Qwen3-Embedding-0.6B 官方模型页](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [BAAI/bge-m3 官方模型卡](https://huggingface.co/BAAI/bge-m3)
