# 组件结果回填与 Agent 评测

工作台将组件操作会话、长程任务和评测中心放在同一租户与私有空间中。组件卡片、工作流中的组件节点、任务节点结果均可进入操作；BeatDesign/RNA 项目读取和旧组件操作结果也可以转入会话。

## 从组件返回主项目

1. 选择租户、私有空间，在组件或节点旁进入操作并创建会话。服务端冻结组件部署及项目绑定；需要任务的操作必须选择本空间任务。
2. 通过会话入口打开组件控制台，操作后返回原工作台。原生控制台保留自己的认证方式；全局控制台只向当前全局管理员提供。
3. 点击读取最新结果。主项目通过白名单适配器读取当前结果，生成可预览的候选及摘要。手动导入 JSON 始终标记 `manual_unverified`、`verified: false`。
4. 核对结果并确认保存。接受请求必须携带当前 revision、幂等键和候选摘要。任务关联会话同时写入 `component.result.committed` 存证，不推进任务版本。
5. 选择 JSON Pointer 片段并回填本机工作流草稿，可追加本机输入节点或确认替换既有节点输入。单次草稿输入限制 128 KiB，拒绝凭据。保存会话和回填草稿是两个明确动作。

原生网站没有通用可信回调协议，因此这里使用“返回后读取”的方式，避免让外部页面携带主项目令牌。结果会话可重新打开；回填不会修改运行任务的冻结 DAG、审批或 checkpoint。`verified` 表示结果来自受控读取，不能据此证明业务目标达成，也不能直接成为 RNA 发布依据。

| 组件 | 会话读取内容 |
| --- | --- |
| Octop | 本空间任务或实际节点结果 |
| BeatDesign | 已绑定项目、画布、资产、技能、生成模型/历史、编辑器与诊断 |
| RNA | 已绑定项目的基因与可观测记录 |
| Hindsight | 本租户及私有空间的有效记忆召回 |
| CommerceAgentBench | 本空间相对结果目录中的报告 |
| Semantica | 当前任务已导出的回执与证据摘要；索引写入仍走原审批 |
| Temporal | 服务端派生的当前任务 workflow 状态 |

API 目录为 `GET /api/longterm/operation-session-operations`，私有会话位于 `/api/longterm/tenants/{tenant_id}/spaces/{space_id}/operation-sessions`，支持创建、历史、读取以及 `readback/import/commit/cancel`。组件关闭、绑定更换或成员撤权会阻止后续操作；同一用户的既有历史不会因绑定更换而丢失。

## 评测设计依据

参考课程为 [《Agent 评测工程实战》](https://www.aiaitutor.com/#/course?courseId=371345979816939520)。本次读取范围是公开第一章和十章目录；后续正文要求登录，未将未读取内容表述为课程结论。第一章的重复运行、至少一次成功与全次成功分列、结果与过程共同评分及能力集毕业进入回归集，落为以下实现；通过失败规则和轨迹支持人工定位错误，尚未自动标注首个错误。

| 课程章节目录 | 项目对应能力 |
| --- | --- |
| 认知：体系化评测 | 运行结果、发布门禁、失败原因分列；单次成功不能代替可靠性 |
| 环境：评测实验室 | 根 Compose 同时启动主项目、worker、数据库与各组件 |
| 建模：评测对象 | 版本化套件、冻结被测工作流与评分器、每例独立派生任务 |
| 指标：指标框架 | 用例宏平均、重复成功率、pass@k、覆盖率与未知结果界限 |
| 数据：数据集 | 用例 ID、输入、标签、风险、能力/回归类型与不可变版本 |
| 评分：评分器工程 | 有限规则评分；结果断言、实际操作轨迹、耗时和用量约束 |
| 归因：Badcase | 失败评分器、证据引用、人工根因与修复记录 |
| 优化：改进修复 | 原分保持不变，通过新运行验证修复 |
| 闭环：门禁机制 | 可比运行对照、逐用例退化检查、显式进入回归集 |
| 落地：组织实施 | 租户权限、私有空间、版本、运行历史及可复查证据 |

除第一章外，上表是依据目录制定的项目实现映射，未声称复现登录后课程的具体方法。

## 原生执行与外部基准

原生评测为每个 case × repeat 创建独立长程任务，使用已有授权、预算、审批、证据链和本机/Temporal 运行器。用例、被测工作流、评分器在创建运行时冻结。默认合同套件用 `local.echo` 检查评测链路，不调用模型；只有显式选择真实被测任务后才评估对应 agent/组件。执行状态、规则评分结果和业务 `goal_outcome` 分开；没有独立业务证据时目标仍是 `inconclusive`。

运行轨迹记录实际操作标识、输入摘要、实际开始/完成时刻、耗时、结果或错误类型及用量。并行节点在各自执行协程中计时。`agent.analyze` 的外层节点轨迹不等于模型内部工具轨迹；没有记录的必要工具步骤不能通过过程评分。

CommerceAgentBench 当前上游 harness 面向 OpenClaw。外部报告保留 `external_import` 来源与 harness 信息，与 Octop 原生多 agent 运行分列；mock judge 标为 synthetic。仓库中的基准任务及验证器用于评测目录与方法参考，不把外部成绩包装成 Octop 的原生成绩。真实 CommerceAgentBench 执行、模型与付费 judge 需要用户配置和显式发起，Compose 健康检查不会触发这些任务。

统计使用预分配的完整用例/重复矩阵作为分母。先在每个用例内聚合，再对用例取宏平均；至少一次成功率与所有重复成功率分列。pass@k 使用无放回组合公式，缺失、基础设施错误或未验证结果不能当作通过。对照要求相同套件与评分器版本，并检查配对、覆盖和逐用例退化；合同或 synthetic 结果不能批准生产 RNA 发布。

评测 API 根路径是 `/api/longterm/tenants/{tenant_id}/evaluations`，套件、运行、对照、失败用例和 CAB 导入分别为 `suites`、`runs`、`comparisons`、`badcases`、`imports`。请求使用主项目登录令牌；创建运行需要明确 `space_id` 和幂等键。运行详情保存预分配的 attempts，每个原生 attempt 都可读取其任务和真实轨迹。

例如创建一个结果与过程共同评分的套件，向 `suites` 提交以下 JSON，并将 `space_id` 换为自己的私有空间 ID：

```json
{
  "space_id": "your-space-id",
  "name": "输入传递合同",
  "split": "capability",
  "expected_revision": 0,
  "cases": [{
    "case_id": "echo-message",
    "title": "正确结果及必要步骤",
    "goal": "返回指定内容",
    "input_overrides": {"echo": {"message": "expected"}},
    "graders": [
      {"id": "answer", "type": "json_equals", "path": "/nodes/echo/result/output/message", "expected": "expected"},
      {"id": "process", "type": "required_operation", "operation": "local.echo", "safety": true}
    ]
  }]
}
```

评分器白名单为 `json_equals`、`contains`、`required_operation`、`forbidden_operation`、`execution_status`、`max_duration_ms`、`max_tokens`，不执行上传脚本或任意代码。结果路径从 `nodes/task/trace/metrics` 评分包装中取值。组件过程操作名称格式是 `component.<组件ID>.<操作>`；必要操作必须有实际成功记录，禁止操作只要出现即失败。真实模型内部工具若没有合格记录，不能按外层节点名称伪造为已观察到。

默认内置被测对象和纯本机 echo 来源标为 synthetic。选择 `subject: {"kind":"source_task","task_id":"..."}` 时复用该用户、同私有空间任务的冻结工作流；评测不启动或修改这个原任务，而是为各次执行新建派生任务。改变套件生成新版本，旧运行继续指向原始版本。失败用例的人工根因和修复引用不会改写原成绩；指定成功的同套件同用例重评后才能验证修复，再显式复制到回归套件。

## 本地调试

在仓库根执行 `docker compose up -d --build`，或前台 `docker compose up --build --watch`。组件源码、端口、凭据读取和已有后台栈的 watch 用法见 [统一 Compose 说明](../docker/README.stack.md)。先在主项目配置组件、绑定项目，再进入工作台的评测中心；先运行默认合同套件核对链路，再创建有明确目标与评分规则的业务套件。

另开调试项目时，需同时设置 `COMPOSE_PROJECT_NAME` 和全部发布端口，避免与已有栈冲突。不要通过删除数据卷修复评测失败。每次评测都保留自己的冻结版本、派生任务与评分证据；生产效果需使用真实模型和业务样本单独验收。
