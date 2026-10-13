# 智能体（Agent）意图路由与分步执行方案

> 版本：V1.1（已确认并开发完成 P1+P2） · 2026-10-13
> 适用范围：智能问答（/v1/chat/stream）、公文起草（/v1/generate/stream）及后续所有涉及生成的链路
> 状态：**P1 意图路由 + P2 分步执行已开发并端到端验证通过**；P3 体验打磨随配置中心 Agent 设置页一并向运维开放
> 前序版本：V1.0（待确认稿，本文档取代之）

---

## 一、需求理解

用户原话："智能问答，公文起草这类所有涉及生成的功能，都应有 agent 概念，即我们应该首先判断用户意图（希望确切回答，还是完成复杂任务等），然后基于意图来分步骤来完成任务。"

四个设计目标：

| # | 目标 | 含义 |
|---|------|------|
| G1 | **意图先行** | 每次生成请求先判断用户想干什么，再决定执行策略，杜绝"一把梭"式检索+生成 |
| G2 | **分步执行** | 复杂任务拆解为有序步骤，逐步推进、逐步可见，而非一次黑盒输出 |
| G3 | **全链路覆盖** | 问答、起草及未来所有生成功能共用同一套 Agent 内核，不各写一套 |
| G4 | **不破底线** | 意图判断失败/超时/模型抖动时，必须降级为现有直答链路，绝不允许"agent 坏了就连问答都用不了" |

---

## 二、用户确认决策（2026-10-13，V1.0 待确认事项的拍板）

| # | 事项 | 决策 | 落实方式 |
|---|------|------|----------|
| 1 | 意图枚举是否认可 | **认可五类，但必须留足扩展可能** | `agent/intents.py` 注册表：新增意图 = `register_intent(IntentSpec(...))` 一行 + 执行面加一个策略函数，路由/编排骨架零改动；LLM 分类白名单同步补充即可 |
| 2 | LLM 分类模型 | 复用现有 gen 角色（qwen3.8-27b） | temperature=0、max_tokens=64 的轻量分类调用 |
| 3 | 步骤是否可见 | **可见，且 UI 要"简洁但内容具体"** | 新增 `AgentStepPanel.vue`：意图徽标 + 步骤时间线（状态图标/动作类型/描述/结论摘要）+ 步骤计数实时推进；流式结束后保留到下次发送可回看 |
| 4 | agent_mode 默认值 | **默认开启**，运维可一键关闭 | 请求级 `agent_mode` > 配置中心开关 > 代码默认 true；关闭后 meta 如实回报 `agent_mode:false`，行为与旧版一致 |
| 5 | 起草链路是否纳入 P1 | **纳入** | `/v1/generate/stream` 同样意图识别（显式文种→draft 强信号）+ 审计留痕 + 无材料兜底 |

### 额外需求（本次新增，硬性）

> "对于无法检索到有用/事实信息的情况：如果用户的意图为确切回答则委婉回答没有相关内容，不能编造；如果用户意图为完成任务（起草、写作、设计等）则应用自身能力帮用户完成任务，但需注明需用户自行甄别生成内容。"

落实为 `agent/policies.py` 的确定性策略（不依赖模型自觉）：

- **确切回答型（direct_qa）+ 无有用信息** → 固定话术委婉拒答（说明未检索到内容 + 给出调整空间/换关键词/补充入库三个建议），**不调用模型**，从机制上杜绝编造；
- **任务型（draft / complex_task）+ 无有用信息** → 任务不失败，切换"模型自身能力"提示词完成，答案末尾强制追加甄别声明（`TASK_DISCLAIMER`），done 事件带 `disclaimer:true`，前端 warning 提示；
- **"无有用信息"判定**（双信号同时异常才判定，防误杀）：引用为空，或（向量路 top1 余弦距离 > 0.60 **且** 关键词路零命中）。阈值标定：主题相关查询实测 ≈0.28，无关查询 ≈0.64~0.70；单信号弱异常时放行给模型（提示词纪律兜底"依据不足"）。

---

## 三、总体架构（已实现）

```
                 ┌────────────────────────────────────────────┐
                 │               Agent 内核（新增）            │
                 │                                            │
  用户请求 ─────▶│  ① 意图路由器 IntentRouter                  │
                 │      ├─ 规则通道（关键词/正则/长度/标点）    │
                 │      └─ LLM 通道（gen 模型分类，3s 超时）    │
                 │                    │                       │
                 │                    ▼                       │
                 │  ② 计划器 Planner（仅复杂任务）             │
                 │      任务 → 步骤列表（子问题/章节/校验）     │
                 │                    │                       │
                 │                    ▼                       │
                 │  ③ 执行器 Executor                         │
                 │      每步复用 hybrid_retrieve / ModelProvider│
                 │                    │                       │
                 │                    ▼                       │
                 │  ④ 汇总器 Aggregator → 脱敏 → 落库          │
                 └────────────────────────────────────────────┘
                                      │
        SSE：meta(+intent) → plan → step_start/step_end → token/reasoning → done(+disclaimer)
```

**Agent 是"编排层"，不是"新引擎"**：检索（hybrid_retrieve）、生成（ModelProvider）、脱敏（redact_sensitive）、权限下推（build_visibility_filter）全部复用；每一步检索都带权限/密级/空间过滤下推，敏感词脱敏只在最终输出执行。

### 3.1 意图分类体系（已实现）

| intent | 说明 | 执行策略 | 无有用信息时 |
|--------|------|----------|--------------|
| `direct_qa` | 确切回答 | direct_answer：检索 + 直答 | **固定话术委婉拒答，不编造** |
| `complex_task` | 复杂任务（比较/汇总/排查/多条件） | step_plan：拆步 → 分步执行 → 汇总 | 自身能力完成 + 甄别声明 |
| `draft` | 公文起草 | 问答场景按直答处理；/v1/generate 走起草策略 | 自身能力起草 + 甄别声明 |
| `chitchat` | 寒暄闲聊 | 固定话术即时回应（不走检索） | — |
| `out_of_scope` | 超出能力边界 | 固定话术拒答 + 能力边界说明 | — |

### 3.2 意图路由（已实现）

规则通道（优先级从高到低）：显式文种 → 起草动词 → 复杂任务信号（比较/汇总/排查/多问句）→ 寒暄模式 → LLM 兜底（3s 超时，输出 JSON `{"intent","confidence"}`，不可解析/未注册 → direct_qa）。

### 3.3 分步执行（已实现）

- Planner：规则模板（比较类拆分双方 / 汇总类 / 排查类）→ LLM 兜底（JSON 步骤列表）→ 失败退化为单步检索（汇总器负责最终回答，等价直答）；步骤硬上限 6 步（可配置 1-12）；
- Executor：逐步执行（retrieve / generate 两类动作），单步失败重试 1 次后标记 failed 并继续；步骤间注入前序产物摘要（截断 500 字/步）；无有用信息的检索步不回传引用（走兜底路径）；
- Aggregator：合并去重引用 + 失败步骤声明"依据不足" + 汇总流式生成。

---

## 四、改动清单（已落地）

### 后端新增

| 文件 | 职责 |
|------|------|
| `app/services/agent/intents.py` | 意图枚举 + 可扩展注册表（register_intent / get_spec / all_specs） |
| `app/services/agent/config.py` | Agent 运行时配置（ConfigKV category=agent，缺省代码默认值） |
| `app/services/agent/router.py` | 意图路由器（规则 + LLM 双通道，3s 超时降级） |
| `app/services/agent/planner.py` | 计划器（规则模板 + LLM 兜底 + 单步降级） |
| `app/services/agent/executor.py` | 执行器（异步生成器，yield step_start/step_end 事件） |
| `app/services/agent/aggregator.py` | 汇总器（messages 拼装 + 引用合并 + 失败声明） |
| `app/services/agent/policies.py` | 话术策略（拒答/寒暄/超范围/甄别声明）+ 无有用信息双信号判定 |
| `app/services/agent/__init__.py` | 包入口（编排层唯一依赖面） |

### 后端修改

| 文件 | 改动 |
|------|------|
| `app/services/chat.py` | 重写为 Agent 编排层：意图分发（_CHAT_STRATEGIES）+ 五策略 + 统一收尾（脱敏/落库/done）；meta/plan/step_*/done 事件扩展 |
| `app/services/generation.py` | 起草链路意图识别 + 审计 + 无材料兜底（提示词切换 + 甄别声明）；非流式 generate_answer 同步 |
| `app/services/generation_prompt.py` | build_generation_messages 增加 no_materials 模式 |
| `app/services/retrieval.py` | RetrieveResp 增加 top_vector_distance / keyword_hits 判定信号 |
| `app/schemas/contracts.py` | ChatRequest/GenerateRequest 增加 agent_mode；RetrieveResp 增加两个信号字段 |
| `app/services/audit.py` | record_audit 支持显式 action（意图识别/任务计划）；actor_from_request 下沉 |
| `app/api/routes/chat.py` `generate.py` | 透传 actor（意图识别审计用） |
| `app/main.py` | 中间件改用 actor_from_request（去重） |

### 前端

| 文件 | 改动 |
|------|------|
| `src/api/client.ts` | PlanStep/AgentMeta/DoneExtra 类型；onPlan/onStepStart/onStepEnd 事件；onMeta/onDone 扩展参数；agent_mode 入参 |
| `src/components/AgentStepPanel.vue` | **新增**：Agent 执行过程面板（意图徽标 + 步骤时间线 + 状态提示），Chat/Draft 共用 |
| `src/views/Chat.vue` | 接入 Agent 面板（实时步骤状态 + 保留回看）、无材料/甄别提示 |
| `src/views/Draft.vue` | 意图徽标 + 无材料甄别提示（纸面 warn-chip） |
| `src/views/Config.vue` | **新增「Agent 设置」页签**：总开关 / LLM 分类兜底 / 分类超时 / 步骤上限，保存即生效 |

---

## 五、SSE 事件协议（向后兼容）

```
meta    {conversation_id, model, intent, intent_label, agent_mode, no_result, citations, truncated_by_security}
plan    {steps:[{id, action, query, status}], channel}        仅 complex_task
step_start  {id, action, query}                               仅 complex_task
step_end    {id, ok, summary}                                 仅 complex_task
token / reasoning                                             正文与思考链
done    {conversation_id, answer, sensitive_hits, model, intent, no_result, disclaimer}
error   {message}
```

老客户端忽略不认识的 `type` 即可（switch default 分支），**接口不破坏**。

---

## 六、降级与容错矩阵（已实现）

| 故障点 | 现象 | 降级策略 |
|--------|------|----------|
| 规则+LLM 分类都失败 | intent 无法确定 | 默认 `direct_qa`，体验与旧版一致 |
| LLM 分类超时（>3s） | 分类慢 | 放弃分类，走 `direct_qa`（不等） |
| Planner 输出不可解析 | 步骤列表坏 | 退化为单步检索 + 汇总直答 |
| 某一步执行失败 | 步骤 failed | 重试 1 次 → 跳过该步继续，汇总时声明依据不足 |
| 汇总生成失败 | 最终无答案 | 推送 error 事件；已落库部分仍在会话中可查 |
| agent_mode=false / 配置关闭 | 运维关闭 Agent | 完全走现有直答链路（meta 如实回报 agent_mode:false） |
| 无有用检索信息 | 语料没这东西 | direct_qa 固定话术拒答；任务型自身能力完成 + 甄别声明 |

---

## 七、验证记录（2026-10-13，dev 环境实测）

| 场景 | 输入 | 结果 |
|------|------|------|
| 复杂任务分步 | "对比默认空间和安全空间的制度差异" | plan 3 步（rule）→ step 事件带摘要 → 汇总答案表格化、逐项标注"依据不足"，不编造 |
| 确切回答·有材料 | "跨网传输系统的审批策略是怎样的？" | direct_qa，答案命中知识库真实内容（密级匹配审批策略） |
| 确切回答·无有用信息 | "员工食堂饭卡去哪里充值？" | no_result=true，固定话术委婉拒答 + 三条建议 |
| 超范围 | "今天天气怎么样" | LLM 通道判定 out_of_scope，固定话术拒答 + 能力边界 |
| 闲聊 | "你好" | chitchat 固定话术，即时响应不走检索 |
| 任务型无材料 | 起草"元宇宙虚拟年会通知" | disclaimer=true，通用能力成文 + 末尾甄别声明 |
| 任务型有材料 | 起草"三季度安全生产大检查通知" | 正常带引用起草，disclaimer=false |
| 多轮 | 复杂任务首轮后追问"那第三方同步接口的权限怎么控制？" | 同会话 direct_qa，结合历史作答 |
| 开关回退 | agent_mode=false | 无 plan/step 事件，meta agent_mode:false，走旧直答链路 |
| 配置中心 | enabled=false → true | 实时生效（关闭时 intent=direct_qa/disabled 通道；恢复后 chitchat 正常） |
| 审计 | — | 「意图识别」「任务计划」动作分类落账，可按意图统计 |

---

## 八、分期实施状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| **P1 意图路由** | 规则通道 + LLM 兜底 + intent 进 SSE/审计 + 配置开关 | ✅ 完成 |
| **P2 分步执行** | Planner/Executor/Aggregator + plan/step_* 事件 + 前端步骤时间线 | ✅ 完成 |
| **P3 体验打磨** | 配置中心 Agent 设置页（开关/超时/步骤上限可调） | ✅ 随 P2 交付 |
| 后续可选项 | 新意图扩展（注册表机制已就绪）；按意图的用量统计报表；verify 步骤类型 | 预留 |

---

*V1.0 为待确认稿；本版本记录用户拍板决策与最终落地实现，作为现行文档。*
