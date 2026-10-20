# 组件进程、控制台与可信部署清单

组件的集成配置和进程生命周期是两项独立操作。“启用”允许 Octop 使用该适配器；保存连接参数、运行连接检查或设置 `OCTOP_COMPONENT_RUNTIME_FILE` 都不应自动启动服务、提交模型请求或执行评测。停用集成也不等于取消已经提交的任务，停止 worker 也不等于停止 Temporal 中的工作流。

默认使用本机嵌入能力或已独立部署的外部服务。只有运维人员在可信清单中显式登记的 `process` / `compose` 条目，才供系统管理员执行进程启停。普通租户继续通过 Octop 的租户、空间和项目授权访问集成功能。

## 默认清单与安装位置

[安全默认示例](../docker/component-runtime.example.json) 不登记任何可启动命令。将它复制到部署配置目录，审阅后按需添加条目，再给 **Octop 服务进程** 设置该文件的绝对路径：

```powershell
$env:OCTOP_COMPONENT_RUNTIME_FILE = 'G:/deploy/octop/component-runtime.json'
uv run octop run
```

清单使用 JSON，顶层为 `{"version":1,"components":{...}}`。组件标识为 `octop`、`semantica`、`beatdesign`、`commerceagentbench`、`rna`、`hindsight`、`temporal`。未登记的组件沿用其嵌入或外部接入方式。

`mode` 支持 `embedded`、`external`、`process`、`compose`。前两种不授予进程启停能力：`embedded` 表示随宿主使用的 SDK/本机能力，`external` 表示由其他运维系统管理的独立实例。示例中 CommerceAgentBench 的 `embedded` 只表示 Octop 内的结果读取适配器，评测 harness 仍由独立 CLI 运行。清单首次读取后会缓存，修改文件后需要协调重启 Octop；修改任务连接参数仍需另行协调对应 worker。

系统管理员可从 `GET /api/longterm/integrations/runtime` 查看 `mode`、`state`、`enabled`、`revision`、允许的动作、服务状态和控制台链接。`enabled` 是集成功能开关；`process` / `compose` 的 `state` 表示观测到的进程或容器状态，两者可以不同。原控制台链接存在也不等于服务已经健康。

`POST /api/longterm/integrations/{component_id}/lifecycle` 只接受 `action`（`start` / `stop`）、布尔值 `confirmed:true` 和最新的 `expected_revision`。嵌入/外部模式的动作只切换本机 `enabled`，不会启停远端服务器；托管模式只操作可信清单中的进程或服务。并发操作或过期版本返回 409，先刷新状态再决定下一步。操作前复核当前管理员权限，记录请求与结果审计；命令原始输出不返回到 HTTP 或审计正文。

清单、Compose 文件、启动脚本及其父目录应只允许运维账户写入；租户上传目录、同步空间、评测产物和模型可写目录都不适合存放它们。修改清单具有在 Octop 服务账户下执行代码的权限，不能由 HTTP 请求提供命令、可执行文件、cwd、环境变量或 Compose 路径。

以下路径均为示例，必须换成部署机器上的真实绝对路径。Windows JSON 可以使用 `G:/...`；使用反斜杠时需写成 `G:\\...`。容器中的路径必须在容器内存在，不能直接使用宿主机盘符。建议将 `argv[0]` 固定到已审阅的可执行文件绝对路径，避免服务账户的 PATH 变化改变执行内容。

## 显式托管 Temporal 开发服务

现有 [长程 Compose](../docker/docker-compose.longterm.yml) 提供固定镜像和数据卷。只登记 Temporal 服务的清单为：

```json
{
  "version": 1,
  "components": {
    "temporal": {
      "mode": "compose",
      "compose_file": "G:/dev/agents/Octop/docker/docker-compose.longterm.yml",
      "project_name": "octop-longterm",
      "services": ["temporal"],
      "profiles": ["temporal-server"],
      "console_url": "http://127.0.0.1:8233"
    }
  }
}
```

这只登记管理员可操作的目标，不表示服务已经启动。宿主机需安装可用的 Docker CLI、Compose 插件和 Docker daemon；清单引用的文件应在该账户下可读。原 Compose 的变量检查还要求部署环境提供 `OCTOP_LONGTERM_DB_PASSWORD`，即使当前只操作 Temporal 服务也需要满足整个文件的解析条件。不要把真实密码写进清单、命令行或版本库。

服务启动后，Temporal gRPC 为 `127.0.0.1:7233`，Web 为 `127.0.0.1:8233`。这是带本地数据库文件的 `server start-dev`，适合本机开发；生产服务使用独立 Temporal 集群、命名空间、认证和备份。`worker` 是另一个进程，按 [长程部署说明](../docker/README.longterm.md) 启动并与 Octop 共用控制库；它没有独立网页。

`services` 和 `profiles` 必须是固定允许清单。不要把整个项目的 `up`、`down` 或 `down -v` 当作单组件操作；停止组件应保留数据库、日志与命名卷。Compose 的 `depends_on` 仍可能启动依赖，登记前应审阅完整文件。

## RNA 的可选本机进程

RNA 默认继续使用 SDK 或独立外部实例。只有确实需要管理其本机控制台时，才将下面条目合入 `components`：

```json
{
  "rna": {
    "mode": "process",
    "argv": ["C:/Program Files/nodejs/node.exe", "server.mjs"],
    "cwd": "G:/dev/agents/rna_dev/prototypes/control-center",
    "env": {
      "RNA_AGENT_PORT": "4317",
      "RNA_AGENT_STATE": "G:/deploy/octop/rna-instance/state.json"
    },
    "console_url": "http://127.0.0.1:4317"
  }
}
```

需要 Node ≥22.19.0。先在 RNA 的 `web` 目录执行 `npm ci`、`npm run build` 准备控制台；未构建时 daemon 的 API 仍可运行，网页只提示构建说明。状态目录应由运维预先准备并授权给服务账户，不要指向用户已有的 RNA 状态文件。端口和 `console_url` 应同时修改。

RNA 的标准 `node server.mjs` 入口启用调度、主动协作、后台学习和空闲压缩。加载已有真实项目后，启动可能恢复模型任务并产生费用；本示例没有默认登记它。Octop 的候选生成功能与 RNA daemon 的后台运行是不同能力。不要为“只检查连接”而启动 daemon 或执行候选生成。

`process` 应运行前台进程，`argv` 是独立参数数组。不要加入 `cmd /c`、PowerShell 拼接、`sh -c`、`start`、`nohup` 或另一个自脱离守护程序。进程管理器必须保留归属；端口有响应不能证明对应进程由本管理器启动。

正常关闭 Octop 控制器时，会停止所属本机进程；Windows 的进程树停止使用强制终止，不能据此承诺 RNA 的优雅排空。只有本管理器确认完成停止后，才将进程标记为 `stopped`。父进程自行退出、记录来自另一个控制器实例或重启前的进程时，状态为 `unknown`，系统拒绝猜测 PID、终止未知进程或重复启动。自脱离的启动器及逃离所属进程组的后代不在本机进程模式的托管范围内。由运维先核实进程身份、端口和状态，再按正式恢复流程处理；不要直接编辑运行中的控制库。多级应用和正式长期部署优先使用 Compose 或独立服务管理器，开发验收可使用新的专用测试数据目录。

## 各组件的启动与控制台边界

| 组件 | 接入和部署入口 | 控制台与说明 |
| --- | --- | --- |
| Octop | 当前 Octop API 与本机运行时 | 使用当前工作台；不能从自身组件按钮终止宿主 Octop 进程。 |
| BeatDesign | 仓库根先 `pnpm install`、`pnpm db:push`；`pnpm dev:agent` 启动 Web 与 HTTP MCP | Web `http://127.0.0.1:3020`；MCP `http://127.0.0.1:3031/mcp`。Node ≥22、pnpm 10；`BEATDESIGN_DATA_DIR` 可分离本机数据。该入口可能复用已有 Web，停止时不能误杀用户原进程。 |
| Semantica | Octop 按租户使用 `ProvenanceManager` 派生 SQLite 索引；独立 Explorer 可用 `python -m uvicorn semantica.explorer.app:app --host 127.0.0.1 --port 8000` | Explorer `http://127.0.0.1:8000`，健康 `/api/health`。需要 explorer 依赖和前端资源；单实例可用 `SEMANTICA_PROVENANCE_DB` 指向一个租户的索引，默认不会读取 Octop 的索引。 |
| CommerceAgentBench | 独立 CLI `commerce-agent-bench list` / `run`；执行环境需要 Python ≥3.11 和 Linux amd64 Docker harness | 没有常驻管理服务。运行产物中的 `report.html` 是静态报告，Octop 提供经过空间授权的导入和查看入口；启动组件不能自动运行基准或 LLM judge。 |
| RNA | 嵌入 SDK，或上述专用 Node daemon | Web `http://127.0.0.1:4317`，健康 `/api/health`；标准启动可能恢复后台任务。 |
| Hindsight | 上游 standalone Docker 镜像，或外接 PostgreSQL 的独立服务 | API `http://127.0.0.1:8888`，健康 `/health`；Web `http://127.0.0.1:9999`。standalone 数据卷挂载 `/home/hindsight/.pg0`；retain 可能调用模型。 |
| Temporal | 上述 Compose 服务，或独立生产集群；Octop 使用 Python SDK 和独立 worker | 开发 Web `http://127.0.0.1:8233`。SDK 可用、服务健康、worker 轮询和任务结果应分别验收。 |

上游 Semantica Compose 虽列出 FalkorDB，当前 Explorer 的 `GraphSession` 仍使用内存 `ContextGraph`，配置该环境变量不会自动获得图持久化。`SEMANTICA_PROVENANCE_DB` 只绑定一个 provenance 文件；不能向互不信任租户开放共用的全局 Explorer。SQL 原始证据链仍由 Octop 保存。

## 原控制台认证与多实例部署

`console_url` 是运维审阅后的固定 HTTP/HTTPS 地址，不是凭证传递通道。URL 不能包含用户名、密码、查询参数或 fragment。清单中的地址优先于页面配置；没有清单地址时可使用页面保存的合规地址。打开原控制台仍需完成该组件自己的登录或 API-key 配置，Octop 登录不会自动成为上游单点登录。Semantica 受保护路由需要 `SEMANTICA_API_KEY`；默认未配置时拒绝访问。匿名模式仅适合受控本机环境。

BeatDesign、RNA 和其他本机工作台具有全局状态或单用户假设。系统管理员的控制台链接不应下放给普通租户，项目绑定也不能替代上游 RBAC。远端访问应通过已认证的反向代理、TLS 和上游认证；远端浏览器中的 `127.0.0.1` 指向浏览器所在机器，不能照抄本机 URL。

多实例优先使用单独的 Compose 文件/项目，分别配置镜像摘要、命名卷、数据目录、端口、服务账户和凭证。避免上游固定 `container_name` 造成冲突；固定 project name 也不能隔离显式外部卷和共用宿主挂载。每个受管实例只交给一个生命周期控制器，避免多个 Octop API 副本同时管理相同进程或 Compose 项目。

容器版 Octop 默认镜像没有同时提供所有上游组件和宿主机进程管理环境。宿主 Node、Docker CLI、环境变量和源码路径不会自动进入容器。为容器挂载 Docker socket 会赋予控制宿主 Docker 的高权限，应作为单独的运维部署决定；本示例不要求默认挂载它。

清单的 `env` 只保留审阅后的非敏感配置。模型密钥由受保护的服务环境或部署机密机制提供，避免写进 JSON、浏览器同步空间和启动日志。部署时还应核实继承环境是否包含不应传给该组件的凭证。

## 静态核对与实际验收

可以先做不启动服务的 JSON 语法检查：

```powershell
uv run python -m json.tool G:/deploy/octop/component-runtime.json > $null
```

准备 Compose 所需部署变量后，执行 `docker compose -f <绝对路径> --profile temporal-server config --quiet` 检查编排。语法检查不证明 Docker、模型、控制台认证或业务目标可用。

真实验收应分别记录管理员主动启动、当前状态、连接检查、上游控制台认证、停止与再次启动、持久数据恢复，以及外部已运行实例不被误停的结果。进程存活、HTTP 健康、worker 轮询和业务任务达成应分别展示；缺少实际验证的项目保持未知。
