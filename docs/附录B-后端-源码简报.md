# 布丁智服 Java 版 · 后端/可观测性技术简报（面试讲稿版）

> 全部数值均来自真实文件（逐行读过）。凡"文件里没写"的，明确标注为 `框架默认` 或 `未找到`。
> 技术栈事实：Java 21 / Spring Boot **3.5.0** / Spring AI **1.1.0** / springdoc 2.8.17 / micrometer-registry-prometheus（`pom.xml`）。

---

## 0. 一句话总览

`/chat` 是一条"**Redis 工作记忆 → 三路投票意图识别 → 知识库工具（改写+并行召回+LLM rerank+熔断）→ 多 Agent 编排 → 答案校验 → 记忆写回 + 异步画像**"的同步链路；监控是**进程内 10s 采样 + 惩罚回写路由分**；评测是**意图 P/R/F1 + LLM-as-Judge 四维打分 + 5% 相对退化判定**；模型接入靠 **Spring profile 二选一 + 关键词静态兜底**。

---

## 1. 记忆体系真实实现（`com.pudding.memory`）

### 1.1 Redis 里到底存什么

| 用途 | Key 命名规则 | Redis 类型 | TTL | 写入时机 |
|---|---|---|---|---|
| 工作记忆（近 N 轮对话） | `wm:{userId}:{conversationId}` | List（`leftPush` 头插，即 index 0 = 最新） | **86400 s（24h）** | 每条消息 `addMessage` 写入后**刷新** TTL；压缩后重建并再刷新一次 |
| 会话摘要 | `summary:{userId}:{conversationId}` | String | **86400 s** | 每次压缩时把新摘要 `oldSummary + "\n" + summary` 追加 |

- `wmKey()` / `summaryKey()`（`MemoryManager.java:182/186`）：`userId`、`conversationId` 空值被 `safe()` 换成 `""`，**不做 hash、不做转义**（`conversationId` 里带 `:` 会有 key 歧义风险）。
- TTL 来源：`pudding.memory.ttl-seconds = ${MEMORY_TTL_SECONDS:86400}`（`application.yml:55`），`PuddingProperties.Memory.ttlSeconds = 86400`（默认值一致）。
- **画像和"情节记忆"不在 Redis**：`profileStore`（`ConcurrentHashMap`）和 `episodicStore`（`CopyOnWriteArrayList`）是**进程内内存 + 本地 JSON 文件**（见 1.4）。所以"记忆体系"实际是三处存储：Redis（窗口+摘要）/ JVM 堆（画像+情节）/ 本地文件（画像+情节持久化）。

### 1.2 一条消息的 JSON 结构

`ConversationMessage`（record）：`role`、`content`、`timestamp`、`metadata`。序列化用**容器里的 ObjectMapper**（受 `spring.jackson.property-naming-strategy: SNAKE_CASE` 影响），实际写入形如：

```json
{"role":"USER","content":"我想申请退款，订单号是 #12345","timestamp":"2026-01-01T10:00:00Z","metadata":{}}
```

- `role` 只有三值：`USER / ASSISTANT / SYSTEM`（`MessageRole.java`，`SYSTEM` 定义了但**全链路未使用**）。
- `metadata` 恒为 `Map.of()`（空对象），属于预留位，`addMessage` 里写死。
- 读：`getWorkingMemory()` 用 `LRANGE 0..(workingMax-1)`（= **0..19**），然后**倒序**还原成时间正序（`MemoryManager.java:98-114`）。

### 1.3 记忆压缩 / 摘要（真实触发条件）

| 项 | 真实值 | 出处 |
|---|---|---|
| 触发条件 | `addMessage` 后 `LLEN(wm:*) >= compress-at` | `MemoryManager.java:63-66` |
| 阈值 | **15** 条（`MEMORY_COMPRESS_AT`，默认值同） | `application.yml:57` / `PuddingProperties.Memory.compressAt=15` |
| 读取上限 | **20** 条（`MEMORY_WORKING_MAX`） | `application.yml:56` / `workingMax=20` |
| 保留条数 | `keep = min(5, size)`，**只留最近 5 条** | `MemoryManager.java:121` |
| 压缩粒度 | **按消息条数，不是字符/token**（全仓库无 token 计数逻辑） | — |
| 执行方式 | **同步**（`compress()` 未加 `@Async`），在 `/chat` 请求线程内执行 | `MemoryManager.java:65` |
| 摘要 prompt | `用 2-3 句话总结以下对话关键信息：\n{dialogue}`，`temperature=0.0, maxTokens=256` | `MemoryManager.java:130` |
| 失败兜底 | `"对话包含 N 条历史消息。"`（N = 本轮被压缩掉的条数） | `MemoryManager.java:132` |
| 压缩后 | 删 `wm:*` → 把 5 条按原顺序 `leftPush` 回去 → 刷新 TTL；摘要追加进 `summary:*` | `MemoryManager.java:136-148` |
| 压缩后的"有效间隔" | 从 5 条涨到 15 条需再写 **10 条消息**（≈5 轮问答）才再次压缩 | 由 15−5 推导 |
| 副作用 | 每次压缩会 `episodicStore.add(...)` 一条情节记忆 + **全量重写 `memory-store.json`** | `MemoryManager.java:134-135` |

**摘要怎么拼进上下文**（`MemoryContext.toPromptText`，`MemoryContext.java:15-41`）——顺序固定、共 4 段：

```
[会话摘要]
{summary}                        ← 来自 Redis summary:*

[相关历史]
- {episodic summary} ...         ← 只注入前 3 条（检索取回 5 条，注入截断到 3）

[用户画像]
{"preferences":[...],"entities":{...}}   ← JSON 字符串

[最近对话]
user: ...
assistant: ...                   ← 最多 20 条（workingMax）
```

注意：**最近对话段不截断字符**，20 条原始消息全量进 prompt；`/chat` 里另有一份"最近 5 条"history 只用于意图识别与 Agent 请求（`PuddingController.java:106-109`）——**两个窗口，不同长度**，这是个高频讲错点。

### 1.4 用户画像：怎么生成、存哪、何时更新

- **生成**：`MemoryManager.updateProfile(userId, conversationId)`（`MemoryManager.java:72-96`）
  - 取工作记忆 → 只取**最后 10 条**（`tail(messages, 10)`）→ 拼 `role: content`
  - prompt（原文）：
    ```
    从以下客服对话中提炼用户画像，返回 JSON：
    {text}
    格式: {"preferences":["..."],"entities":{"产品":[],"问题类型":[]}}
    ```
  - `llmGateway.chat("", prompt, 0.0, 512)` —— system prompt 传空串，温度 0，512 token
  - 解析用 `sliceJson()`：取第一个 `{` 到最后一个 `}`，失败则 `{}`
- **存哪**：`profileStore: Map<userId, Map<String,Object>>`（JVM 内存）→ `persistMemory()` 全量写 `data/java/memory-store.json` 的 `profiles` 字段；启动 `@PostConstruct init() → loadPersistentMemory()` 回灌（`MemoryManager.java:220-248`，会打日志 `Loaded persisted memory: episodic=…, profiles=…`）
- **何时更新**：**每次 `/chat` 的最后一步**都调一次 `memoryManager.updateProfile(...)`（`PuddingController.java:137`），方法上有 `@Async` → 走线程池、异步执行；**评测链路（`/eval/run`）不更新画像**（evaluator 直接调 orchestrator，绕过 MemoryManager）。

### 1.5 情节记忆检索（"相关历史"从哪来）

- 存储：`episodicStore`（`CopyOnWriteArrayList<EpisodicEntry>`，字段 `userId/conversationId/summary/fullText/timestamp/embedding`）
- 检索 `searchEpisodic()`（`MemoryManager.java:151-162`）：`query == null || blank` → 空；否则**对全量情节记忆做 O(n) 过滤（userId 精确匹配）+ 余弦相似度排序，取 top 5**
- 向量 `embed()` 是**自研 256 维特征哈希**：字符 n-gram（n=1..3），`String.hashCode()`，`floorMod(hash,256)` 定位，`(hash & 1)==0 ? +1 : -1`
  - 讲清楚：**这不是 embedding 模型**，没有语义能力，只有字面 n-gram 相似；碰撞严重、跨语言归零

### 1.6 线程池：`AsyncConfig` 真实值（⚠️ 这里是"反高潮"答案）

`AsyncConfig.java` 全文 9 行，**只有 `@EnableAsync`，没有定义一个线程池、没有一个数值**：

```java
@Configuration
@EnableAsync
public class AsyncConfig {}
```

`SchedulingConfig.java` 同样只有 `@EnableScheduling`。全仓库 `grep` 结果：**没有 `ThreadPoolTaskExecutor`、没有 `Executor` 的 `@Bean`、没有 `spring.task.execution.*` 配置**。

所以"真实值"要分两层讲：

| 层 | 事实 |
|---|---|
| 代码显式配置 | **无**（0 个自定义池，0 个数值，0 个拒绝策略） |
| 生效值（`框架默认`，Spring Boot 3.5.0 `TaskExecutionAutoConfiguration` → `applicationTaskExecutor`） | core **8**、max **`Integer.MAX_VALUE`**、queue **`Integer.MAX_VALUE`**、keepAlive **60 s**、`allowCoreThreadTimeOut=false`、线程名前缀 **`task-`**、拒绝策略 `ThreadPoolTaskExecutor` 默认 **AbortPolicy** |
| 风险 | max 与 queue 都是无界 → **AbortPolicy 永远不触发**，实际等价"无界队列+可无限建线程"，高峰是**OOM / 排队延迟不可控**，而不是拒绝 |
| 调度线程池 | `@Scheduled` 用 Boot 单线程调度器（`spring.task.scheduling.pool.size` 默认 **1**），与业务池隔离 |

**哪些操作丢线程池：**

| 操作 | 线程池 | 证据 |
|---|---|---|
| `MemoryManager.updateProfile`（每次 `/chat` 一次，内含一次 LLM + 一次全量 JSON 写盘） | `@Async` → Boot 默认 `applicationTaskExecutor` | `MemoryManager.java:72` |
| 多 Agent 并行 `runParallel` | `CompletableFuture.supplyAsync(...)` **不给 executor** → `ForkJoinPool.commonPool`（并行度 = CPU 核数 − 1） | `AgentOrchestrator.java:122-124` |
| 查询改写后的多路召回 `searchWithRewrite` | 同样 `supplyAsync` 无 executor → 同一个 `commonPool` | `KnowledgeToolManager.java:48-50` |
| 监控采集 | `@Scheduled` 单线程调度器 | `PerformanceMonitor.java:51` |

一句话："本项目**有一个线程池意识，但没有一个线程池配置**；真正并行的两处还借了 ForkJoin commonPool，与 Spring 池完全隔离，无法统一限流/观测。"

---

## 2. 监控真实实现（`com.pudding.monitor`）

### 2.1 采集的指标（名字全抄）

`PerformanceMonitor` 构造函数里只注册 **3 个自定义 Micrometer 指标**（`PerformanceMonitor.java:41-43`）：

| 指标名 | 类型 | 语义 |
|---|---|---|
| `pudding_agent_success_rate` | **Gauge** | 全池**最低**成功率（`agentSuccessRate`，初值 1.0）；**无 tag**，不能按 agent 下钻 |
| `pudding_monitor_collections_total` | **Counter** | 采集轮次累计 |
| `pudding_monitor_collection_duration` | **Timer** | 单轮 `collect()` 耗时（Micrometer base unit = 秒） |

其余指标来自 Spring Boot Actuator 自动装配（`pom.xml` 引了 `spring-boot-starter-actuator` + `micrometer-registry-prometheus`），`application.yml:37-45` 暴露 `health,info,prometheus,metrics`，并开启 `health.probes`。业务侧"自定义业务指标"就上面 3 个——Agent 成功率、P95、token 用量**都没有做成 metric**，只在 `/monitor` 的 JSON 里（每次请求现算）。

### 2.2 采集频率

- **`@Scheduled(fixedDelay = 10000)` → 每 10 秒一次**（`PerformanceMonitor.java:51`）。`fixedDelay` 不是 `fixedRate`：上一轮结束再等 10 s，天然防重入。
- 另有 `@PostConstruct init() → collect()`，**启动即采一次**（`PerformanceMonitor.java:46-49`）。
- **全仓库没有任何 cron 表达式**（`grep @Scheduled|fixedDelay|fixedRate|cron` 只有上面这一处）。要准备被问"cron 在哪"——答案是**没有，是 fixedDelay=10000ms**。
- 外部抓取侧：`config/prometheus.yml` → job `pudding-java-app`，`metrics_path: /metrics`，`scrape_interval: 10s`，target `pudding-java:8080`；global 15 s。`docker-compose.yml` 里 Prometheus 映射 **9091→9090**。

### 2.3 `monitor_penalty` 完整公式（逐步）

阈值（`application.yml:66-69`）：`success-rate-threshold: 0.90`（`MONITOR_SUCCESS_RATE_THRESHOLD`）、`latency-ms-threshold: 3000`（`MONITOR_LATENCY_MS_THRESHOLD`）。

惩罚计算 `routingPenalty(successRate, avgMs)`（`PerformanceMonitor.java:92-101`）——**注意这里 0.90 / 3000 是硬编码字面量，不读配置**：

```
penalty = 0
if (sr < 0.90)      penalty += min(0.5, (0.90 - sr) * 2)      // 系数 2，封顶 0.5 → sr=0.65 触顶
if (avgMs > 3000)   penalty += min(0.4, (avgMs - 3000)/10000) // 每 1000ms 加 0.1，封顶 0.4 → avg=7000ms 触顶
return min(0.9, penalty)                                      // 总分封顶 0.9
```

端点自检：`sr=0.50 → +0.5`（触顶）；`sr=0.80 → +0.2`；`avg=4000 → +0.1`；`avg=6000 → +0.3`；`avg=12000 → +0.4`（触顶）；`sr=0.60 且 avg=8000 → 0.5+0.4=0.9`（双重触顶）。

**怎么回写编排器（完整闭环）：**

1. `collect()` 读 `orchestrator.stats()`，每个 agent 算 penalty，塞进 `Map<String,Double> penalties`，key = `"{agentType}_{index}"`（如 `technical_0`）
2. `orchestrator.updateRoutingPenalties(penalties)`（`AgentOrchestrator.java:446-452`）→ 逐个 `stats().setMonitorPenalty(...)`，`AgentStats.setMonitorPenalty` 再 **clamp 到 [0, 0.9]**
3. 路由分：`routingScore = (successRate*0.7 + latencyScore*0.3) * max(0, 1 - monitorPenalty)`，其中 `latencyScore = 1/(1 + avgMs/1000)`（`AgentStats.java:34-38`）
4. 生效点：`bestAgent(agentType)` = 同类型实例里 `routingScore` 最大者（`AgentOrchestrator.java:423-426`）

⚠️ **必须自己先说破的两个真相**（否则会被追着打）：
- 惩罚**只影响"同类型里选哪个实例"**；`routeDecision()` 选**类型**时只看 `domainScores()`，完全不看 penalty。
- `AgentConfig` 每种类型只放了 **1 个实例**（`List.of(new GeneralAgent(...))` 等）→ **当前 penalty 在路由上不产生任何实际差异**，属于"埋好通路、等扩容生效"。
- 惩罚阈值（0.90/3000）与告警阈值（读配置）**双份维护**，改 yml 只影响告警、不影响惩罚——这是个真实缺陷，面试官问"阈值能不能配"时别说"能配"。

### 2.4 告警 webhook 触发条件

- 触发点只有两处 `addAlert`（`PerformanceMonitor.java:66-71`）：
  - `sr < success_rate_threshold`（默认 0.90）→ key `agent_success_rate:{type}_{i}`
  - `avg > latency_ms_threshold`（默认 3000）→ key `agent_avg_ms:{type}_{i}`
- 去重：`alertKeys.add(metricKey)` 成功才发 → **同一 key 进程生命周期内只发一次**；列表里写死 `"resolved": false`，**没有恢复/摘除逻辑**，也没有恢复通知。
- 发送：`sendWebhook()` → `restClient.post().uri(url).body(alert).retrieve().toBodilessEntity()`；`url` 取 `pudding.monitor.webhook-url`（`ALERT_WEBHOOK_URL`，**默认空字符串 → 直接 return，不发**）；`RestClient.create()` 无自定义超时/重试；**异常 `catch (Exception ignored)` 全吞**，只打 warning 都没打。
- 附带的"优化建议"：`addSuggestion("路由权重已根据在线表现调整", …, priority=7)`，按 title 去重 → 进程内只出现一次（`PerformanceMonitor.java:77-79`）。
- `/monitor` 返回 `agent_stats / tool_stats / active_alerts(tail 10) / suggestions(tail 5)`（`PerformanceMonitor.java:83-90`），`alerts` 列表本身**无上限**。

---

## 3. 评测真实实现（`com.pudding.evaluation`）

### 3.1 `/eval/run` 完整流程与默认用例

入口 `PuddingController.eval()`（`PuddingController.java:244-248`）：`POST /eval/run`，`@RequestBody(required=false)` → body 可为空对象/空，直接 `evaluator.run(request)`。

`EndToEndEvaluator.run()`（`EndToEndEvaluator.java:41-117`）：

1. 用例选择：`request == null || request.intentCases() == null` → **内置默认 8 条意图用例**；`dialogCases()` 同理 → **内置默认 4 条对话用例**
2. **意图评测**：逐条 `intentRecognizer.recognize(message, null)`（**history 传 null**）
3. **对话评测**：逐条 case → 逐 turn `orchestrator.run(AgentRequest.of(turn, userId, convId, history.toString(), history))` → 立刻 `judge.judge(turn, response, history.toString())` → 追加历史
4. 汇总：`pass_rate / total / passed / avg_scores / per_class / regressions / recommendations / results`
5. `saveBaseline(report)` —— **写完才返回**（`EndToEndEvaluator.java:115`）

**默认用例从哪来**：全部**硬编码在 `EndToEndEvaluator` 的私有方法里**，不读文件、不读 DB、不读环境变量。

- `defaultIntentCases()`（8 条，`L119-130`）：`我的订单什么时候到？→query`、`帮我取消订单→request`、`你们服务太差了！→complaint`、`应用一直报500错误→technical`、`为什么扣了两次款？→billing`、`我要投诉，转人工！→escalation`、`你好→greeting`、`修改我的邮箱地址→account`
- `defaultDialogCases()`（4 条，`L132-139`）：3 条单轮（订单超时 / 登录 401 / 多扣 50 元）+ 1 条三轮（`["你好，我想退款","订单号是 #12345","退款多久能到账？"]`）
- 用例规模：**8 + 6 = 14 条 result**（对话第 4 条 3 轮 → 3 条 result）；`total=14`
- LLM 调用次数（估算，每条）：意图用例 **1 次**（识别）；对话每轮 **3 次**（意图识别 0.1/256 + Agent 回答 0.2/1024 + Judge 0.0/256）→ 默认用例约 **8 + 6×3 = 26 次** LLM 调用，串行

### 3.2 意图评测：Accuracy / Macro-F1 / 逐类 P/R/F1

- **Accuracy**：`intentCorrect / intentCases.size()`，其中 `passed = predicted.equals(c.expectedIntent())`，`predicted = result.intent().name().toLowerCase(Locale.ROOT)`（`L52-60, L99`）
  - ⚠️ **`expectedIntent` 没有 lowercase**（对比 `predicted` 小写了）→ 调用方传 `"TECHNICAL"` 永远判错；默认用例全小写所以看不出。
- **逐类 P/R/F1**：`perClassMetrics(predictions, groundTruth)`（`L150-172`）
  - label 集合 = `predictions ∪ groundTruth`（`HashSet` 合并）
  - 对每个 label 线性扫全样本：`tp`（预测=label 且真值=label）、`fp`（预测=label 且真值≠label）、`fn`（预测≠label 且真值=label）
  - `precision = tp/(tp+fp)`（分母 0 → 0.0）、`recall = tp/(tp+fn)`、`f1 = 2PR/(P+R)`（P+R=0 → 0.0）
  - 全部 `round()` 到**小数点后 4 位**（`Math.round(v*10000)/10000.0`，`L141-143`）
- **Macro-F1**：`perClass.values().map(f1).average()`（`L145-148`），即**未加权平均**，包含只在预测里出现的类（其 f1 通常 0）→ 预测越"细粒度"，宏 F1 越被拉低
- **逻辑推断（未实跑，标明推断）**：意图识别 `vote()` 里有一条"通用意图 → 细粒度意图"的修正规则（`IntentRecognizer.java:240-246`，通用类 + 具体 pattern 命中 ≥0.5 + 当前分 <0.8 时改判细粒度），而默认用例的 `expected` 用的是**粗粒度**（`technical`、`escalation`），所以 `应用一直报500错误`（pattern 命中 `500`+`报错` → `technical_crash`）和 `我要投诉，转人工！`（命中 `转人工` → `human_handoff`）**大概率被判错**——这是"prompt 要求细粒度 / 用例标注粗粒度"的真实口径冲突，讲成"已知口径不一致 + 我会把 expected 改成组内归一化（`intentGroup`）比对"是加分项。

### 3.3 LLM-as-Judge：四维、范围、prompt 约束、防乱给分

- 四维（`QualityScores`）：**relevance（相关性）、accuracy（准确性）、completeness（完整性）、helpfulness（有用性）**
- 范围 **0.0–1.0**；`overall = (r+a+c+h)/4.0`（**简单等权平均**，无权重，`QualityScores.java:11-13`）
- judge prompt（`LLMJudge.java:22-30`）关键约束：
  ```
  你是客服质量评估专家。请对以下客服响应进行评分。
  用户问题: %s
  Agent 响应: %s
  背景信息: %s

  从 relevance、accuracy、completeness、helpfulness 四个维度评分，范围 0.0-1.0。
  只返回 JSON，例如 {"relevance":0.9,"accuracy":0.8,"completeness":0.7,"helpfulness":0.85}
  ```
  `llmGateway.chat("", prompt, 0.0, 256)` → **温度 0（贪心、可复现）+ 256 token 上限 + 只准返回 JSON + 给了 few-shot 形状**；解析用首 `{` 到末 `}` 截取（`L33-35`）。
- **防止 judge 乱给分**（真实存在的 4 道闸，别夸大）：
  1. `asDouble` 把值 **clamp 到 [0.0, 1.0]**（`Math.max(0,Math.min(1,..))`），字符串数字也能解析
  2. 字段缺失/非数字 → **回退 0.5**
  3. 解析异常 → 返回**全 0.5 + `judgeFailed=true` + `error=异常信息`**（`L45-47`），异常不冒充真分
  4. `judge_failed` / `judge_error` 显式写进每条 result 的 `metadata`（`L90-91`），可事后筛掉
- **没有做的**（被问"怎么防 judge 偏见"时主动交底）：
  - 没有 **rubric 锚点**（每个分数段的行为描述）→ 维度语义靠模型自行理解
  - 没有**参考答案（reference）**或要点清单对照
  - 没有**多次采样取均值**、没有**换位测试（A/B swap）**、没有**独立 judge 模型**
  - **judge 与被评模型是同一个 `LlmGateway`/同一 provider** → 自偏好（self-preference）+ 同源失败（模型挂了，judge 也挂）
  - judge 失败按 0.5 计入均值且 `0.5 < 0.75` → **判为 fail**（保守但不区分"基础设施故障 vs 质量差"）

### 3.4 Baseline 与回归：存哪、退化阈值、怎么判定

- **路径**：`pudding.eval.baseline-path = ${EVAL_BASELINE_PATH:data/eval/baseline.json}`（`application.yml:71`）；Java 版仓库里 **`data/` 目录当前不存在**，所以首次运行 `detectRegressions` 返回空，`saveBaseline` 会 `Files.createDirectories` 建目录
- **存什么**：`saveBaseline(report)` 把**整份 report**（`pass_rate/total/passed/avg_scores/per_class/regressions/recommendations/results` 全量，含每条答案原文）`writerWithDefaultPrettyPrinter` 写盘（`L210-217`）——**是快照不是瘦身指标**
- **退化阈值**：`delta = (cur - prev)/prev`，`delta < -0.05` 判退化（**相对下降超过 5%**，`L197-201`）；`prev.doubleValue() > 0` 才比（prev=0 跳过，避免除零）
- **比较对象**：`avg_scores` 里的 3 个键 —— `intent_accuracy`、`macro_f1`、`dialog_overall`（`L100-104`）
- **调度顺序上的关键事实**：`detectRegressions()` **先于** `saveBaseline()` 执行（`L104 -> L115`），所以
  - **每跑一次都会覆盖 baseline** → 你对比的永远是"上一次运行"，不是"人工认可的黄金基线"
  - 首次运行没有 baseline → `regressions=[]`，`recommendations` 只看 `intent_accuracy<0.9`
  - CI 里连跑两次，第二次若下滑 5% 就会报，但**报完就把坏结果存成新 baseline** → 退化只报一次，下一次自动"洗白"。这是这套 baseline 最该被挑战的地方。
- **recommendations 规则**（`L219-231`）：`intent_accuracy < 0.9` → "补充低准确率意图类别的样本和 Few-shot 示例"；`regressions` 非空 → "发现评测回归…检查最近 prompt 或检索逻辑变更"；都无 → "所有指标均达标"
- **通过线**：对话单条 `scores.overall() >= 0.75` 才算 passed（`L78`，硬编码）；`pass_rate = passed/14`（意图+对话混合）

---

## 4. 模型接入（`com.pudding.llm` + `application.yml` 多 profile）

### 4.1 Spring AI 怎么选 provider

**用 Spring profile 做"编译期二选一"，不是运行时路由：**

| profile | 保留的 AutoConfiguration | 排除的 AutoConfiguration | 关键配置 |
|---|---|---|---|
| `anthropic`（默认，`SPRING_PROFILES_ACTIVE:anthropic`） | `AnthropicChatAutoConfiguration` | `DeepSeekChatAutoConfiguration`、`ChromaVectorStoreAutoConfiguration` | `model: claude-3-5-sonnet-20241022`（`ANTHROPIC_MODEL`）、temperature 0.2、max-tokens 1024、`base-url: https://api.anthropic.com` |
| `deepseek` | `DeepSeekChatAutoConfiguration` | `AnthropicChatAutoConfiguration`、`ChromaVectorStoreAutoConfiguration` | `model: deepseek-chat`（`DEEPSEEK_MODEL`）、temperature `${DEEPSEEK_TEMPERATURE:0.2}`、max-tokens 1024、`base-url: https://api.deepseek.com` |

- 每个 profile 只剩**一个 `ChatModel` bean**，`SpringAiLlmGateway` 用 `ObjectProvider<ChatModel>.getIfAvailable()` 拿这个唯一实例（`SpringAiLlmGateway.java:31-40`）——**没有 `@Primary`、没有 provider 列表、没有运行时权重**。
- 切换方式：`SPRING_PROFILES_ACTIVE=deepseek` / `--spring.profiles.active=deepseek`（`docker-compose.yml:52` 也透传该变量）。
- **两处都要讲的连带事实**：`ChromaVectorStoreAutoConfiguration` 在**两个 profile 里都被排除** → `VectorStore` bean 不存在 → `KnowledgeBaseService` 的 `ObjectProvider<VectorStore>` 拿到 `null`，Chroma 写入是 best-effort 空转（`KnowledgeBaseService.java:43-46,135-143`）。**Java 版真实检索是本地 BM25 + 特征哈希向量加权**（`bm25Weight 0.45 / vectorWeight 0.55`），不是 Chroma。别在面试里说"用 Chroma 做向量库"。
- 另注：`pudding.rag.top-k = 4` 是**死配置**——全仓库没有任何 `getTopK()` 调用；`/chat` 硬编码 `searchWithRewrite(message, 3)`，`/search` 默认 `topK=5`。

### 4.2 失败 fallback 到底做了什么

`SpringAiLlmGateway.chat()`（`L43-72`）失败路径：

1. `chatModel == null` → 抛 `IllegalStateException("Spring AI ChatModel is not configured")`
2. `modelCredentialsConfigured == false` → 抛 `IllegalStateException("No real API key configured for active LLM profile: …")`
   - 判定逻辑（`L92-103`）：`profile` 含 `deepseek` 就查 deepseek key，否则查 anthropic key；key 空白 或 等于 `pudding-local-placeholder`（`LLM_STARTUP_API_KEY` 占位符）→ **视为未配置**
3. 任一异常进 `catch`：`properties.getLlm().isFallbackEnabled()` 为 false → **原样抛出**；为 true（默认，`LLM_FALLBACK_ENABLED:true`）→ `log.warn` + `return fallback(userPrompt)`
4. `fallback()`（`L74-86`）= **关键词命中的中文静态话术**，按顺序匹配：
   - 含 `退款` / `refund` → "退款通常需要先提交申请并等待审核。请提供订单号…"
   - 含 `报错` / `登录` / `error` → "先确认账号状态、网络环境和错误码…"
   - 含 `扣款` / `账单` / `发票` → "账单问题需要核对支付记录、订单号和扣款时间…"
   - 默认 → "我已收到你的问题。当前模型服务不可用，系统返回了本地降级回复；请稍后重试或转人工处理。"

**必须说破**：这**不是 provider 互备**（不会 anthropic 失败就切 deepseek），是**静态兜底话术**；没有重试、没有熔断、没有超时配置、没有降级指标打点（只在日志里 `warn`）。代价是"服务可用但答案质量骤降且不易被指标发现"——评测里 judge 会给 0.5 / parse fail，反而能暴露。

---

## 5. 接口与可观测

### 5.1 `/chat` 处理顺序（从 controller 到返回，逐行）

入口 `PuddingController.chat()`（`PuddingController.java:96-159`），`@Valid @RequestBody ChatRequest`：

| # | 步骤 | 细节 |
|---|---|---|
| 1 | 规范化 ID | `userId = userIdOrDefault()` → 空为 `"anonymous"`；`conversationId` 空则 `UUID.randomUUID().toString()`；`requestId = UUID 前 8 位`（`L99-103`） |
| 2 | 读记忆 | `memoryManager.getContext(userId, convId, message)` → Redis `LRANGE 0..19` + 情节检索 top5 + 画像 + summary（4 次读取，1 次 O(n) 余弦扫描） |
| 3 | 拼记忆文本 | `memoryContext.toPromptText(objectMapper)` → 4 段（摘要/相关历史≤3/画像/最近对话≤20） |
| 4 | 取 history | 最近 **5** 条转 `List<Map<role,content>>`，供意图识别与 Agent |
| 5 | 意图识别 | `intentRecognizer.recognize(message, history)`：LLM 0.70 / 字面 Jaccard 0.20 / 关键词 pattern 0.10 加权投票 + 置信度阈值 0.5 + 通用→细粒度修正；结果有 **进程内缓存**（key = 消息小写 + 最近 3 条 history，>1000 条清空） |
| 6 | 是否查知识库 | `shouldUseKnowledge(intent)` 白名单：QUERY/COMPLAINT/REQUEST/TECHNICAL/BILLING/ACCOUNT/ORDER_STATUS/LOGISTICS/REFUND/INVOICE/PAYMENT_ISSUE/ACCOUNT_SECURITY/TECHNICAL_LOGIN/TECHNICAL_CRASH → true；GREETING/ESCALATION/HUMAN_HANDOFF/FEEDBACK/OTHER → false |
| 7 | 调工具 | `knowledgeToolManager.searchWithRewrite(message, 3)`：LLM 改写成 3 个子查询（temp 0.3）/ 缓存 300 s / `recallK = max(3,5) = 5` / 并行召回（commonPool，`get(30s)`）/ LLM rerank 截 3 / 熔断器 5 次失败 60 s / 任何异常都返回"成功但降级"的结果（`success=true` + fallback 文档） |
| 8 | 组装上下文 | `memoryText + "\n\n" + knowledgeText`；知识段带"[知识库检索结果]"编号 + 相关度 + "请优先依据以上知识库内容回答…" |
| 9 | 编排 | `orchestrator.run(agentRequest, [knowledgeTrace])`：必要时内部再识别一次意图（`/chat` 已带 intent，跳过）→ 澄清判断 → 路由决策 → 单 Agent 或并行多 Agent → `recordTrace` |
| 10 | 答案校验 | `answerVerifier.verify(message, response, fullContext)`：一次 LLM（temp 0）返回 `{pass, grounded, need_escalation, reason}`；解析失败兜底 `pass=true, grounded=context非空, need_escalation=答案含"转人工"` |
| 11 | 升级判定 | `escalated = result.escalated() \|\| verification.needEscalation()` |
| 12 | 写记忆 | `addMessage(USER)` → `addMessage(ASSISTANT)`（第 15 条触发同步压缩） |
| 13 | 异步画像 | `updateProfile(...)`（`@Async`，立刻返回） |
| 14 | 返回 | `ChatResponse` 20 字段（`conv_id/request_id/response/intent/intent_group/agent_type/agent_types/primary_agent/supporting_agents/routing_reason/routing_confidence/escalated/latency_ms/knowledge_used/verified/grounded/entities/intent_confidence/intent_source_scores`）——Jackson `SNAKE_CASE` 全局生效 |

**一次 `/chat` 的 LLM 调用次数（正常路径）**：意图 1 + 查询改写 1 + rerank 1 + Agent 1（多 Agent 时 N）+ 校验 1 = **≥5 次串行**；若恰好触发压缩再 +1，触发画像（另一线程）+1。**延迟口径坑**：返回的 `latencyMs` 是 `OrchestratorResult.latencyMs`，**只含编排段**，不含记忆读取/知识检索/答案校验/写记忆——对外说自己 P95 时别拿这个数当端到端。

### 5.2 trace 怎么存

`RequestTraceStore`（`trace/RequestTraceStore.java`）：

- 结构：`ArrayDeque<RequestToolTrace> traces`（时间序）+ `HashMap<String, RequestToolTrace> byId`（O(1) 查询）
- 容量：**`MAX_SIZE = 200`**，超了 `removeFirst()` 并同步从 `byId` 删除（环形淘汰）
- 并发：`record/find/recent/clear` **全部 `synchronized`**（单锁，200 条量级够用，但高频写会串行）
- **纯内存、重启即失、单实例不共享**（多副本时 `/trace` 打到哪个 Pod 就看哪个 Pod 的 200 条）
- 记录时机：`AgentOrchestrator.recordTrace()`（`L353-371`），**每条请求结束记一次**，`endpoint` 字段**硬编码 `"chat"`**（评测链路也写 `"chat"`，无法区分来源）
- 时间戳：`Instant.now().toString()`（若 trace 自带 timestamp 则保留）

### 5.3 `/trace/tool/{requestId}` 返回什么

`PuddingController.toolTrace()`（`L211-217`）：

```json
{"found": true, "trace": { /* RequestToolTrace 15 字段 */ }}
```
未命中：
```json
{"found": false, "trace": {}}
```

`trace` 的 15 个字段（`trace/RequestToolTrace.java`）：`requestId, timestamp, endpoint, userId, conversationId, intent, intentGroup, agentType, primaryAgent, supportingAgents, toolsUsed, toolCalls, knowledgeUsed, escalated, latencyMs`。

其中 `toolCalls[]` 每条（`ToolCallTrace`）7 字段：`toolName, success, fallbackUsed, cached, reranked, latencyMs, error`。
- `knowledgeUsed` 真实判定：`toolsUsed` 包含 `search_knowledge_base` **或** `knowledge_search`（`AgentOrchestrator.java:367`）
- `fallbackUsed` 在 `toTrace()` 里定义为 `!response.success()`（`L415`）——语义偏"失败"而非"降级"，容易被追问（知识库降级时 `success=true`，这里 fallbackUsed=false，**降级不可见**）
- 另有 `GET /trace/tools?limit=20` → `{"items":[...]}`，`recent()` 返回**倒序**（最新在前，`RequestTraceStore.java:73`）

### 5.4 其他可观测面

- `GET /health`：自写（非 actuator），`{"status":"ok","agents":<orchestrator.stats()>}`（含每个 agent 的 `total/success_rate/avg_ms/monitor_penalty/routing_score`）
- `GET /metrics`：`prometheusMeterRegistry.scrape()`，`text/plain; version=0.0.4`
- `GET /monitor`：见 2.4
- Actuator：`/actuator/{health,info,prometheus,metrics}`（`management.endpoints.web.exposure.include`）
- `GET /docs`：**手写 HTML** 引 CDN `swagger-ui-dist@5` 指向 `/v3/api-docs`——因为 `pom.xml` 只引了 `springdoc-openapi-starter-webmvc-api`（**无 swagger-ui 静态资源**），所以自己搭了个壳
- `docker-compose.yml` 事实：redis `6380:6379`、chromadb `8002:8000`（镜像 `chromadb/chroma:0.5.23`）、app `8080:8080`、prometheus `9091:9090`、nginx `8081:80`；redis 启动带 `--appendonly yes --requirepass pudding123`；app `depends_on` 用 `service_healthy`，nginx/prometheus 用 `service_started`；app healthcheck `curl -sf http://localhost:8080/health`（`start_period: 90s`）

---

## 6. "为什么这么设计"：替代方案 + 当前代价

| 决策点 | 当前方案 | 替代方案 | 当前方案的代价（要自己先讲） |
|---|---|---|---|
| 工作记忆存储 | Redis List + 24h TTL | 全量 DB 落库、或纯内存 | 部署依赖 Redis；key 未转义；**多实例共享工作记忆但画像/情节记忆不共享** → 记忆是"半分布式" |
| 压缩策略 | 按**条数**15 触发、留 5 | 按 token 预算 / 按时间窗 / 摘要+原始混合 | 一条 2000 字消息和一条"你好"等价计 1 条 → **条数≠信息量**；同步压缩让**每 10 条消息就有一次抖动**（LLM 调用在请求线程内） |
| 压缩后语义连续性 | 只留最近 5 条 + 追加摘要 | 分层摘要（滚动摘要 + 关键事实表） | 早期细节只活在 prose 摘要里，**不可检索、不可回溯**；摘要追加无上限 → `summary:*` 会无限增长到 TTL |
| 画像生成 | 每次 `/chat` 一次异步 LLM，取最近 10 条 | 批量/定时（离线）/变更检测后才更新 | 每轮多 1 次 LLM 成本；10 条窗口**无跨轮累积**，画像会"遗忘"；异步无边界的 8 线程池并发写同一个 JSON 文件 |
| 情节记忆检索 | 256 维特征哈希 + 余弦，全量 O(n) 扫描 | 真 embedding + ANN（pgvector/Chroma/Milvus） | 无语义能力（同义改写即失配）、O(n) 随用户量线性劣化、**全量驻留 JVM 堆**；换真向量库的成本是真的没做，别硬吹 |
| 持久化 | 单 JSON 全量重写（`synchronized`） | SQLite/DB/对象存储、追加日志 | 每次写入 O(全量)；**写放大 + 单点文件锁**；多实例会互相覆盖（last-write-wins） |
| 线程池 | 不配置，用 Boot 默认 | 显式有界池 + 有界队列 + 拒绝策略 + 埋点 | 无界 → 压力下 OOM；`@Async` 与 `commonPool` 割裂，**无法统一限流/观测** |
| 监控放进程内 | `@Scheduled` 10s 自采 + 惩罚回写 | 外置采集（Prometheus 侧 recording rules / 独立服务） | 优点：**零额外依赖、能反向改路由**（闭环）；代价：**采集与业务抢同一 JVM**、无持久化（重启丢失告警去重状态与 alerts 列表）、多副本各算各的、**无告警恢复语义** |
| 在线惩罚 vs 离线调参 | 运行期 10s 用成功率/延迟改路由分 | 离线 A/B + 定期发布权重 | 简单、可热更新；但**硬编码阈值 + 单实例 Agent → 实际零影响**，且有"自激振荡"风险（惩罚→少接流量→样本变少→抖动） |
| 评测用 LLM-as-Judge | 同模型打分 + 人工缺位 | 纯人工标注 / 人工+LLM 双评 / 规则断言 | **纯人工**：一致但不可持续（每天几十条 × 4 维，成本高、滞后、无法进 CI）；**纯规则**：只能测"是否含订单号/是否转人工"，测不了"答得好不好"；LLM judge 换来"可进 CI 的规模化质量信号"，代价是**自偏好、无 rubric、同源故障、0.5 兜底污染均值** |
| baseline | 每次运行覆盖自身 | 版本化黄金集 + 只允许人工批准更新 | 覆盖式 baseline 能跑但**退化只报一次**；且无标签/无版本 → 无法回答"相对上个 release 如何" |
| 评测与线上指标同构 | 评测走 orchestrator（不经 Controller 的校验/记忆） | 评测走完整 `/chat` 端点 | 现在**评测不覆盖 AnswerVerifier、不覆盖记忆压缩、不覆盖画像** → 线上/评测信号不一致（这是最容易被问倒的点） |

---

## 7. 面试追问清单（含 30 秒答案 + 深挖应对）

**Q1（基础）"你记忆是怎么做的？"**
> 30s：Redis 两把 key，`wm:{user}:{conv}` 是 List 存最近 20 条消息 JSON，`summary:{user}:{conv}` 存滚动摘要，都 24h TTL 且每次写刷新；攒到 15 条就同步压缩：留最近 5 条、把其余交给 LLM 用"2-3 句话"总结、摘要追加进 summary key，再回填上下文；画像和情节记忆走进程内 + 一个 JSON 文件。
> 深挖：问"为什么 15/20 差 5"→ 答"15 是触发、20 是读取上限、5 是保留量，触发后只留 5，所以实际每 10 条触发一次；这三个数是我拍的，没做过 token 预算校准"。

**Q2 "压缩为什么放请求线程里？不卡吗？"**
> 30s：确实卡——`compress()` 没有 `@Async`，它是 `addMessage()` 内同步调用的，每 10 条消息会多一次 LLM（256 token）延迟 + 一次全量写盘。当时考虑的是"压缩必须原子，异步会读到旧窗口"；代价是那一跳尾延迟。
> 深挖：正确做法是"双写窗口 + 后台压缩 + 版本号 CAS"或"先把 list 截断到空，再异步补摘要"，并给压缩单独一个单线程池（保证同一会话串行）。

**Q3 "线程池多大？拒绝策略是什么？"（钓鱼题）**
> 30s：`AsyncConfig` 只有 `@EnableAsync`，**没有任何数值**，实际用的是 Spring Boot 3.5 的默认 `applicationTaskExecutor`：core 8 / max `Integer.MAX_VALUE` / queue `Integer.MAX_VALUE` / keepAlive 60s / 前缀 `task-` / AbortPolicy 但因为无界所以永不触发。真正并行的两处（多 Agent、多路召回）还是 `CompletableFuture.supplyAsync` 没传 executor，直接落到 ForkJoin commonPool。
> 深挖：坦白这是欠账，给出"显式 8/16/队列 200 + CallerRuns 或自有拒绝策略 + 关键指标（队列长度、拒绝数、耗时）"的改法；并解释 CallerRuns 在网关场景的背压价值。

**Q4 "monitor_penalty 怎么算的？真生效吗？"（最刁钻之一）**
> 30s：`sr<0.9` 时加 `min(0.5,(0.9-sr)*2)`，`avg>3000ms` 时加 `min(0.4,(avg-3000)/10000)`，总和封顶 0.9；回写到 `AgentStats.monitorPenalty`，进 `routingScore = (0.7*sr + 0.3/(1+avg/1000)) * (1-penalty)`。
> 深挖（必须自曝）：**它只影响"同类型里挑实例"，不影响"选哪个类型"；而每种类型只有 1 个实例，所以当前对路由结果零影响**。要真生效得（a）给同一类型多实例，（b）把 penalty 融进 `domainScores` 的类型选择，（c）把硬编码 0.9/3000 换成读配置（现在和告警阈值是两份）。

**Q5 "LLM-as-Judge 怎么保证不瞎给分？"（最刁钻之二）**
> 30s：四维 relevance/accuracy/completeness/helpfulness，0-1 分，温度 0、只准输出 JSON、给示例；解析时 clamp 到 [0,1]、缺字段回退 0.5、异常标 `judge_failed=true` 并把 error 写进 metadata，便于剔除。overall 是四维等权平均，≥0.75 算通过。
> 深挖：主动交底"没有 rubric 锚点、没有参考答案、没有多次采样/换位、judge 和被评模型同源"→ 给出改进路线：每条维度写清 0.2/0.5/0.8 的行为锚点 + 引入要点清单做 grounded 校验 + 3 次采样取均值/中位数 + 抽 5% 人工双评算与 judge 的 Spearman 相关（做 judge 校准），以及"judge 失败不应计入均值，应单列 infrastructure failure 率"。

**Q6 "评测怎么防回归？baseline 存哪？"**
> 30s：`data/eval/baseline.json`，存整份 report；每次跑先比 `avg_scores` 的 `intent_accuracy/macro_f1/dialog_overall`，相对下降超过 5% 记 regression，然后**用本次结果覆盖 baseline**。
> 深挖（自曝）：覆盖式 baseline 有两个后果——首次无基线不报、报完即洗白；正确做法是 baseline 版本化（按 git commit / release tag 目录），只允许人工 approve 更新，并区分"退化报警"和"基线更新"两个动作。

**Q7 "Anthropic 挂了会切 DeepSeek 吗？"**
> 30s：**不会**。provider 是 Spring profile 二选一（另一个 provider 的 AutoConfiguration 被 exclude），`ObjectProvider<ChatModel>` 只拿到唯一实例；失败后是 `fallback()` 的关键词静态话术，受 `LLM_FALLBACK_ENABLED` 控制，还会识别 `pudding-local-placeholder` 占位 key 直接走兜底。
> 深挖：要做真互备得自己包一层 `RoutingChatModel`：多 bean + 健康探测 + 超时/重试 + 熔断 + 降级打点；并解释"静默降级比报错更危险"（用户拿到模板话术，指标全绿）。

**Q8 "trace 存哪？多副本怎么办？"**
> 30s：进程内 `ArrayDeque` + `HashMap`，`MAX_SIZE=200`，环形淘汰，方法全 `synchronized`；`requestId` 是 UUID 前 8 位；`/trace/tool/{id}` 返回 `{found, trace}`。
> 深挖：缺点明确——重启丢失、多副本不共享、8 位 requestId（32 bit）有碰撞概率（200 条窗口内可控但理论上存在）、`endpoint` 硬编码 chat。改法：写 OTel span / 落 Redis Stream 或 ClickHouse，requestId 换 128bit traceId 并与日志 MDC 打通。

---

## 8. 容易讲错 / 翻车的点（逐条对照代码）

1. **"自定义线程池 core=…"** —— ❌ `AsyncConfig` 一个数字都没有。说 8/MAX/MAX 时必须加定语"Spring Boot 3.5 默认，不是我配的"。
2. **"监控每秒/每分钟采集"** —— ❌ 是 `fixedDelay = 10000`（10 秒），没有 cron。Prometheus 侧才是 15s global / 10s job。
3. **"阈值可配置所以惩罚也变"** —— ❌ `routingPenalty` 里 0.90 / 3000 / 2 / 0.4 / 0.9 全是字面量，只有告警阈值读配置。
4. **"惩罚会影响选哪个 Agent"** —— ❌ 只影响同类型实例选择，而每类型 1 个实例，**当前零影响**。
5. **"告警会恢复/会重发"** —— ❌ `alertKeys` 永久去重、`resolved` 恒 false、无恢复逻辑、webhook 异常被吞。
6. **"用 Chroma 做向量库"** —— ❌ 两个 profile 都把 `ChromaVectorStoreAutoConfiguration` 排除了；真实检索是本地 BM25 0.45 + 特征哈希向量 0.55。`docker-compose` 里起 Chroma 只是"预留/对位 Python 版"。
7. **"RAG top-k 是 4"** —— ❌ `rag.top-k` 是死配置；`/chat` 硬编码 3，`/search` 默认 5。
8. **"记忆都在 Redis"** —— ❌ 画像 + 情节记忆在 JVM 堆 + `memory-store.json`；多副本会不一致。
9. **"上下文里有 20 条 + 摘要"** —— 半对：prompt 里最近对话最多 20 条（workingMax），但给意图识别/Agent 的 history 只有**最近 5 条**；相关历史**检索 5 条、注入 3 条**。
10. **"latency_ms 是端到端"** —— ❌ 只含 orchestrator 编排段，不含记忆、检索、校验、写回。
11. **"意图准确率算的是 14 条里对几条"** —— ❌ accuracy 只在 **8 条意图用例**上算；`pass_rate` 才是 14 条混合；`macro_f1` 是**未加权**宏平均，且 label 集合含"只出现在预测里的类"。
12. **"expectedIntent 大小写无所谓"** —— ❌ `predicted` 小写、`expectedIntent` 原样比较，传大写必错。
13. **"judge 失败会被剔除"** —— ❌ 失败按全 0.5 参与平均，且 0.5 < 0.75 直接判 fail，只留 `judge_failed` 标记。
14. **"baseline 是黄金基线"** —— ❌ 每次运行覆盖。
15. **"评测走的是线上同一条链路"** —— ❌ 评测直接调 `orchestrator.run`，绕过 `/chat` 里的知识库调用、AnswerVerifier、记忆写入/压缩、画像更新。
16. **"Redis 端口 6379 对外"** —— ❌ compose 是 `6380:6379`；Chroma 是 `8002:8000`；Prometheus 是 `9091:9090`。
17. **拿 Python 版 baseline 数字来讲 Java 版** —— ❌ `布丁智服/pudding-python/data/eval/baseline.json` 是另一套 schema（`relevance/accuracy/...` + `intent_recognition` 单条 result，`intent_accuracy 0.8182 / macro_f1 0.6923`），Java 版的 `avg_scores` 是 `intent_accuracy / macro_f1 / dialog_overall`。两套不能混引。
18. **"SYSTEM 角色在用"** —— ❌ `MessageRole.SYSTEM` 定义了但全链路未写入；system prompt 走的是 `LlmGateway.chat(systemPrompt, …)` 参数，不进 Redis。

---

## 9. 代码定位（`文件名:方法名`，附行号）

**记忆**
- `MemoryManager.java:getContext` L49-55（4 路读取聚合）
- `MemoryManager.java:addMessage` L57-70（leftPush + expire + **LLEN≥15 → compress**）
- `MemoryManager.java:updateProfile` L72-96（`@Async`，最近 10 条，0.0/512）
- `MemoryManager.java:getWorkingMemory` L98-114（`LRANGE 0..19` + 倒序）
- `MemoryManager.java:compress` L116-149（留 5、摘要 prompt、"对话包含 N 条历史消息"、删 key 回填）
- `MemoryManager.java:searchEpisodic` L151-162（全量 cosine top5）
- `MemoryManager.java:embed` L194-206（256 维 hash n-gram）
- `MemoryManager.java:loadPersistentMemory` / `persistMemory` L220-248 / L250-264
- `MemoryManager.java:wmKey` / `summaryKey` L182-188
- `MemoryContext.java:toPromptText` L15-41（四段拼接、history 只注入 3）
- `ConversationMessage.java` / `MessageRole.java`（record / 三值枚举）

**监控**
- `PerformanceMonitor.java:性能指标注册` L41-43（`pudding_agent_success_rate` / `pudding_monitor_collections_total` / `pudding_monitor_collection_duration`）
- `PerformanceMonitor.java:init` L46-49（启动即采）
- `PerformanceMonitor.java:collect` L51-81（`fixedDelay=10000`）
- `PerformanceMonitor.java:routingPenalty` L92-101（公式）
- `PerformanceMonitor.java:addAlert` / `sendWebhook` L103-109 / L117-126
- `PerformanceMonitor.java:summary` L83-90（`/monitor` 载荷）
- `AgentStats.java:routingScore` / `setMonitorPenalty` L34-38 / L44-46
- `AgentOrchestrator.java:stats` / `updateRoutingPenalties` / `bestAgent` L428-443 / L446-452 / L423-426

**编排（回答第 5 节要引用）**
- `AgentOrchestrator.java:run` L54-109、`runParallel` L119-156、`routeDecision` L169-211、`domainScores` L213-273、`recordTrace` L353-371、`execute` L341-351

**评测**
- `EndToEndEvaluator.java:run` L41-117（默认用例兜底、双阶段、`saveBaseline` 在最后）
- `EndToEndEvaluator.java:defaultIntentCases` / `defaultDialogCases` L119-130 / L132-139
- `EndToEndEvaluator.java:perClassMetrics` L150-172、`macroF1` L145-148、`round` L141-143
- `EndToEndEvaluator.java:detectRegressions` L185-208（-5%）、`saveBaseline` L210-217、`recommendations` L219-231
- `LLMJudge.java:judge` L21-48、`asDouble` L50-59
- `QualityScores.java:overall` L11-13
- `IntentRecognizer.java:recognize` L89-117、`vote` L216-251、`patternRecognize` L169-198、`semanticRecognize` L154-167、`extractEntities` L263-275

**模型接入**
- `LlmGateway.java`（单方法接口 L5）
- `SpringAiLlmGateway.java:构造器` L31-40（`ObjectProvider<ChatModel>` + 三个 `@Value`）
- `SpringAiLlmGateway.java:chat` L43-72（占位 key 拦截 → catch → fallback 开关）
- `SpringAiLlmGateway.java:fallback` L74-86（关键词话术）
- `SpringAiLlmGateway.java:credentialsConfigured` / `hasRealKey` L92-103

**接口 / 配置 / 可观测**
- `PuddingController.java:chat` L96-159、`shouldUseKnowledge` L250-271、`buildKnowledgeContext` L273-285
- `PuddingController.java:monitor` L205-209、`toolTrace` L211-217、`recentToolTraces` L219-223、`metrics` L238-242、`eval` L244-248
- `RequestTraceStore.java:record` L22-53（`MAX_SIZE=200`）、`find` L55-57、`recent` L59-74（reversed）
- `RequestToolTrace.java`（15 字段）、`ToolCallTrace.java`（7 字段）
- `AsyncConfig.java`（全文 9 行，仅 `@EnableAsync`）、`SchedulingConfig.java`（全文 9 行，仅 `@EnableScheduling`）
- `PuddingProperties.java`（`Memory` L56-84：86400/20/15；`Monitor` L146-174：0.90/3000/webhook；`Eval` L176-186；`Rag` L86-114：4/0.45/0.55；`Storage` L116-144；`Skills` L188-207：5000）
- `AppConfig.java`（`@EnableConfigurationProperties(PuddingProperties.class)`）、`OpenApiConfig.java:openAPI` L15-27（8080/8081 两个 server）
- `application.yml`（profile 分段 L76-103；redis 6380/密码 L14-19；ai 段 L20-35；`pudding.*` L51-74）
- `docker-compose.yml`（redis L2-14、chromadb L16-32、app L34-65、prometheus L67-80、nginx L82-91、volumes L93-97）
- `config/prometheus.yml`（scrape 10s / global 15s / job `pudding-java-app`）
