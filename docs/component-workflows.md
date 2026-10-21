# 主项目组件互调与统一联调

所有操作由 Octop 的认证 API、租户与私有空间、冻结 DAG 和任务运行器协调。工作台的工作流编辑器中选择“组件操作”，即可将组件调用与本机节点、已有 agent/技能节点放在同一任务中；本机和 Temporal 模式使用相同的分发代码。操作目录为 `GET /api/longterm/component-operations`，需要登录，不返回连接地址或凭据。

| 组件 | 可编排的操作 | 运行约束 |
| --- | --- | --- |
| Octop | `local.echo`、既有 agent/技能分析节点 | 本机节点可检查数据流；agent 节点需要已配置模型 |
| BeatDesign | `project`、`canvas`、`assets`、`skills`、`generation_models`、`generation_history`、`editor`、`editor_diagnostics` | 使用当前租户独占绑定的项目和真实 MCP，只读 |
| RNA | `genes`、`observability` | 使用当前租户绑定的真实项目，只读 |
| Hindsight | `recall` | 只召回当前租户与私有空间中具有合格来源和有效期的记忆 |
| CommerceAgentBench | `report` | 从当前租户/空间下的相对目录读取真实或明确标记的调试产物；不会自动运行模型评测 |
| Semantica | `index` | 使用真实 SDK 索引本任务证据，需字面 `consent: true` 和当前冻结计划的 R1 审批 |
| Temporal | 整个 DAG 选择 `mode: temporal` | 由独立 worker 执行同一套组件节点，控制命令和审批仍通过主项目 |

BeatDesign 和 RNA 的项目绑定在“组件管理”中配置，需要全局管理员和租户 owner/admin 权限。创建任务时会冻结项目 ID、绑定版本及调用目标摘要；绑定或服务地址变化后，旧任务拒绝继续调用，应重新创建任务。模型密钥与组件令牌只由服务端读取，不进入同步配置或冻结计划。

## 将前序组件结果传给后序节点

节点的 `input` 可以包含 `{"$node":"前驱节点 ID","pointer":"/result/output/字段"}`。引用必须来自该节点的 `depends_on`，且前驱已成功完成。`pointer` 使用 JSON Pointer，数组下标从 0 开始，`~1` 表示 `/`、`~0` 表示 `~`；省略或空字符串表示整个成功结果包装。不能通过引用访问文件、环境变量或任意 URL。解析后的输入最多 128 KiB，缺失路径会在派发前明确停止任务。

例如，以下工作流将本机输入交给 Hindsight，再把真实召回结果交给后续节点：

```json
{
  "nodes": [
    {"id":"query","skill":"local.echo","risk":"R0","input":{"text":"已验证的任务经验"}},
    {"id":"memory","skill":"component.call","component_id":"hindsight","operation":"recall","risk":"R0","depends_on":["query"],"input":{"query":{"$node":"query","pointer":"/result/output/text"},"max_tokens":2048}},
    {"id":"readback","skill":"local.echo","risk":"R0","depends_on":["memory"],"input":{"memory":{"$node":"memory","pointer":"/result/output"}}},
    {"id":"index","skill":"component.call","component_id":"semantica","operation":"index","risk":"R1","depends_on":["readback"],"input":{"consent":true}}
  ],
  "max_cycles": 1,
  "max_parallel": 4
}
```

任务会在 `index` 等待审批。通过工作台批准当前等待节点后继续；API 调用使用 `/api/longterm/tenants/{tenant_id}/tasks/{task_id}/commands`，发送 `command: approve`、最新 `expected_revision`、`node_id: index` 与该任务 `plan_digest`。控制命令采用版本检查和幂等键；不要直接编辑数据库中的控制状态。

组件结果与本机/agent 结果一样写入 `checkpoint.node_results`，并记录 `action_intent`、`action_completed` 和证据哈希链。下游 agent 可消费这些实际结果；组件读取的模型用量为 0，不把 HTTP 成功或调试回显解释为经营目标已达成。跨组件关系由 DAG 决定，不开放任意 MCP 工具、远端命令或未经许可的写入。

CommerceAgentBench 节点输入为 `{"run_dir":"相对结果目录","expected_task_id":"可选的基准任务 ID"}`，目录位于结果根下的 `sha256(tenant_id)/space_id/`。Semantica 索引仍按租户和任务隔离，独立 Explorer 不会自动聚合所有租户的私有存证。Hindsight 保存/删除、RNA 提案与发布状态、评测报告导入继续使用工作台原有的显式操作。

## 一个 Compose 文件启动调试

源码目录、端口和首次登录见 [统一部署说明](../docker/README.stack.md)。在仓库根目录运行：

```sh
docker compose up --build --watch
```

Octop 和 worker 会同时同步 Python 与嵌入 SDK 源码并重启；前端及需编译的组件会自动重建。已有后台栈可运行 `docker compose watch --no-up --prune=false`。Hindsight/Temporal 服务端仍使用固定镜像；相关变更方式见部署说明。

调试时先在主项目检查组件连接，绑定 BeatDesign/RNA 项目，选择租户与私有空间，再创建本机或 Temporal 任务。通过主项目查看节点结果、审批、原始证据和索引回读；各原生控制台用于检查对应组件状态。真实模型、付费评测、生产 Temporal 与实际业务发布需要相应配置和独立验收。
