# 布丁智服 · Pudding Service

**面向电商客服场景的多 Agent 智能服务台**：先判断用户到底要什么，再把问题交给最合适的 Agent 处理，需要查资料时按需检索，
最后用监控和评测证明它确实在变好。

> 不是"套壳聊天框"，而是一个**可观测、可评测、可降级**的客服 Agent 运行时。

```text
用户提问 → 意图识别（三路投票） → 按意图决定是否检索 → 主/辅 Agent 路由 → 生成并校验回复
        ↘ 工作记忆 / 长期记忆 / 用户画像                    ↘ Prometheus 指标 + LLM-as-Judge 评测
```

技术栈：`Java 21` · `Spring Boot 3.5` · `Spring AI` · `LangChain4j` · `Redis` · `ChromaDB / 本地哈希向量` · `Micrometer / Prometheus` · `Docker Compose` · `Vue 3`

---

## 为什么叫"布丁"

布丁的性格是**柔软、有弹性、有边界**，这三条被写成了运行时真正生效的规则，而不是 README 里的口号：

| 人格 | 落地方式 |
|---|---|
| 柔软 | 客服话术先接住情绪再解决问题，写进 `skills/general_customer_service/SKILL.md`，运行时注入 system prompt |
| 有弹性 | 同一件事按用户身份和场景换说法，由 Skills 按 Agent 类型 + 关键词动态加载，不必重启服务 |
| 有边界 | 不承诺做不到的结果、不编造订单状态，`AnswerVerifier` 兜底校验，拿不准就 `escalated=true` 转人工 |

---

## 核心能力

### 1. 意图识别与 Agent 编排

- **三路投票定意图**：LLM Few-shot 判断 + 字符 n-gram 相似度 + 关键词规则三路加权融合，
  输出 `intent`、`intent_group`、`intent_confidence`、`source_scores`、`entities`、`urgency`。
- **低置信度先确认**：置信度不足时不硬答，先追问澄清，避免把"开发票"误判成"退款"。
- **主/辅 Agent 打分路由**：按意图、关键词、实体分别给 `general / technical / billing` 打分，
  生成 `primary_agent`、`supporting_agents`、`routing_reason`、`routing_confidence`。
- **复合问题并行处理**：像"登录报 401 又被重复扣款"这类跨域问题，拆成主处理 + 辅助处理并行执行后合并结果。

### 2. 知识增强（RAG）

- **按意图触发检索**（RAG gating）：14 个业务意图检索，问候、转人工、反馈、OTHER 这 5 类不检索，省掉无效调用。
- **混合召回**：`recursive(500, 80)` 切片后，BM25（k1=1.5、b=0.75）与 256 维哈希向量各归一，按 **0.45 / 0.55** 加权融合。
- **LLM 重排**：候选数多于 topK 时才调 LLM 重排（temp=0），失败自动回退融合分顺序，不阻塞回复。
- **工具治理**：查询改写（1 原查询 + 3 子查询）、并行召回、TTL 缓存（300s）、熔断（5 次失败 / 60s）、fallback、调用统计。
- **实现取舍**：向量用的是字符 n-gram 哈希投影，不是 embedding 模型——省掉外部依赖和冷启动成本，
  代价是语义泛化弱，所以融合里 BM25 占 0.45 兜关键词；Python 版则是真接 ChromaDB，两版对照见 `pudding-python/`。

### 3. 上下文与记忆

- **动态 Skills**：从 `skills/*/SKILL.md` 读取业务规范，按 Agent 类型和用户消息关键词注入提示词，
  调用 `POST /skills/reload` 即可热更新，不用重启。
- **分层记忆**：Redis 存当前会话工作记忆；JSON / 向量库存历史摘要、长期记忆与用户画像。
- **不阻塞主链路**：同步向量库操作放进线程池，回复不必等画像更新完成。
- **回答校验**：`AnswerVerifier` 判断回复是否解决问题、是否基于上下文、是否需要转人工。

### 4. 监控与评测

- **在线指标**：Micrometer + Prometheus 采集 Agent 成功率、延迟、工具调用统计，`GET /metrics` 直接抓取。
- **动态降权**：成功率或延迟越过阈值时计算 `monitor_penalty` 写回编排器，参与
  `routing_score = (成功率×0.7 + 延迟分×0.3) × (1 − penalty)`，用于同类型多实例的选优；
  当前每类只挂一个实例，所以它的主要价值是暴露问题 + 告警，跨域流量迁移留作后续改造（见 Roadmap）。
- **意图评测**：用标注用例算 Accuracy、Macro-F1、逐类 Precision / Recall / F1。
- **端到端评测**：真实调用编排链路生成回复，再用 LLM-as-Judge 从**相关性、准确性、完整性、有用性**四个维度打分。
- **回归检测**：结果与 `baseline` 对比，任一指标退化超过阈值即标记 regression 并给出优化建议。

---

## 目录结构

```text
pudding/
├── pudding-java/                  # 主实现：Java 21 + Spring Boot 3.5（本文档描述的就是它）
│   ├── src/main/java/com/pudding/
│   │   ├── intent/                # 意图识别（三路融合）
│   │   ├── agent/                 # 多 Agent 编排、回答校验
│   │   ├── knowledge/             # 知识库与 Hybrid RAG
│   │   ├── tool/                  # 工具层：缓存 / 超时 / 熔断 / 重排
│   │   ├── memory/                # Redis 工作记忆 + 长期记忆 / 用户画像
│   │   ├── monitor/               # 在线指标与路由降权
│   │   ├── evaluation/            # 意图评测 + LLM-as-Judge
│   │   └── api/                   # REST 接口与 Swagger
│   ├── skills/                    # 通用客服 / 技术支持 / 账单退款 三套 Skill
│   └── docker-compose.yml         # Redis + ChromaDB + App + Prometheus + Nginx
├── pudding-python/                # 原型实现：FastAPI 版，附完整 wiki（架构图、业务流程图）
├── pudding-frontend/              # Vue3 调试台：对话 / 知识库 / 评测三个视图
└── docs/                          # 设计说明与简历条目
```

---

## 快速开始

### 1. 准备

- Docker 与 Docker Compose
- 一个模型 API Key：`ANTHROPIC_API_KEY`（默认 profile）或 `DEEPSEEK_API_KEY`（`SPRING_PROFILES_ACTIVE=deepseek`）

### 2. 启动

```bash
cd pudding-java
cp .env.example .env        # 填入你自己的 API Key
docker compose up -d --build
docker compose ps
docker compose logs -f pudding-java
```

一键拉起：`Redis` + `ChromaDB` + `App` + `Prometheus` + `Nginx`。

### 3. 验证

```bash
curl http://localhost:8080/health
```

| 入口 | 地址 |
|---|---|
| API | `http://localhost:8080` |
| Swagger / 调试页 | `http://localhost:8080/docs` |
| 健康检查 | `GET /health` |
| 监控快照 | `GET /monitor` |
| Prometheus 指标 | `GET /metrics` |

### 4. 本地跑（不用 Docker）

```bash
cd pudding-java
./mvnw spring-boot:run     # Windows: mvnw.cmd spring-boot:run
```

需要本地有 Redis（`REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD`）。知识库存 `data/java/knowledge-store.json`，
检索用本地哈希向量 + BM25，不依赖外部向量库（Chroma 容器保留在 compose 里，Java 侧为可移植性未启用其自动配置）。

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/chat` | 对话主入口：意图识别 → 检索 → 路由 → 生成 → 校验 |
| `POST` | `/search` | 直接检索知识库（调参、看召回效果用） |
| `POST` | `/knowledge/add` | 批量导入知识文档 |
| `GET` | `/knowledge/stats` | 知识库统计 |
| `GET` | `/skills` | 查看已加载的 Skill |
| `POST` | `/skills/reload` | 热加载 Skill（改完 SKILL.md 立刻生效） |
| `GET` | `/monitor` | Agent 成功率、延迟、工具统计快照 |
| `GET` | `/metrics` | Prometheus 文本格式指标 |
| `POST` | `/eval/run` | 跑意图评测 + LLM-as-Judge，返回分数与回归结论 |
| `GET` | `/trace/tool/{requestId}` | 某次请求的工具调用链路 |
| `GET` | `/trace/tools` | 最近的工具调用记录 |

### `/chat` 示例

```bash
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"登录一直 401，而且被扣了两次款","user_id":"u1001"}'
```

```json
{
  "conv_id": "conv-001",
  "request_id": "a1b2c3d4",
  "response": "先帮你拆成两件事：登录异常我按技术问题排查…… 扣款记录我按账单问题核实……",
  "intent": "billing",
  "intent_group": "billing",
  "primary_agent": "technical",
  "supporting_agents": ["billing"],
  "routing_reason": "包含登录故障关键词，同时命中扣款实体，按技术主处理、账单辅助",
  "routing_confidence": 0.82,
  "escalated": false,
  "knowledge_used": true,
  "latency_ms": 253
}
```

路由原因和置信度都返回给调用方，出问题时能直接判断是**意图错**还是**打分错**。

---

## 关键实现位置

| 能力 | 代码 |
|---|---|
| 三路融合意图识别 | [`intent/IntentRecognizer.java`](pudding-java/src/main/java/com/pudding/intent/IntentRecognizer.java) |
| 主/辅 Agent 编排 | [`agent/AgentOrchestrator.java`](pudding-java/src/main/java/com/pudding/agent/AgentOrchestrator.java) |
| 回答校验与转人工判断 | [`agent/AnswerVerifier.java`](pudding-java/src/main/java/com/pudding/agent/AnswerVerifier.java) |
| Hybrid RAG（BM25 + 向量 + 重排） | [`knowledge/KnowledgeBaseService.java`](pudding-java/src/main/java/com/pudding/knowledge/KnowledgeBaseService.java) |
| 工具层：缓存 / 超时 / 熔断 / 降级 | [`tool/KnowledgeToolManager.java`](pudding-java/src/main/java/com/pudding/tool/KnowledgeToolManager.java) |
| 熔断器实现 | [`tool/CircuitBreaker.java`](pudding-java/src/main/java/com/pudding/tool/CircuitBreaker.java) |
| 分层记忆 | [`memory/MemoryManager.java`](pudding-java/src/main/java/com/pudding/memory/MemoryManager.java) |
| Skills 热加载 | [`skill/SkillManager.java`](pudding-java/src/main/java/com/pudding/skill/SkillManager.java) |
| 在线监控与路由降权 | [`monitor/PerformanceMonitor.java`](pudding-java/src/main/java/com/pudding/monitor/PerformanceMonitor.java) |
| 评测与回归检测 | [`evaluation/EndToEndEvaluator.java`](pudding-java/src/main/java/com/pudding/evaluation/EndToEndEvaluator.java) |
| LLM-as-Judge 打分 | [`evaluation/LLMJudge.java`](pudding-java/src/main/java/com/pudding/evaluation/LLMJudge.java) |

---

## 几个设计取舍

**为什么不让大模型直接回答，而要先做意图识别？**
客服问题的处理动作是分叉的：退款要核对订单、登录异常要排障、投诉要转人工。只靠一个 prompt，模型会"什么都答一点"，
还会编造订单状态。先用三路投票把意图定下来，才能决定查不查知识库、走哪个 Agent、要不要人工。

**为什么要多 Agent，而不是多个 prompt？**
不同业务的话术规范和调优目标不一样，拆开之后每个 Agent 能独立注入 Skills、独立看指标；
路由层只负责决策，某个 Agent 表现变差时由 Monitor 扣分，不用改代码就能把流量挪走。

**RAG 为什么按意图触发？**
问候、感谢、"转人工"这类请求检索是纯粹的浪费，还会把无关片段塞进上下文造成答非所问。
先看意图再决定检索，是成本和质量的双赢。

**为什么记忆要分 Redis 和向量库？**
工作记忆要求低延迟、按会话读写；长期摘要和用户画像要求语义检索。两类需求放一个存储里必然有一边将就。

**熔断和降级怎么避免"静默失败"？**
工具层记录每次调用的成功率和延迟，连续失败进熔断；回复侧由 `AnswerVerifier` 兜底，必要时主动转人工，
而不是把兜底话术当正常回复返回。

---

## 评测与监控怎么看

```bash
# 指标：各 Agent 成功率、延迟、工具调用
curl http://localhost:8080/metrics

# 在线快照：当前降权情况
curl http://localhost:8080/monitor

# 跑一轮评测：意图 Accuracy / Macro-F1 + 四维质量分 + 回归结论
curl -X POST http://localhost:8080/eval/run -H 'Content-Type: application/json' -d '{}'
```

传空对象即用内置用例；也可以自己传 `intentCases` / `dialogCases` 做回归。

---

## Roadmap

- [ ] 把 `monitor_penalty` 乘进领域打分（`domainScores`），实现跨 Agent 的流量迁移，而不只是同类型多实例选优
- [ ] 用真实工单数据补充意图标注集，替换内置用例
- [ ] 检索链路加缓存命中率指标，进一步压低平均延迟
- [ ] 支持多租户隔离（不同商家的知识库与 Skills 分开）
- [ ] 把评测接进 CI，让 regression 在合并前暴露

---

## 说明

- 本项目为个人工程实践，模型调用需要自备 API Key；仓库不包含任何密钥，`.env` 已在 `.gitignore` 中忽略。
- `data/` 为运行时生成的记忆与知识库持久化目录，同样不入库。
- 如果只关心 Java 实现，可以只看 `pudding-java/`；`pudding-python/` 是最初的原型，保留是为了对照两套实现的设计差异。
