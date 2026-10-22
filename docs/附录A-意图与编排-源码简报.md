# 布丁智服 · 意图识别 / Agent 编排 / 回答校验 技术简报

> 全部结论逐行来自以下真实代码（只读，未做任何修改）：
> `pudding-java/src/main/java/com/pudding/` 下的 `intent/*`（4 个文件）、`agent/*`（12 个文件），
> 以及被调用链牵进来的 `monitor/PerformanceMonitor.java`、`skill/SkillManager.java`、`config/PuddingProperties.java`、`api/PuddingController.java`、`llm/SpringAiLlmGateway.java`。
> 凡代码里没有的，本文会显式写"代码中未找到"。

---

## 0. 先记住调用链（面试时定位的锚点）

`PuddingController.chat()`（`PuddingController.java:96-159`）：

```
MemoryManager.getContext(userId, convId, message)      → memoryText
IntentRecognizer.recognize(message, history)           → IntentResult      ← 1 次 LLM 调用
shouldUseKnowledge(intent) ? KnowledgeToolManager.searchWithRewrite()
AgentOrchestrator.run(AgentRequest.of(...), knowledgeTrace)  → OrchestratorResult  ← 1 次 LLM 调用（或多 Agent 时 N 次并行）
AnswerVerifier.verify(message, result.response(), fullContext) → VerificationResult ← 1 次 LLM 调用
escalated = result.escalated() || verification.needEscalation()
组装 ChatResponse（intent / agent_type / primary_agent / supporting_agents / routing_reason /
                  routing_confidence / escalated / verified / grounded / intent_source_scores …）
```

关键事实：**一次 `/chat` 串行打三次 LLM**（意图 256 tokens → Agent 1024 tokens → 校验 256 tokens）。
意图识别在 Controller 里已经跑过一次，`AgentOrchestrator.run()` 里 `if (req.intent() == null)` 才会再跑，
而 Controller 传的是非 null 的 `IntentResult`，所以**不会重复调用**；即便有人传 null，`IntentRecognizer` 还有进程内缓存（`IntentRecognizer.java:90-94`）。

---

## 1. 模块一句话职责

| 模块 | 一句话职责 | 真实实现手段 |
|---|---|---|
| **意图识别** `intent/IntentRecognizer` | 把用户一句话判定为 19 个 `IntentCategory` 之一，并附带置信度、紧急度、粗粒度意图组、结构化实体 | 1 次低温 LLM（0.7 权重）+ 字符 n-gram Jaccard（0.2）+ 关键词 contains 规则（0.1）加权投票 |
| **编排** `agent/AgentOrchestrator` | 决定这句话交给哪个 Agent（可多个），并在低置信度时先澄清、在紧急时直接转人工 | 规则化路由表 + 领域加权打分（intent 归类 + 关键词命中数 + 实体命中），支持 `CompletableFuture` 并行派发 |
| **三类 Agent** `GeneralAgent` / `TechnicalAgent` / `BillingAgent` | 各自用不同 system prompt 调 LLM 生成客服回答，并记录自身成功率/延迟用于择优 | 模板方法模式，子类**只覆写** `type()` 和 `systemPrompt()`，`handle()` 全部复用 `BaseAgent` |
| **回答校验** `agent/AnswerVerifier` | 用一次独立 LLM 调用判断回答"是否解决、是否有据、是否要转人工" | 单次 LLM（temperature=0.0，256 tokens）输出 4 个 JSON 字段；**只标注，不改写、不重生成、不拦截** |

---

## 2. 意图识别：真实实现

### 2.1 三路信号是什么（原文级别）

`IntentRecognizer.recognize()`（`IntentRecognizer.java:89-117`）里依次调用三路，**三路互不阻塞、也没有互相剪枝**：

```java
Map<String, Object> llm      = llmRecognize(message, history);
Map<String, Object> semantic = semanticRecognize(message);
Map<String, Object> pattern  = patternRecognize(message);
VoteResult vote = vote(llm, semantic, pattern);
```

#### 路 1：LLM（`llmRecognize`，`IntentRecognizer.java:119-152`）

- 调用：`llmGateway.chat("", prompt, 0.1, 256)` —— **system prompt 为空字符串**，temperature=`0.1`，maxTokens=`256`。
- prompt 关键内容（原文要点）：
  - 角色："你是客服意图分析专家。根据示例判断用户意图，返回 JSON。"
  - 关键一句："**如果用户问题能匹配细粒度业务意图，请优先返回细粒度意图，而不是宽泛大类。** 例如退款优先返回 refund，发票优先返回 invoice，登录故障优先返回 technical_login。"
  - 示例：遍历 `TEMPLATES`，**每个意图只取第 1 条模板**（`samples.getFirst()`），拼成 `消息: "我的订单状态是什么？" -> 意图: query` 这种格式 → 共 **18 条示例**。
  - 最近对话：`history` 整个列表 `toString()` 直接塞进 prompt（`history == null ? "" : history`，是 Java List 的 `toString`，不是 JSON）。
  - 输出契约：`返回格式: {"intent":"technical","confidence":0.9,"reasoning":"一句话说明"}`，可选意图为 `allIntentNames()`（全部枚举小写、逗号分隔）。
- 解析：`sliceJsonObject(raw)` 取**第一个 `{` 到最后一个 `}`**（`IntentRecognizer.java:337-344`），再 Jackson `readValue`；`parseIntent()` 用 `IntentCategory.valueOf(value.toUpperCase())`，失败 → `OTHER`（`:329-335`）。
- 失败兜底：「异常」分支才写 `failed=true`、`intent=OTHER`、`confidence=0.0`、`reasoning="LLM recognition failed"`（`:144-151`）。

> **极重要的真实细节**：`SpringAiLlmGateway` 在 `pudding.llm.fallback-enabled=true`（`PuddingProperties.Llm.fallbackEnabled` 默认 `true`）时**不抛异常**，而是返回一段中文散文兜底（`SpringAiLlmGateway.java:74-86`）。
> 这段散文里没有花括号 → `sliceJsonObject` 返回 `"{}"` → `readValue("{}")` **成功**（空 Map，不抛异常）→ 于是 `failed` 永远不会被置位，`intent` = `parseIntent("null")` = `OTHER`、`confidence=0.0`。
> 也就是说：**"LLM 挂了"这件事在意图识别里表现为"LLM 投了 OTHER 0.0 分"，而不是走 failed 分支。** 这条链路只有 `fallback-enabled=false` 时才会走 `failed=true`。

#### 路 2：字符 n-gram 相似度（`semanticRecognize`，`IntentRecognizer.java:154-167`）

算法（`charNgrams` + `jaccard`，`:346-366`）：

1. `normalize()`：`toLowerCase(Locale.ROOT).trim()`（`:368-370`）。
2. `charNgrams()`：**n 取 1、2、3 全部拼接**（`for (int n = 1; n <= 3; n++)`），即同时生成一元/二元/三元字符组。例："退款多久到账" 会得到 6 个 unigram + 5 个 bigram + 4 个 trigram。
3. `jaccard()`：**先转成 `HashSet`（去重！）**，再 `交集 / 并集`（`intersection / (|a| + |b| - intersection)`），空集返回 `0.0`。
4. 对 `TEMPLATES` 的 **18 个意图 × 每意图 3 条模板 = 54 条样本**逐条算 Jaccard，取**最大值**（严格 `>`，即并列取先遍历到的），返回 `{intent, confidence=bestScore}`，无命中返回 `{OTHER, 0.0}`。

因为去重 + 一元组参与，长文本的 unigram 集合会趋同，相似度会被拉高；这也是它只能当 0.2 权重辅助信号的隐含原因。

#### 路 3：关键词规则（`patternRecognize`，`IntentRecognizer.java:169-198`）

- 匹配方式：**`msg.contains(keyword)` 纯子串包含**，不是正则、不是分词、不是词边界匹配；`msg` 已 `normalize`（小写 + trim）。英文关键词因此等价于小写子串匹配。
- **两级结构，先细后粗**：
  1. 先在 9 个**细粒度**意图的规则表里找（`:171-181`）：`HUMAN_HANDOFF`(转人工/人工客服/找人工)、`ORDER_STATUS`(订单状态/发货了吗/处理到哪/order status)、`LOGISTICS`(物流/快递/配送/运单/delivery/shipping)、`REFUND`(退款/退货/refund/return)、`INVOICE`(发票/抬头/税号/invoice)、`PAYMENT_ISSUE`(重复扣款/多扣/支付失败/扣费/payment failed)、`ACCOUNT_SECURITY`(被盗/异常登录/重置密码/两步验证/安全)、`TECHNICAL_LOGIN`(无法登录/登录失败/401/验证码)、`TECHNICAL_CRASH`(崩溃/闪退/500/报错/crash)。
  2. **只有细粒度全落空（intent 仍为 OTHER）** 才查 8 个**粗粒度**规则表（`:187-196`）：`ESCALATION`(投诉/经理/supervisor)、`COMPLAINT`(太差/糟糕/horrible/等了很久)、`QUERY`(？/?/怎么/什么/status)、`REQUEST`(帮我/需要/please/help)、`GREETING`(你好/嗨/hello/hi)、`BILLING`(退款/扣款/发票/refund)、`TECHNICAL`(崩溃/报错/error/crash)、`ACCOUNT`(密码/邮箱/账户/password)。
- 打分公式（`bestPatternMatch`，`:200-214`）——**这是面试最容易被问到的"小公式"**：

```java
long hits = 该意图关键词列表中被 msg.contains 命中的个数;   // 每个关键词只计 1 次
if (hits > 0) score = Math.min(1.0, 0.5 + 0.25 * (hits - 1));
```

即：**1 个命中 = 0.50，2 个 = 0.75，3 个及以上 = 1.00（封顶）**；取所有意图中的最大 score（严格 `>`，并列取遍历先到者）。

### 2.2 权重、阈值、归一化、置信度合成（真实常量）

`vote()`（`IntentRecognizer.java:216-251`）+ `addScore()`（`:253-257`）：

```
scores[intent] = 0.70 * conf_llm     (若 llm 判为该 intent)
               + 0.20 * conf_semantic
               + 0.10 * conf_pattern
取 scores 中最大值对应的 intent 及其分值
```

- 权重常量：**`0.70` / `0.20` / `0.10`**（`:232-234` 三行 `addScore` 的第三个实参），代码中**没有**抽成配置项、没有注释说明来源、仓库内**没有调参实验记录**（`evaluation/` 里有 `EndToEndEvaluator`，但其中未出现这三组权重）。
- 阈值常量：`private static final double CONFIDENCE_THRESHOLD = 0.5;`（`:25`），用于"低于 0.5 → 判 OTHER"。
- **归一化方式：没有归一化。** 权重和虽然是 1.0，但某一路缺席（不投这个 intent）时**不做重归一**，也就是说分值天然被"未投票的路"压掉。
  - 直接推论（很重要）：**单靠 pattern 最多只能得 `0.10 × 1.0 = 0.10`，单靠 semantic 最多 `0.20`，两者永远过不了 0.5。**
  - 只有 LLM 支路能把分数顶过阈值：单靠 LLM 时 `0.7 × conf ≥ 0.5` ⇒ `conf ≥ 0.714`。
  - 若 semantic 也同意且 `conf_sem=0.6`，则 LLM 只需 `conf ≥ (0.5-0.12)/0.7 ≈ 0.543`。
- `sourceScores` 的 key 是**写死的字符串**：`"llm"`、`"embedding"`、`"pattern"`（`:218-220`），必要时追加 `"refined_by_pattern"`（`:244`）。这个 Map 原样透出到 `ChatResponse.intentSourceScores`（`ChatResponse.java:50`）。
  > ⚠️ key 叫 `embedding`，但这一路**根本不是向量**，是第 2.1 节的字符 n-gram Jaccard。命名与实现不一致，是历史遗留。

**置信度的准确语义**：它是"该意图在所有路加权得分之和"，**不是概率**，不 sum to 1，也不是 softmax。三个候选意图的分值是独立累加的，没做归一（代码中未找到 softmax / 归一化逻辑）。

### 2.3 两个"隐藏规则"：refine 与 <0.5 降级

`vote()` 的三条分支必须背下来（`:222-250`）：

1. **`llm.get("failed") == TRUE`**（只在异常路径触发）：
   - semantic 非 `OTHER` 且 `conf > 0` → 直接返回 semantic 的 intent + **它自己的原始 confidence**（不做加权！）；
   - 否则 pattern 非 `OTHER` 且 `conf > 0` → 同理想返回 pattern；
   - 否则 `OTHER, 0.0`。
   > 注意：这条分支下返回的置信度是**单路原始分**，与正常分支的量纲不同（0.5/0.75/1.0 或 Jaccard 值）。

2. **细粒度纠偏（refine）**（`:240-246`）：同时满足四个条件时，**用 pattern 的细粒度意图覆盖投票结果**：
   - `GENERIC_INTENTS.contains(best.intent)`，`GENERIC_INTENTS = {QUERY, BILLING, TECHNICAL, ACCOUNT, ESCALATION}`（`:60-66`）
   - `SPECIFIC_INTENTS.contains(pattern.intent)`，`SPECIFIC_INTENTS = {ORDER_STATUS, LOGISTICS, REFUND, INVOICE, PAYMENT_ISSUE, ACCOUNT_SECURITY, TECHNICAL_LOGIN, TECHNICAL_CRASH, HUMAN_HANDOFF}`（`:48-58`）
   - `patternConf >= 0.5`（即 pattern 至少命中 1 个关键词）
   - **`best.score < 0.8`**
   命中后：`意图 = pattern.intent`，`置信度 = max(best.score, patternConf)`，并写 `sourceScores["refined_by_pattern"] = patternConf`。
   > **这是全模块最锋利的一处取舍**：因为 `0.7 × 任何 confidence ≤ 0.7 < 0.8`，只要 LLM 单独给出粗粒度意图，`best < 0.8` 几乎恒成立 ⇒
   > **只要用户句子里出现一个细粒度关键词，结果基本都会被关键词覆盖成细粒度意图**（例如 LLM 说 billing 0.95 → 0.665，句中有"退款" → 直接变成 refund 0.665）。
   > 好处：`IntentGroup` 和下游路由能拿到细粒度；风险：关键词与语义冲突时（"我不需要退款，我要开发票"）取的是 map 遍历先命中的那个，**有不确定性**（`Map.ofEntries` 的迭代顺序未定义）。

3. **低于阈值**（`:247-249`）：`best.score < 0.5` → 返回 `OTHER`，**但置信度保留原分值**（不是 0.0）。所以 `intent_confidence` 可能是 0.35、0.42 这种"OTHER 但仍有一定分"的值。

### 2.4 低置信度澄清：触发条件在两处，字段不同

- **意图层（不澄清，只标记）**：`vote()` 把 <0.5 的判成 `OTHER`（`IntentResult.intent = OTHER`, `confidence = 原分值`）。
- **编排层（真正产澄清话术）**：`AgentOrchestrator.needsClarification()`（`AgentOrchestrator.java:330-339`）：

```java
req.intent() == IntentCategory.OTHER          // 必须是 OTHER
&& req.message().trim().length() > 2          // 超过 2 个字符（"嗯""哦"不澄清）
&& req.intentConfidence() < 0.5               // 置信度严格小于 0.5
```

澄清时返回的 `OrchestratorResult`（`AgentOrchestrator.java:62-79`）字段固定为：

| 字段 | 值 |
|---|---|
| `response` | `"我还不能确定您要处理的是哪类问题。请补充一下是订单物流、退款账单、账户资料，还是技术故障？"`（硬编码） |
| `agentType` | `GENERAL` |
| `agentTypes` | `[GENERAL]` |
| `primaryAgent` | `GENERAL` |
| `supportingAgents` | `[]` |
| `escalated` | `false` |
| `routingReason` | `"低置信度 OTHER 意图，先澄清用户需求"` |
| `routingConfidence` | `req.intentConfidence()`（原始分值，可能是 0.3x） |
| `toolsUsed` / `toolCalls` | 只含外部传入的 knowledge trace |

> 边界：`OTHER` 但 `confidence >= 0.5`（例如 LLM 明确说"这是 other，0.9" → 0.63）**不会澄清**，会进 GeneralAgent。
> 且澄清只做**一轮**：下一次仍 OTHER 仍低置信会再返回同一句话，代码中未找到多轮槽位填充/澄清状态机。

### 2.5 `IntentCategory` 全部枚举值（19 个，原样抄）

```java
QUERY, COMPLAINT, REQUEST, GREETING, ESCALATION, TECHNICAL, BILLING, ACCOUNT, FEEDBACK,
ORDER_STATUS, LOGISTICS, REFUND, INVOICE, PAYMENT_ISSUE, ACCOUNT_SECURITY,
TECHNICAL_LOGIN, TECHNICAL_CRASH, HUMAN_HANDOFF, OTHER
```
（`IntentCategory.java:3-23`。前 9 个是"粗粒度/通用"，后 9 个是细粒度，最后 `OTHER` 是兜底。）

**细→粗的意图组映射**（`INTENT_GROUPS`，`IntentRecognizer.java:68-78`，API 字段 `intent_group`，`intentGroup()` `:386-388` 会把没有映射的意图原样小写返回）：

| 细粒度 | 归属组 |
|---|---|
| ORDER_STATUS, LOGISTICS | `query` |
| REFUND, INVOICE, PAYMENT_ISSUE | `billing` |
| ACCOUNT_SECURITY | `account` |
| TECHNICAL_LOGIN, TECHNICAL_CRASH | `technical` |
| HUMAN_HANDOFF | `escalation` |

### 2.6 附带产物：紧急度与实体

- **`UrgencyLevel`：`LOW, MEDIUM, HIGH, CRITICAL`**（`UrgencyLevel.java:3-8`）。判定顺序（`urgency()`，`:314-327`）：
  1. 文本含 `紧急` / `urgent` / `立刻` → **CRITICAL**
  2. 含 `今天` / `马上` / `尽快`，或 `intent == ESCALATION`，或 `intent == HUMAN_HANDOFF` → **HIGH**
  3. `intent == COMPLAINT` → **MEDIUM**
  4. 其余 → **LOW**
  > 注意顺序：**纯文本关键词优先于意图**（一句"紧急咨询"会直接 CRITICAL，即使意图是 QUERY）。
- **实体抽取**（`extractEntities`，`:263-275`，正则，`CASE_INSENSITIVE`，结果 `distinct` 去重）：
  - `order_id`：`(?:订单号?|order(?:_id)?|#)\s*[:：#]?\s*([A-Za-z0-9_-]{4,32})`（取 group 1）
  - `product`：**恒为 `List.of()`（空）**——代码中未找到实现
  - `date`：`(今天|明天|昨天|本周|这周|下周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)`
  - `error_code`：先 `\b[45]\d{2}\b`，再合并 `\b[A-Z][A-Z0-9_-]{2,16}\b`（`uniqueConcat`），**同名 key 被 put 两次，后者覆盖前者**
  - `amount`：`((?:¥|￥)\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*(?:元|块|rmb|cny|usd|美元))`

### 2.7 进程内缓存（面试可主动提的工程点）

`cache` 是 `ConcurrentHashMap<String, IntentResult>`（`:82`）：
- key = `normalize(message)` + 最近 **3 条** history 的 `|role:content`（`cacheKey()`，`:372-384`）；注意 Controller 传给 LLM 的是最近 **5 条**，缓存只认 **3 条**，两者口径不同。
- 无 TTL、无容量上限策略：`if (cache.size() > 1000) cache.clear();`（`:112-114`）——**全清**，不是 LRU，会有一次"惊群"式的穿透。
- 缓存 key **不含 userId**：不同用户发同一句话会共享意图结果（意图/实体都只依赖 message，通常无副作用；但如果将来引入用户画像就会出错）。

---

## 3. 编排：真实实现

### 3.1 路由表：静态映射（第一层）

构造函数里建 `EnumMap<IntentCategory, AgentType>`（`AgentOrchestrator.java:37-47`）：

| intent | AgentType |
|---|---|
| TECHNICAL / TECHNICAL_LOGIN / TECHNICAL_CRASH | TECHNICAL |
| BILLING / REFUND / INVOICE / PAYMENT_ISSUE / ACCOUNT / ACCOUNT_SECURITY | BILLING |
| ESCALATION / HUMAN_HANDOFF | ESCALATION |
| 其余（QUERY / COMPLAINT / REQUEST / GREETING / FEEDBACK / ORDER_STATUS / LOGISTICS / OTHER …） | 未映射 → GENERAL |

**`AgentType` 只有 4 个值**：`GENERAL, TECHNICAL, BILLING, ESCALATION`（`AgentType.java:3-8`）。
但**只有 3 个被注册成实例**：`AgentConfig.agentPool()` 返回 `Map.of(GENERAL→[GeneralAgent], TECHNICAL→[TechnicalAgent], BILLING→[BillingAgent])`（`AgentConfig.java:14-21`），**没有 ESCALATION 实例**。
→ 后果见 3.6 与第 7 节。

### 3.2 领域打分公式（第二层，`domainScores`，`:213-273`）

**基础分**（`:216-218`）：`GENERAL = 0.1`，`TECHNICAL = 0.0`，`BILLING = 0.0`。

**① intent 归类加分（互斥，每次最多命中 1 条）**

| 判定 | 加分 |
|---|---|
| intent ∈ {QUERY, ORDER_STATUS, LOGISTICS, REQUEST, COMPLAINT, GREETING, FEEDBACK, OTHER} | `GENERAL += 0.55` |
| intent ∈ {TECHNICAL, TECHNICAL_LOGIN, TECHNICAL_CRASH} | `TECHNICAL += 0.75` |
| intent ∈ {BILLING, ACCOUNT, ACCOUNT_SECURITY, REFUND, INVOICE, PAYMENT_ISSUE} | `BILLING += 0.75` |

**② 关键词命中加分（三路各自独立，可叠加）**（`:252-258`）

```java
TECHNICAL += min(0.45, technicalHits * 0.18)   // 9 个关键词：崩溃 报错 error crash 无法登录 登录失败 500 401 验证码
BILLING   += min(0.45, billingHits   * 0.18)   // 10 个关键词：退款 退货 扣款 发票 账单 支付 订阅 refund invoice 多扣
GENERAL   += min(0.35, generalHits   * 0.12)   // 8 个关键词：订单 物流 快递 配送 会员 积分 咨询 帮助
```

`countHits()`（`:320-328`）是**逐关键词布尔命中**后就计数，同一关键词出现多次只算 1 次。
因此每一路命中 2 个就接近封顶：technical 2→0.36、3→0.54 被截到 **0.45**；general 2→0.24、3→0.36 被截到 **0.35**。

**③ 实体加分**（`:260-269`）

| 实体非空 | 加分 |
|---|---|
| `error_code` | `TECHNICAL += 0.2` |
| `amount` | `BILLING += 0.15` |
| `order_id` | `GENERAL += 0.1` |

**④ 收尾**：`scores.replaceAll((k,v) -> round(v))`，`round()` = `Math.round(v * 1000.0) / 1000.0`（三位小数，`:454-456`）。

**分数上限**（用于回答"会不会超 1"）：TECHNICAL 理论最高 `0.75 + 0.45 + 0.2 = 1.40`，GENERAL `0.1 + 0.55 + 0.35 + 0.1 = 1.10`。所以**分数不是概率、可以 > 1**，只在生成 `routing_confidence` 时被 `min(..., 1.0)` 截断。

### 3.3 primary / supporting 判定与 `routing_confidence`

`routeDecision()`（`:169-211`）的完整判定顺序：

1. **`urgency == CRITICAL`** → `primary = ESCALATION`，`supporting = []`，reason = `"紧急度为 CRITICAL，触发升级路由"`，**`confidence = 1.0`**（`:170-172`）。→ 直接影响：**紧急场景永远单 Agent、永远不走并行**。
2. **`intent == ESCALATION || intent == HUMAN_HANDOFF`** → `primary = ESCALATION`，`supporting = []`，reason = `"意图为 xxx，触发升级路由"`，**`confidence = max(intentConfidence, 0.8)`**（`:173-181`，即保底 0.8）。
3. 计算 `domainScores(req)`，再过滤出"可用"类型：`agentType == GENERAL || pool.containsKey(agentType)`（`:185-189`）。
   - `availableScores.isEmpty()` → 返回 `GENERAL` + `confidence 0.1` + reason `"无可用专属 Agent，降级到 GeneralAgent"`。
   - **⚠️ 这个分支实际不可达**：`domainScores` 永远会 `put(GENERAL, 0.1)` 且 GENERAL 恒满足条件，所以 `availableScores` 至少有一个元素。属死代码。
4. **primary** = 按分值降序排序后的第 1 个（`ordered.getFirst()`）。排序用 `sorted(comparingByValue().reversed())`，对上的是 `EnumMap` 的枚举声明序，**并列时按 `GENERAL > TECHNICAL > BILLING` 的枚举序取前者**（稳定排序）。
5. **supporting**（`:199-204`）——三个条件同时满足：

```java
entry.getKey() != AgentType.GENERAL                      // ① GeneralAgent 永远不辅助
&& entry.getValue() >= 0.45                              // ② 绝对阈值
&& entry.getValue() >= primaryScore * 0.55               // ③ 相对阈值（不能比主 Agent 差太多）
```

6. **`routing_confidence`** = `round(Math.min(primaryScore, 1.0))`（`:209`）——**就是 primary 的领域分（封顶 1.0）**，不是概率、不是多 Agent 的融合置信度。

**举例（可直接背）**："退款申请一直报错 500，登录失败"
- 假设意图为 REFUND → `BILLING = 0.75 + 0.18（退款）= 0.93`
- `technicalHits` = 报错/500/登录失败 = 3 → `min(0.45, 0.54) = 0.45`；`error_code` 命中 `500` → `+0.2` ⇒ `TECHNICAL = 0.65`
- 校验：`0.65 ≥ 0.45` 且 `0.65 ≥ 0.93 × 0.55 = 0.512` ⇒ **supporting = [TECHNICAL]**
- 结果：`primary = billing`，`supporting = [technical]`，`routing_confidence = 0.93`，进入并行分支。

### 3.4 复合问题的并行派发与结果合并

`runParallel()`（`:119-156`）：

```java
List<CompletableFuture<AgentResponse>> futures = targets.stream()
        .map(type -> CompletableFuture.supplyAsync(() -> execute(req, type)))
        .toList();
List<AgentResponse> responses = futures.stream().map(CompletableFuture::join).toList();
```

- **线程池**：`supplyAsync` **没有传 Executor** ⇒ 用的是 **`ForkJoinPool.commonPool()`**。
  项目里虽然 `AsyncConfig` 打了 `@EnableAsync`（`AsyncConfig.java:6-9`），但**这条链路完全没用到它**；全仓库唯一的 `@Async` 在 `MemoryManager.java:72`。**没有自定义线程池、没有 core/max/queue 配置**。
- **没有超时**：`join()` 无 `orTimeout`/`get(timeout)`，任一 Agent 的 LLM 调用 hang 住就会拖住整个请求。
- **异常基本不会炸**：`BaseAgent.handle()` 内部 `try/catch (Exception)` 并返回 `success=false` 的响应（`BaseAgent.java:36-40`），所以 `supplyAsync` 抛异常的概率很低；即便抛了，`join()` 会 `CompletionException` 冒到 Controller，没有兜底。
- **合并策略：字符串拼接**（`:126-133`）：

```java
for (AgentResponse r : responses) {
    if (r.success()) {
        String role = (r.agentType() == decision.primaryAgent()) ? "主处理" : "辅助处理";
        parts.add("[" + r.agentType().name().toLowerCase() + " - " + role + "]\n" + r.content());
    }
}
String content = parts.isEmpty() ? "抱歉，所有 Agent 均处理失败。" : String.join("\n\n", parts);
```

  - 只有 `success == true` 的结果会被拼进去；顺序 = `decision.agentTypes()` 顺序 = **primary 先、supporting 后**。
  - **没有二次 LLM 汇总/去重/消解矛盾**，就是两段回答叠在一起，前端看到的是带 `[technical - 辅助处理]` 前缀的长文本。
- `escalated = responses.stream().anyMatch(AgentResponse::escalate)`（`:134`）——**并行分支只看 Agent 自己返回的 escalate**；串行分支还会额外看 `urgency == CRITICAL` 和 `intent ∈ {ESCALATION, HUMAN_HANDOFF}`（`:88-91`）。这两个分支的语义并不完全一致（实际影响被 3.3 的短路掩盖了，因为 CRITICAL/ESCALATION 都不会产生 supporting）。
- `agentTypes`：取所有 `success` 的响应类型；若全失败则回落到 `targets`（`:146`，保证接口不空）。

### 3.5 Agent 实例择优 + `monitor_penalty` 如何影响 `routing_score`（务必说准）

`execute()`（`:341-351`）：

```java
BaseAgent agent = bestAgent(agentType).orElseGet(() -> bestAgent(AgentType.GENERAL).orElse(null));
if (agent == null) return new AgentResponse(GENERAL, "服务暂时不可用，请稍后重试。", false, 0.0, 0, false, "", false, false, false, "service unavailable");
AgentResponse response = agent.handle(req);
if (!response.success() && agentType != GENERAL) return bestAgent(GENERAL).map(a -> a.handle(req)).orElse(response);
return response;
```

`bestAgent()`（`:423-426`）：`pool.get(type)` 里按 **`stats().routingScore()` 取最大值**（同一类型多实例时才有多选）。

`AgentStats.routingScore()`（`AgentStats.java:34-38`）——**这就是"降级/monitor_penalty"的真实公式，是乘法，不是减法**：

```java
double latencyScore = 1.0 / (1.0 + avgLatencyMs() / 1000.0);   // 0~1，越大越慢越低
double base         = successRate() * 0.7 + latencyScore * 0.3;
double routingScore = base * Math.max(0.0, 1.0 - monitorPenalty);   // ← 乘法惩罚系数
```

- `successRate()`：无样本时返回 **1.0**（不是 0）；`avgLatencyMs()`：无样本返回 0.0（`AgentStats.java:24-32`）。
- `monitorPenalty` 是 `volatile double`，由 `setMonitorPenalty()` 夹紧到 **`[0.0, 0.9]`**（`AgentStats.java:44-46`）。
- `monitor_penalty` 的值由 `PerformanceMonitor.routingPenalty(successRate, avgMs)` 计算（`PerformanceMonitor.java:92-101`）：

```java
double penalty = 0.0;
if (successRate < 0.90) penalty += Math.min(0.5, (0.90 - successRate) * 2);
if (avgMs > 3000)       penalty += Math.min(0.4, (avgMs - 3000) / 10000);
return Math.min(0.9, penalty);
```

  例：`successRate=0.7` → `+0.4`；`avgMs=8000` → `+0.5` 但被 `min(0.4,...)` 截到 `0.4` ⇒ penalty 0.8 ⇒ `routingScore` 乘 0.2。
- 采集节奏：`@Scheduled(fixedDelay = 10000)`（10 秒一次，`PerformanceMonitor.java:51-81`），从 `orchestrator.stats()` 读 `success_rate` / `avg_ms`，算完 `orchestrator.updateRoutingPenalties(penalties)`（`:446-452`）按 key **`general_0` / `technical_0` / `billing_0`** 回写（key 由 `stats()` 的 `type.name().toLowerCase() + "_" + i` 生成，`:439`）。阈值来自 `PuddingProperties.Monitor`：`successRateThreshold = 0.90`、`latencyMsThreshold = 3000`（默认值）。
- **必须说准的边界**：`monitor_penalty` / `routing_score` **不参与 3.2 的领域打分**，`domainScores()` 里完全没有用到统计信息。它只影响"同一 AgentType 下多个实例挑哪个"。
  而 `AgentConfig` 里**每个类型只注册了 1 个实例** ⇒ 当前 `routingScore` 对路由结果**没有任何可观测影响**，是"能力已实现、尚未生效"。这是很加分的一句实话。

### 3.6 降级链（四层，逐层说）

1. **类型级**：`routing` 表没有该 intent（QUERY/ORDER_STATUS/LOGISTICS/COMPLAINT/...）→ 直接 GENERAL（`domainScores` 的 `GENERAL +0.55` 打底保证 GENERAL 胜出）。
2. **实例级**：目标类型在 `pool` 里不存在 → `bestAgent()` 空 → 回落 `bestAgent(GENERAL)`。**ESCALATION 就死在这一层**：有枚举、有路由表、没实例 ⇒ 实际由 `GeneralAgent` 生成回答。
3. **失败重试级**：目标 Agent `success=false` 且类型不是 GENERAL → 再调一次 GENERAL（`:347-349`），代价是**多一次 LLM 延迟**。
4. **LLM 级**：`SpringAiLlmGateway` 在 `fallback-enabled=true` 时返回确定性中文兜底（`SpringAiLlmGateway.java:66-86`），意图识别里表现为"OTHER 0.0"（见 2.1 的警告）。

### 3.7 编排结果里被"写回"的字段（`OrchestratorResult`）

`OrchestratorResult`（`OrchestratorResult.java:8-23`）13 个字段：`requestId, response, agentType, intent, escalated, latencyMs, agentTypes, primaryAgent, supportingAgents, toolsUsed, toolCalls, routingReason, routingConfidence`。
`routingReason` 是**人可读的完整打分明细**（`routingReason()`，`:275-296`），格式为：

```
intent=refund, group=billing, primary=billing, supporting=technical, scores=[billing=0.93, technical=0.65, general=0.1]
```

> 这是这套方案最大的工程优势：**任何一个路由决策都能从接口字段直接解释**，不需要回捞日志。
> ⚠️ 但 `routingReason` 里的 `scores` 用的是**过滤后的 `availableScores`**（只含可用类型），而 reason 字符串在 `routeDecision` 里传入的是 `availableScores`——不是包含全部三个类型的原始分。

`toolsUsed` / `toolCalls` 由 `collectToolCalls(...)` 合并"外部传入的 knowledge trace + 每个 AgentResponse 的 toolName 非空项"（`:373-421`）；因为 `BaseAgent` 成功时 `toolName` 传的是 `""`，所以**Agent 自身目前不会贡献任何 tool trace**，实际只有 Controller 传进来的知识库 trace。
`recordTrace()` 会写一条 `RequestToolTrace` 到 `RequestTraceStore`，其中 `used_knowledge` 的判定是 `toolsUsed` 含 `"search_knowledge_base"` 或 `"knowledge_search"`（`:367`）。

---

## 4. 回答校验：`AnswerVerifier` 判定什么

`AnswerVerifier.verify(question, answer, context)`（`AnswerVerifier.java:21-45`）：

- 输入：用户问题、Agent 生成的回答、`fullContext`（`memoryText + knowledgeText`，由 Controller 拼好传入，`PuddingController.java:128,133`）。
- 实现：**1 次 LLM 调用**，`llmGateway.chat("", prompt, 0.0, 256)`——system 空、**temperature = 0.0**、256 tokens。
- prompt 要求判断三件事（原文）："判断回答是否解决用户问题、是否基于上下文、是否需要转人工。"
  输出契约：`{"pass":true,"grounded":true,"need_escalation":false,"reason":"..."}`。
- JSON 截取：`raw.indexOf('{')` 到 `raw.lastIndexOf('}')`，然后 `substring(start, end + 1)` 直接反序列化。
  > ⚠️ 这里**没有像 `IntentRecognizer.sliceJsonObject` 那样判 `start>=0 && end>start`**，所以只要模型输出里没有 `{`，`substring(-1, ...)` 会抛 `StringIndexOutOfBoundsException` → 被 `catch` 吃掉 → 走 fallback。
- 返回：`record VerificationResult(boolean pass, boolean grounded, boolean needEscalation, String reason)`（`:47-48`）。
- **异常 fallback（fail-open）**（`:41-44`）：

```java
boolean grounded = context != null && !context.isBlank();
return new VerificationResult(true, grounded, answer != null && answer.contains("转人工"), "verifier fallback");
```

  即：**校验器崩了就默认"通过"**（`pass=true`），`grounded` 退化为"上下文非空"，`needEscalation` 退化为字符串包含 `"转人工"`，reason 固定 `"verifier fallback"`。

- **什么条件置 `escalated`**：只有一处（`PuddingController.java:134`）：

```java
boolean escalated = result.escalated() || verification.needEscalation();
```

  即最终 `escalated` 来自 **orchestrator 的判定 ∪ 校验器的 `need_escalation`**。而 orchestrator 侧 `escalated` 的构成是（`:88-91` / `:134`）：
  `AgentResponse.escalate()`（回答文本含 `转人工`/`人工客服`/`escalate`/`specialist`/`无法处理`，`BaseAgent.java:70-77`）**或** `urgency == CRITICAL` **或** `intent ∈ {ESCALATION, HUMAN_HANDOFF}`。
  （澄清分支里 `escalated=false` 固定。）
- **校验器不做什么**（面试要点，别夸大）：
  - 不重写/重生成回答；
  - 不改变路由、不影响 `agentType`；
  - `pass=false` **不会**改变返回给用户的 `response`，只是把 `verified` 字段置 false 透出（`ChatResponse.verified`）；
  - 没有分数、没有 rubric 分项、没有多模型交叉评审、没有人工反馈回路（代码中未找到 thumbs/feedback 采集）。

---

## 5. 每个模块"为什么这么设计"：替代方案 / 代价 / 代码里的取舍

### 5.1 意图识别：三路加权投票 vs 单一 LLM

- **替代方案**：a) 纯 LLM 结构化输出（一次调用搞定）；b) 真 embedding + 向量近邻分类；c) 训练一个小分类器；d) 纯规则。代码选的是 d+a+b(伪) 的混合。
- **动机（从代码可见）**：LLM 是唯一能"理解"的通道，所以给 0.70；但 LLM 有失败/超时/输出不可解析风险，所以留两条**零成本本地通道**兜底，并把它们的主要职责定为"**细粒度纠偏**"（refine）而不是"独立判定"。
- **代价**：
  - 延迟上几乎是白送（两路都是本地字符串运算，微秒级），**成本几乎为零**（只有 1 次 256-token 调用）。
  - **维护成本高**：54 条模板 + 9+8 张关键词表 + 19 个枚举 + 5 个意图组映射，四份需要人工同步；加一个意图要改 4 处（枚举、TEMPLATES、SPECIFIC/GENERIC 集合、pattern 表、INTENT_GROUPS）。
  - **可解释性 vs 准确性矛盾**：`sourceScores` 三个数字看着很像"集成学习"，但实际 `0.10` 的 pattern 单靠自己永远过不了阈值（见 2.2 推论），**权重的"表面均衡"和"实际作用"不一致**——这是最值得候选人主动坦白的取舍。
- **代码体现的取舍**：宁可让 `IntentResult` 带上 `reasoning`（LLM 自由文本）和 `sourceScores`（三路原始分），也不做二次融合校准——**先保证可观测，再谈准确率**。

### 5.2 意图识别：字符 n-gram Jaccard vs 真 embedding

- **替代方案**：调 embedding API / 本地 sentence-transformers 做语义相似度。
- **动机**：零依赖、零成本、无网络抖动、对中文免分词（字符级天然适配中文）、结果确定可复现。
- **代价**：
  - **没有同义/近义扩展**："退钱" vs "退款" 只共享 unigram `退`，Jaccard 极低；"账单有问题" vs "为什么扣了两次款"几乎无交集。
  - **只用 54 条模板**做全集，覆盖率天花板很低。
  - unigram 参与会让长句之间"无意义地相似"（功能词/标点贡献大量 unigram 交集），**分数分布不可校准**——这也是它只能吃 0.20 权重的现实原因。
- **代码体现**：`sourceScores` 里叫 `embedding` 但实现是 Jaccard，说明**接口抽象留了"以后换真 embedding"的口子**，但没换。

### 5.3 编排：规则加权打分 vs LLM 路由器

- **替代方案**：a) 让 LLM 直接输出 `{"agent":"billing","supporting":["technical"]}`（多一次调用或复用意图那次）；b) 训练/微调路由分类器；c) 语义路由（embedding 形态）。
- **动机**：**零额外 LLM 调用、零额外延迟、完全确定性、可解释到小数点**。`routingReason` 能把三个领域分直接吐给前端/排障。
- **代价**：
  - 三张关键词表（9/10/8 个词）**硬编码在方法体里**（`domainScores` 局部变量），改词要改代码重新发布；没有配置化、没有热更新。
  - **双计分**：同一个"退款"既通过 intent 归类 +0.75，又通过 `billingHits` +0.18 ⇒ 分数不可跨场景比较、无法设统一阈值。
  - **实体加分弱**：`product` 恒空（2.6），等于有一个实体维度是废的。
  - 长尾/反讽/否定句（"我不想退款"）会被关键词机制误伤。代码里**没有否定词处理**。

### 5.4 编排：CompletableFuture + commonPool vs 专用线程池

- **替代方案**：a) 配置 `ThreadPoolTaskExecutor`（有界队列 + 拒绝策略 + 线程名）；b) Spring `@Async`（项目里已 `@EnableAsync`）；c) 结构化并发/`invokeAll` with timeout；d) 串行调用（省一次并发复杂度）。
- **动机**：代码量最小（一行 `supplyAsync`），只有 2 个 future，事件循环压力小。
- **代价**（面试重点，也是项目的真实技术债）：
  - `ForkJoinPool.commonPool()` 并行度是 `CPU-1`，池子里跑的是**网络 IO（LLM HTTP）**，不是 CPU 任务 ⇒ 并发请求一多就会互相排队；一旦被别处（如 parallelStream）共用，还会更难排查。
  - **无超时、无隔离**：`join()` 无限等。
  - **无法监控**：没有线程池指标、没有排队时长（Micrometer 只接了 success_rate / monitor 计数）。
  - `@EnableAsync` 存在却没用在最需要它的地方，是"配置与实现脱节"的典型。
- **代码体现的取舍**：为了合并简单，`runParallel` 里 `join()` 后**只保留 `success == true` 的结果**并直接拼字符串；宁可给用户一段稍长的双段回答，也不做第二次 LLM 融合（省一次调用与延迟）。

### 5.5 三类 Agent：模板方法 + system prompt 差异化

- **替代方案**：每个 Agent 独立实现完整链路（可定制检索、工具、后处理）；或用 LangChain/Spring AI 的 tool-calling 让 Agent 自主决定。
- **动机**：`BaseAgent.handle()` 固定了"拼 prompt → 调 LLM（0.2 / 1024）→ 关键词判转人工 → 记 stats"这条最小闭环，子类**只有 21 行**（真正只覆写 2 个方法）。新增一个 Agent 的成本 ≈ 复制一个 21 行文件。
- **代价**：
  - **三个 Agent 的能力差异只有一行 system prompt**，`ToolCallTrace` 相关字段（`toolName`/`toolCached`/`toolReranked`）在 Agent 内部**全都没被使用**（成功时 `toolName=""`、`toolSuccess=true` 是占位），说明"Agent 自主调工具"的架构**预留了但没实现**——知识检索实际发生在 Controller 里，Agent 只是消费者。
  - **无 history**：`buildPrompt()` 只拼 `[背景信息] context` + `[结构化实体] entities` + `[用户问题] message`（`BaseAgent.java:47-57`），多轮上下文完全依赖 `MemoryManager` 压缩进 `context` 文本。
  - **转人工靠关键词嗅探回答**（`:70-77`）：LLM 只要写一句"如仍无法解决，可转人工"，就会被判 `escalate=true` ⇒ **误升级率高**；正确做法是结构化输出或用 function-calling。
  - **stats 无时间窗口**：`AtomicLong total/success/totalLatencyMs` 是**全生命周期累计**，早期一次冷启动慢请求会永久拉低 `avgLatencyMs`；`successRate` 无样本时返回 **1.0**（乐观默认），冷启动即满分。

### 5.6 回答校验：一次低温 LLM 自评 vs 不校验/规则校验

- **替代方案**：a) 不校验（省一次调用）；b) 规则校验（长度/是否含"转人工"/是否引用知识库）；c) 换第二个模型做 judge；d) 校验失败就重生成（self-refine）。
- **动机**：客服场景需要"有没有胡说/要不要转人工"的判断，纯规则做不到；`temperature=0.0` 让判断尽量稳定；`fail-open` 保证校验器故障时**不阻断主流程**。
- **代价**：
  - **每次请求 +1 次 LLM 调用**（256 tokens）+ 一次串行 RTT。这意味着 `/chat` 的 P99 ≈ 三次网络往返之和，**没有并行化**（校验依赖回答，天然串行；但意图识别其实可以和记忆读取并行，代码里也是串行的）。
  - **自评偏差**：同一个模型评自己的回答，缺少交叉验证/位置交换，`pass` 只是一个布尔，没有 rubric 分项和阈值可调。
  - **`pass=false` 无后果**：不重生成、不降级、不拦返回，用户看到的仍是原回答 ⇒ 校验目前是**观测手段而非防护手段**。
- **代码体现的取舍**：选择了"标注但不改流程"——用 `ChatResponse` 的 `verified` / `grounded` / `escalated` 三个布尔把质量信号透出，把处置权留给上层/人工。

---

## 6. 面试追问清单（8 + 8 + 6 + 6）

> 每题给两组话术：**30 秒版（要点）** 和 **深挖一层（细节/数字）**。

### 6.A 意图识别（8 题）

**Q1「你这个"三路信号"具体是哪三路？权重怎么定的？」**
- 30s：① LLM（`temperature=0.1`、256 tokens）权重 **0.70**；② 字符 1~3-gram 的 Jaccard 相似度，跟 18 类 ×3 条模板比对，权重 **0.20**；③ 关键词子串规则，权重 **0.10**。三路各自给意图投票，按 `0.7/0.2/0.1` 加权累加取最大。
- 深挖：权重是**硬编码常量**，没抽配置、仓库里没有调参实验记录（代码中未找到）；要坦承这是先验而不是调出来的。然后补一句真正有价值的：因为权重和 = 1 且**不做重归一**，单靠关键词最多 0.10、单靠 n-gram 最多 0.20，都过不了 0.5 阈值 ⇒ **这两路天生不能独立定意图**，pattern 的实际作用是 refine（见 Q5）。

**Q2「那 0.2 那一路是 embedding 吗？」**
- 30s：不是。代码里字段名叫 `embedding` 只是历史命名，实现是 `charNgrams`（n=1,2,3 拼接）+ 去重后的 Jaccard，纯本地字符串运算。
- 深挖：说清代价——无同义扩展（"退钱"vs"退款"只共享 unigram 退）、只用 54 条模板做全集、unigram 会让长句无意义地相似、分数不可校准；也说明它为什么只能拿 0.20；换成真 embedding 的位置就是 `semanticRecognize`。

**Q3「置信度怎么合成？是概率吗？」**
- 30s：不是概率。它是 `score(c) = Σ_路 w_路 · conf_路 · [该路判为 c]`，取 argmax。不归一化、不 softmax、各意图不互补。
- 深挖：单靠 LLM 要过 0.5 阈值需 `conf ≥ 0.714`；semantic 同意 0.6 时降到约 0.543。API 里 `intent_confidence` 就是它，保留 4 位小数（`PuddingController.round(c,4)`）。**不能拿它当"准确率"或做 SLA 阈值**，要校准得另做。

**Q4「低置信度会怎样？澄清的触发条件是什么？」**
- 30s：`vote()` 里 `< 0.5` 直接判 `OTHER`（但保留原分值）；编排层 `needsClarification` 要求 `intent==OTHER && message.trim().length()>2 && intentConfidence<0.5`，命中就返回**固定澄清话术**，`agentType=GENERAL`、`escalated=false`、`routingReason="低置信度 OTHER 意图，先澄清用户需求"`。
- 深挖：长度 ≤2（"嗯""哦"）刻意不澄清，避免打断；只做**一轮**，没有槽位/多轮澄清状态机；`OTHER` 但 `confidence ≥ 0.5`（LLM 很确信"这是 other"）**不澄清**，直接进 GeneralAgent。

**Q5「细粒度意图（refund/invoice/technical_login）到底是谁决定的？」——本题最容易被问穿**
- 30s：是**关键词规则兜底决定的**。`vote()` 有一条 refine 规则：当投票结果是粗粒度（QUERY/BILLING/TECHNICAL/ACCOUNT/ESCALATION）、而 pattern 给出了细粒度意图、且 `patternConf ≥ 0.5`、且 `best.score < 0.8` 时，**直接用 pattern 的细粒度意图覆盖**，置信度取两者 max。
- 深挖：关键在 `best.score < 0.8` 这个门槛——因为 LLM 单路最多 `0.7 × 1.0 = 0.7 < 0.8`，**这条规则几乎恒成立**，等价于"句子里有一个细粒度关键词，LLM 的粗粒度判断就被覆盖"。风险：否定句（"我不需要退款，我要开发票"）两条关键词都命中，取的是 `Map.ofEntries` **未定义遍历序**先命中的那个 ⇒ 结果有非确定性。改法：LLM 高置信时禁止关键词覆盖，或对 pattern 命中做冲突消解。

**Q6「LLM 挂了会怎样？」**
- 30s：`vote()` 有 `failed` 分支：先退 semantic（非 OTHER 且 >0），再退 pattern，都不行才 `OTHER/0.0`。此时返回的置信度是**单路原始分**，量纲和正常分支不同。
- 深挖（能立刻拉开差距）：`failed` 只在**抛异常**时置位；但 `SpringAiLlmGateway` 在 `fallback-enabled=true`（默认）时**不抛异常**，而是返回一段中文散文。于是 `sliceJsonObject` 取不到花括号返回 `"{}"`，`readValue("{}")` **成功**、不抛异常 ⇒ `failed` 不会被置位，LLM 表现为"投了 OTHER 0.0"。也就是说**默认配置下那条兜底分支基本走不到**，实际靠的是 refine 规则把结果救回来。

**Q7「加一个新意图要改几处？意图体系为什么分粗细两层？」**
- 30s：加意图要动 5 处——`IntentCategory` 枚举、`TEMPLATES`、`SPECIFIC_INTENTS`/`GENERIC_INTENTS`、`patternRecognize` 的（细/粗）规则表、`INTENT_GROUPS`，另外 `AgentOrchestrator.domainScores` 的 intent 集合也要看。粗细两层的意义：细粒度给业务/路由用（`intent_group` 归一后仍然可下钻），粗粒度给兜底和组聚合用。
- 深挖：这是"枚举 + 硬编码表"的必然代价，**没有单一数据源（SSOT）**；工程上应改成一份 YAML（意图、模板、关键词、组、domain 权重）在启动时加载。项目里 `SkillManager` 已经证明团队有这个能力（它就是从 `skills/` 目录动态加载 `.md/.json`）。

**Q8（最刁钻）「你这套投票相比"就调一次 LLM"，准确率提升多少？有数据吗？」**
- 30s：**代码里没有 A/B 或消融数据**（`evaluation/EndToEndEvaluator` 存在，但里面没有对本模块做分路消融），我不编数字。我能说清的是机制上的收益：LLM 不可用/输出不可解析时系统仍有确定性输出（可用性收益），以及细粒度意图由关键词保底（召回收益）。
- 深挖：诚实给出三个可以立刻做的验证实验——(1) 关掉 semantic/pattern 两路做消融，看 `EndToEndEvaluator` 的意图准确率与 `sourceScores` 分布；(2) 用人工标注 200 条看 `best<0.8` refine 的净收益（正例 vs 否定句误伤）；(3) 把 `confidence` 分桶画 reliability 曲线验证它能不能当阈值用。然后给出你倾向的改造顺序。

### 6.B 编排（8 题）

**Q1「领域分怎么算？」**
- 30s：三个领域打底 `GENERAL=0.1 / TECHNICAL=0 / BILLING=0`；① intent 归类互斥加分（general 类 +0.55、technical 类 +0.75、billing 类 +0.75）；② 关键词命中数加分 `min(0.45, hits*0.18)` / `min(0.35, hits*0.12)`；③ 实体加分（error_code→technical +0.2、amount→billing +0.15、order_id→general +0.1）；最后 `round` 到三位小数。
- 深挖：分数**可以超过 1**（technical 上限 1.40），只在 `routing_confidence` 处 `min(1.0)`；`countHits` 是逐关键词布尔命中，所以 3 个命中就封顶；同一词被计两次（intent 归类 + 关键词）是设计上的"加权确认"，但也让分数不可跨场景比较。

**Q2「primary / supporting 怎么定？routing_confidence 是什么？」**
- 30s：按分排序取第 1 为 primary；其余类型要满足 **`≠ GENERAL` 且 `≥0.45` 且 `≥ primaryScore×0.55`** 才成为 supporting；`routing_confidence = round(min(primaryScore,1.0))`。
- 深挖：两条阈值一个是**绝对线**（防弱信号参与）一个是**相对线**（防与主 Agent 差距过大）；GENERAL 永不做辅助是产品判断（通用回答做辅助只会稀释答案）。`routing_confidence` 是**领域分不是概率**，澄清分支直接透出 `intentConfidence`、ESCALATION 分支用 `max(intentConfidence, 0.8)`、CRITICAL 固定 1.0、无可用 Agent 固定 0.1，**五种量纲混在同一个字段里**，前端不该拿它做统一阈值。

**Q3「为什么不用 LLM 直接路由？」**
- 30s：省一次调用和一次网络往返；确定性（同输入同输出）；可解释（`routingReason` 直接列出三个分数）；出问题能只改规则不动模型。
- 深挖：代价是**规则维护**和**语义盲区**（否定句、反讽、长尾表达）；如果重做，我会复用意图识别那一次 LLM 调用，让它顺带输出 `{"primary":..., "supporting":[...]}`（零额外成本），再把规则分作为 fallback 和交叉校验——这样既有语义又有兜底。

**Q4「复合问题怎么并行？用什么线程池？」**
- 30s：`runParallel` 用 `CompletableFuture.supplyAsync(() -> execute(req, type))`，**没有传 executor ⇒ 用的是 `ForkJoinPool.commonPool()`**，然后按 `decision.agentTypes()` 顺序 `join()`。
- 深挖（必须主动说）：项目里虽然有 `@EnableAsync`（`AsyncConfig`），但**这条链路没用它**，全仓唯一 `@Async` 在 `MemoryManager`。commonPool 并行度是 `CPU-1` 却在跑网络 IO，并发一高会排队；**没有超时、没有隔离、没有线程池指标**。正确做法：`ThreadPoolTaskExecutor`（有界队列 + CallerRuns/拒绝策略）+ `orTimeout`。这是我知道但没做完的部分——主动承认比被问出来好。

**Q5「多 Agent 的结果怎么合并？会不会矛盾？」**
- 30s：只取 `success=true` 的响应，按 `[technical - 辅助处理]` 这种前缀拼成 `"\n\n"` 分隔的长文本；全失败给固定话术 `"抱歉，所有 Agent 均处理失败。"`；`escalated` 取 `anyMatch`。
- 深挖：**没有二次 LLM 融合**，两段可能重复甚至冲突，用户体验是"专家 A 说…专家 B 说…"。要做得更好需要一次 summary 调用（成本 +1 次调用、+1 RTT），或改成 primary 生成 + supporting 只提供"补充要点"的受控合成。另外并行分支的 `escalated` 只看 Agent 自报（`:134`），串行分支还看 urgency/intent（`:88-91`），**两条路径语义略有出入**。

**Q6「monitor_penalty 怎么影响路由？」——最容易答错**
- 30s：它**不参与领域打分**。它作用在 `AgentStats.routingScore()`：
  `latencyScore = 1/(1 + avgMs/1000)`；`base = successRate*0.7 + latencyScore*0.3`；**`routingScore = base * max(0, 1 - monitorPenalty)`（乘法惩罚系数）**。然后 `bestAgent()` 在同类型实例里取 `routingScore` 最大者。
- 深挖：`monitorPenalty` 由 `PerformanceMonitor` 每 **10 秒**用 `sr<0.90 → +min(0.5,(0.9-sr)*2)`、`avg>3000ms → +min(0.4,(avg-3000)/10000)`、总封顶 0.9 算出，按 `general_0/technical_0/billing_0` 回写。**但 `AgentConfig` 每类型只注册 1 个实例** ⇒ 当前对路由结果无实际影响，是"已实现未生效"。

**Q7「降级路径画一下。」**
- 30s：四层——① intent 没路由映射 → GENERAL；② 目标类型不在 pool（**ESCALATION 就死在这层**）→ 回落 GENERAL；③ 专属 Agent `success=false` → 重试 GENERAL；④ LLM 层 `SpringAiLlmGateway` 的确定性兜底文本。
- 深挖：②这一层意味着**"转人工"实际上由 GeneralAgent 回答**：`OrchestratorResult.primaryAgent` 是 `escalation`，但 `agentType` 会是 `general`（因为 `response.agentType()` 来自真正执行的 Agent）。文档/前端如果按 `agent_type` 统计"转人工量"会统计错。

**Q8（最刁钻）「你说支持多 Agent 协作，那紧急场景为什么不协作？服务抖动时会不会雪崩？」**
- 30s：紧急场景是**故意的短路**——`routeDecision` 第一分支 `urgency==CRITICAL` 就直接 `ESCALATION + confidence 1.0 + supporting 空`，不给辅助 Agent 机会；产品逻辑是"紧急先转人工，别再让模型多说话"。
- 深挖：雪崩风险我认，且能指出三处具体缺口——(1) `commonPool` 跑 IO 且无队列上限/超时；(2) `bestAgent` 失败后**串行重试 GENERAL**，故障时把延迟从 2×RTT 叠成 3×RTT（加上意图和校验就是 5 次调用）；(3) `IntentRecognizer` 缓存 `>1000` 时 `cache.clear()` 全清，会周期性放大 LLM 压力。缓解方向：有界线程池 + 超时 + 熔断（`tool/CircuitBreaker` 已有现成实现，但只用在工具层）、意图缓存改 LRU/Caffeine。

### 6.C 三类 Agent（6 题）

**Q1「三个 Agent 差在哪里？」**
- 30s：**只差 `type()` 和 `systemPrompt()` 两个覆写**，各 21 行。链路全部继承 `BaseAgent.handle()`：拼 prompt → `chat(system, user, 0.2, 1024)` → `needsEscalation(content)` → `stats.record(ok, latency)`。
- 深挖：这是模板方法模式，新增 Agent 成本极低；但也意味着"专业能力"目前全压在一行 prompt 上，没有各自的工具集、检索策略或输出格式。

**Q2「prompt 是怎么拼的？带历史吗？」**
- 30s：`[背景信息] context` + `[结构化实体] entities` + `[用户问题] message`（`BaseAgent.buildPrompt`）；`context` 是 Controller 拼的"记忆文本 + 知识库检索结果"。**不带 message 级 history**。
- 深挖：多轮全靠 `MemoryManager` 压缩进 `context`；`history` 只在意图识别里用（而且缓存 key 只认最近 3 条、prompt 里塞的是最近 5 条）。另外 `entities` 是 `Map.toString()` 直接进 prompt，不是 JSON。

**Q3「转人工怎么判的？」**
- 30s：硬编码关键词嗅探**回答文本**：`转人工`/`人工客服`/`escalate`/`specialist`/`无法处理`（`BaseAgent.needsEscalation`）。
- 深挖：这是最脆的一环——LLM 写一句"如仍未解决可转人工"就误升级。改法：让模型输出结构化 `{"escalate":true}`，或在 prompt 里明确"只有真正需要人工操作时才输出 `[ESCALATE]` 标记"。

**Q4「动态 Skills 是什么？」**
- 30s：`SkillManager.promptFor(message, agentType)` 从 `pudding.skills.root-dir` 扫描 `SKILL.md`/`.md`/`.txt`/`.json`，按 `enabled` + `agents` 列表 + `keywords` 子串命中过滤，拼到 system prompt 末尾（`[动态 Skills]`），全局上限 `maxPromptChars=5000`、单块 `min(3200, remaining)`。
- 深挖：这是本模块**唯一配置化、可热加载**的能力（`POST /skills/reload`），和前面硬编码的关键词表形成鲜明对比——说明团队知道该怎么做，只是没推广到意图/路由层。

**Q5「AgentStats 怎么算成功率/延迟？有窗口吗？」**
- 30s：`AtomicLong total/success/totalLatencyMs` + `volatile monitorPenalty`；`successRate` 无样本返回 **1.0**，`avgLatencyMs` 无样本返回 0。
- 深挖：**全生命周期累计，无滑动窗口**，冷启动或一次抖动会永久污染；`successRate` 乐观默认 1.0 意味着新实例一开始 routingScore 就接近满分。改造：改用环形窗口/指数衰减。

**Q6（最刁钻）「`AgentResponse` 里 `toolName`、`toolCached`、`toolReranked` 是谁填的？」**
- 30s：`BaseAgent` 成功时传 `toolName=""`、`toolSuccess=true`、`toolCached=false`、`toolReranked=false`（`BaseAgent.java:35`），**Agent 自己并不调工具**；`collectToolCalls` 会跳过空 toolName，所以真正的 tool trace 只来自 Controller 传进去的知识库 trace。
- 深挖：说明数据模型是**为"Agent 自主调工具"预留的**，但当前架构里检索在 Agent 外部（Controller 里 `KnowledgeToolManager.searchWithRewrite`），Agent 只是 context 的消费者。诚实的说法是"字段是前瞻设计，当前 `toolSuccess=true` 是占位、语义上不准确"。

### 6.D 回答校验（6 题）

**Q1「校验器判什么？返回什么？」**
- 30s：一次 LLM（`temperature=0.0`、256 tokens）判三件事：`pass`（是否解决）、`grounded`（是否基于上下文）、`need_escalation`（是否要转人工），外加 `reason`。返回 `VerificationResult` 四个字段。
- 深挖：输入是 `question / answer / fullContext`，其中 `fullContext = memoryText + knowledgeText`。

**Q2「在链路的哪个位置？会改回答吗？」**
- 30s：在 **Controller 层**、`orchestrator.run()` 之后（`PuddingController.java:133`），**不在 orchestrator 内部**。只标注，不改写、不重生成、不拦截。
- 深挖：所以 `pass=false` 对用户无感，只体现为 `ChatResponse.verified=false`；`escalated = result.escalated() || verification.needEscalation()` 是唯一会影响业务字段的出口。

**Q3「校验器自己出错怎么办？」**
- 30s：`catch` 里 **fail-open**：`pass=true`、`grounded=context 非空`、`needEscalation=answer 含 "转人工"`、`reason="verifier fallback"`。
- 深挖：注意 `verify` 里 `raw.substring(start, end+1)` **没做 `start>=0` 校验**（对比 `IntentRecognizer.sliceJsonObject` 有校验），模型输出没有花括号时会抛 `StringIndexOutOfBoundsException` 并落到 fallback——结果"看起来正常"，实际校验静默失效。这个 bug 值得主动提。

**Q4「为什么 fail-open 不 fail-closed？」**
- 30s：校验是**增值观测**，不是安全闸门；让它挂掉时阻断主流程，等于用一个质量检查点拖垮可用性。
- 深挖：但代价真实——校验失效时 `verified=true` 会**伪装成"已验证通过"**。改进：加 `verification_state`（`ok`/`fallback`/`error`）区分"通过"与"没校验"。

**Q5「成本多少？能不能省？」**
- 30s：每次 `/chat` 多一次 256-token 调用 + 一次串行 RTT。可以按意图裁剪：`GREETING`/`FEEDBACK`/澄清/升级这类不需要校验；或只在高风险意图（`BILLING`/`REFUND`/`PAYMENT_ISSUE`/`ACCOUNT_SECURITY`）上跑。
- 深挖：也可以和意图识别那次调用合并（一次调用输出 `intent + confidence + 质量字段`）——但它们发生在链路两端（校验要看回答），真正能合并的是"校验 + 下一步记忆摘要"。

**Q6（最刁钻）「`pass` 和 `grounded` 哪个更重要？如果 `pass=true` 但用户仍然不满意，你怎么知道？」**
- 30s：客服场景 **`grounded` 更硬**——`pass` 是主观判断（同一模型自评有偏差），`grounded` 至少锚在是否引用上下文；`pass=true` 但用户不满意的情形我**检测不到**，因为没有用户反馈回路（代码中未找到 thumbs/评分/重问率统计）。
- 深挖：可行的补法不贵——(1) 记录"同一会话内 3 轮内重复提问"作为隐式不满信号；(2) 把 `grounded=false` 且 `pass=true` 的样本单独抽样人工看；(3) 上 `LLMJudge`（项目里 `evaluation/LLMJudge` 已有 judge 能力）做离线抽检，而不是在线每次跑。

---

## 7. 容易讲错 / 翻车的点（凭印象瞎讲必踩）

| # | 错误说法（❌） | 真实情况（✅） |
|---|---|---|
| 1 | "三路信号 = 三个模型/三次 LLM 调用" | **只有 1 次** LLM 调用（`chat("", prompt, 0.1, 256)`）；另两路是纯本地字符串运算（n-gram Jaccard、`contains`），零 API 成本。 |
| 2 | "0.2 那路是向量 embedding 检索" | 字段名叫 `embedding`，实现是 `charNgrams` + `HashSet` 去重后的 **Jaccard**，n 取 **1/2/3**。仓库里**没有**向量库参与意图识别（向量只在知识库检索模块）。 |
| 3 | "权重是调优出来的 / embedding 权重 0.4" | 真实是 **0.70 / 0.20 / 0.10**，硬编码在 `addScore` 调用处，无配置、无实验记录。 |
| 4 | "用 function-calling / JSON mode / LangChain 输出结构化意图" | 是**在 prompt 里让模型自己吐 JSON**，然后 `sliceJsonObject`（第一个 `{` 到最后一个 `}`）+ Jackson + `parseIntent` 兜底。没有 JSON mode、没有 schema 校验、没有重试。 |
| 5 | "低置信度会多轮澄清" | 只有**一轮固定话术**，且条件是 `intent==OTHER && len>2 && conf<0.5` 三者同时满足。 |
| 6 | "路由是模型判的 / 是个分类器" | 是**规则化加权打分**：intent 归类 + 关键词命中数 + 实体命中，`routingReason` 里能看到三个分数原值。 |
| 7 | "复合问题用 Spring 线程池 / `@Async`" | 用 `CompletableFuture.supplyAsync` **且没传 executor** ⇒ `ForkJoinPool.commonPool()`。`@EnableAsync` 只服务 `MemoryManager` 的 `@Async`；**没超时、没隔离、没线程池指标**。 |
| 8 | "ESCALATION 有专门的兜底 Agent" | `AgentType` 有 `ESCALATION`、路由表也映射到它，但 `AgentConfig` **只注册了 GENERAL/TECHNICAL/BILLING** ⇒ `execute()` 回落 `GeneralAgent`。结果：`primaryAgent=escalation` 但 `agentType=general`。 |
| 9 | "monitor_penalty 会降低这个领域的路由优先级" | 它只进 `AgentStats.routingScore = base × (1-penalty)`，用于**同类型多实例择优**；**不参与** `domainScores`。而且每类型只有 1 个实例 ⇒ 目前无可观测影响。 |
| 10 | "routing_confidence 是路由概率 / 是三个 Agent 的融合置信度" | 是 **primary 的领域加权分**（`min(score,1.0)`，可 >1 后被截断）。澄清分支透出的是 `intentConfidence`，ESCALATION 用 `max(conf,0.8)`，CRITICAL 固定 1.0，无可用 Agent 固定 0.1——五种量纲混用一个字段。 |
| 11 | "意图有 4/5 类" | **19 个枚举**（含 `OTHER`）。其中 **18 个**有 LLM 示例模板（`OTHER` 不在 `TEMPLATES` 里）；**17 个**出现在 `domainScores` 的加分表里，`ESCALATION` / `HUMAN_HANDOFF` 这两个**不在**加分表、只走 `routeDecision` 的短路分支。 |
| 12 | "回答校验不通过会重新生成 / 会拦截" | 不会。只标注，`pass=false` 不影响返回文本，只让 `ChatResponse.verified=false`；唯一副作用是 `need_escalation` 会抬高 `escalated`。 |
| 13 | "Agent 内部会调工具/检索" | 不调。检索在 Controller（`KnowledgeToolManager.searchWithRewrite`），结果以 `context` 文本进 prompt；`AgentResponse` 的 tool 相关字段是占位（成功时 `toolName=""`、`toolSuccess=true`）。 |
| 14 | "转人工是模型结构化判断的" | 是**回答文本关键词嗅探**：`转人工`/`人工客服`/`escalate`/`specialist`/`无法处理`。最容易误升级。 |
| 15 | "有流式输出 / 有重试 / 有超时" | 三者**代码中均未找到**。`LlmGateway.chat` 是同步阻塞返回完整字符串。 |
| 16 | "AgentOrchestrator 里那个 `route(intent, urgency)` 是主路由" | 那是**死代码**（`AgentOrchestrator.java:158-167`，全文件无调用点）；实际主路由是 `routeDecision`。`collaborationTargets`（`:298-309`）同样是死代码。讲错会非常尴尬。 |
| 17 | "缓存是 LRU / 有 TTL" | 是 `ConcurrentHashMap`，`size() > 1000` 时 **`clear()` 全清**，无 TTL、无淘汰策略。 |
| 18 | "实体抽取包含商品名" | `product` **恒为 `List.of()`（空）**，代码中未找到实现；`error_code` 被 `put` 两次，第二次覆盖第一次（合并正则结果）。 |

---

## 8. 代码定位表（面试"我记得是在 Xxx 里"专用）

| 关键点 | 定位 |
|---|---|
| 置信度阈值 0.5 | `IntentRecognizer.java:25`（`CONFIDENCE_THRESHOLD`） |
| 18 个意图的示例模板 | `IntentRecognizer.java:27-46`（`TEMPLATES`） |
| 细粒度 / 粗粒度意图集合 | `IntentRecognizer.java:48-58` / `:60-66` |
| 细→粗意图组映射 | `IntentRecognizer.java:68-78`（`INTENT_GROUPS`）+ `intentGroup()` `:386-388` |
| 意图识别主流程（三路 → 投票 → 实体 → 缓存） | `IntentRecognizer.java:89-117`（`recognize`） |
| LLM 那一路（prompt 原文、temperature 0.1、256 tokens、失败兜底） | `IntentRecognizer.java:119-152`（`llmRecognize`） |
| 字符 n-gram Jaccard 那一路 | `IntentRecognizer.java:154-167`（`semanticRecognize`）+ `charNgrams` `:346-355` + `jaccard` `:357-366` |
| 关键词规则（细粒度 9 类 + 粗粒度 8 类） | `IntentRecognizer.java:169-198`（`patternRecognize`） |
| pattern 打分公式 `min(1.0, 0.5+0.25*(hits-1))` | `IntentRecognizer.java:200-214`（`bestPatternMatch`） |
| **权重 0.70/0.20/0.10 + refine 规则 + <0.5 降级** | `IntentRecognizer.java:216-251`（`vote`）+ `:253-257`（`addScore`） |
| 紧急度判定顺序 | `IntentRecognizer.java:314-327`（`urgency`） |
| 实体抽取正则 | `IntentRecognizer.java:263-275`（`extractEntities`） |
| 意图缓存（key 含最近 3 条 history、>1000 全清） | `IntentRecognizer.java:90-94, 112-116, 372-384` |
| 路由表（intent → AgentType） | `AgentOrchestrator.java:37-47`（构造函数） |
| 编排主流程（澄清 → 路由 → 串行/并行） | `AgentOrchestrator.java:54-109`（`run`） |
| 低置信度澄清三条件 + 固定话术 | `AgentOrchestrator.java:330-339`（`needsClarification`）+ `:62-79` |
| 路由决策（CRITICAL 短路 / ESCALATION / 打分 / supporting 阈值） | `AgentOrchestrator.java:169-211`（`routeDecision`） |
| **领域打分公式（0.55/0.75/0.18/0.12/0.45/0.35/0.2/0.15/0.1）** | `AgentOrchestrator.java:213-273`（`domainScores`） |
| routingReason 文本格式 | `AgentOrchestrator.java:275-296`（`routingReason`） |
| 并行派发 + 结果拼接 | `AgentOrchestrator.java:119-156`（`runParallel`） |
| Agent 实例择优 + 失败回落 GENERAL | `AgentOrchestrator.java:341-351`（`execute`）+ `:423-426`（`bestAgent`） |
| 死代码：`route()` / `collaborationTargets()` | `AgentOrchestrator.java:158-167` / `:298-309` |
| 打分明细 / 惩罚回写接口 | `AgentOrchestrator.java:428-443`（`stats`）/ `:446-452`（`updateRoutingPenalties`） |
| **routingScore 乘法公式** | `AgentStats.java:34-38`（`routingScore`）+ `:44-46`（`setMonitorPenalty` 夹紧 [0,0.9]） |
| penalty 计算（SR<0.9、avg>3000ms）与 10s 采集 | `PerformanceMonitor.java:92-101`（`routingPenalty`）/ `:51-81`（`collect`） |
| Agent 通用链路（temp 0.2 / 1024 tokens / 记 stats） | `BaseAgent.java:27-41`（`handle`） |
| prompt 三段拼接 | `BaseAgent.java:47-57`（`buildPrompt`） |
| 动态 Skills 注入 system prompt | `BaseAgent.java:59-68`（`buildSystemPrompt`）+ `SkillManager.java:74-96`（`promptFor`） |
| 转人工关键词嗅探 | `BaseAgent.java:70-77`（`needsEscalation`） |
| 三个 Agent 的 system prompt | `GeneralAgent.java:18-20` / `TechnicalAgent.java:18-20` / `BillingAgent.java:18-20` |
| 只注册 3 个实例（无 ESCALATION） | `AgentConfig.java:14-21`（`agentPool`） |
| 回答校验（prompt、temp 0.0、fallback） | `AnswerVerifier.java:21-45`（`verify`）+ `:47-48`（`VerificationResult`） |
| 校验调用点 + escalated 合成 | `PuddingController.java:133-134` |
| `AgentRequest` 字段与 `withIntent` | `AgentRequest.java:11-38` |
| `AgentResponse` 11 个字段 | `AgentResponse.java:3-15` |
| `OrchestratorResult` 13 个字段 | `OrchestratorResult.java:8-22` |
| `AgentType` 4 个值 | `AgentType.java:3-8` |
| `IntentCategory` 19 个值 | `IntentCategory.java:3-23` |
| `UrgencyLevel` 4 个值 | `UrgencyLevel.java:3-8` |

---

## 附录：真实常量速查卡（背这一页就够）

| 常量 | 值 | 位置 |
|---|---|---|
| 意图置信度阈值 | `0.5` | `IntentRecognizer.java:25` |
| 三路权重 | llm `0.70` / embedding `0.20` / pattern `0.10` | `IntentRecognizer.java:232-234` |
| n-gram 的 n | `1, 2, 3`（字符串拼接后去重做 Jaccard） | `IntentRecognizer.java:349` |
| pattern 打分 | `min(1.0, 0.5 + 0.25*(hits-1))` | `IntentRecognizer.java:206` |
| refine 门槛 | `patternConf ≥ 0.5` 且 `best < 0.8` | `IntentRecognizer.java:242-243` |
| 意图 LLM 调用参数 | temperature `0.1`、maxTokens `256`、system 空 | `IntentRecognizer.java:138` |
| 意图缓存上限 | `1000`（超出 `clear()`），key 取最近 `3` 条 history | `IntentRecognizer.java:112-116, 375` |
| 领域基础分 | GENERAL `0.1` / TECHNICAL `0.0` / BILLING `0.0` | `AgentOrchestrator.java:216-218` |
| intent 归类加分 | general 类 `+0.55` / technical 类 `+0.75` / billing 类 `+0.75` | `AgentOrchestrator.java:220-250` |
| 关键词加分 | TECHNICAL `min(0.45, hits*0.18)` / BILLING 同 / GENERAL `min(0.35, hits*0.12)` | `AgentOrchestrator.java:256-258` |
| 实体加分 | error_code `+0.2` / amount `+0.15` / order_id `+0.1` | `AgentOrchestrator.java:261-269` |
| supporting 阈值 | `≥0.45` 且 `≥ primary×0.55`，且 `≠ GENERAL` | `AgentOrchestrator.java:201-202` |
| routing_confidence | `round(min(primaryScore, 1.0))` | `AgentOrchestrator.java:209` |
| ESCALATION 路由置信度 | `max(intentConfidence, 0.8)` | `AgentOrchestrator.java:179` |
| CRITICAL 路由置信度 | `1.0` | `AgentOrchestrator.java:171` |
| 无可用 Agent 置信度 | `0.1` | `AgentOrchestrator.java:191` |
| 澄清条件 | `OTHER` 且 `trim().length() > 2` 且 `conf < 0.5` | `AgentOrchestrator.java:330-339` |
| routingScore | `(successRate*0.7 + (1/(1+avgMs/1000))*0.3) * max(0, 1-penalty)` | `AgentStats.java:34-38` |
| penalty 夹紧 | `[0.0, 0.9]` | `AgentStats.java:45` |
| penalty 公式 | `sr<0.90 → +min(0.5,(0.9-sr)*2)`；`avg>3000 → +min(0.4,(avg-3000)/10000)`；总 `min(0.9,·)` | `PerformanceMonitor.java:92-101` |
| monitor 采集周期 | `fixedDelay = 10000`（10 秒） | `PerformanceMonitor.java:51` |
| 监控阈值 | successRate `0.90`、latency `3000`ms | `PuddingProperties.java:147-148` |
| Agent LLM 参数 | temperature `0.2`、maxTokens `1024` | `BaseAgent.java:31` |
| Verifier LLM 参数 | temperature `0.0`、maxTokens `256` | `AnswerVerifier.java:30` |
| Skills prompt 上限 | 全局 `5000` 字符、单块 `min(3200, remaining)` | `PuddingProperties.java:190` / `SkillManager.java:81` |
| Agent 实例数 | GENERAL 1 / TECHNICAL 1 / BILLING 1（**ESCALATION 0**） | `AgentConfig.java:16-20` |
