# MUSAE 统一容器栈

在 Octop 仓库根目录运行：

```sh
docker compose up -d --build
docker compose ps
docker compose exec octop cat /run/stack-secrets/admin-password
```

打开 <http://localhost:8088>，默认管理员用户名 `admin`，密码由最后一条命令读取。首次构建需要下载镜像和依赖，Hindsight 还需初始化本地模型与数据库。所有服务均有健康检查；`init` 正常状态是 `Exited (0)`。不需要先填写 `.env` 或分别启动七个项目。

需要 Docker Engine 26+（后台容器管理使用 API v1.45）、Compose v2.24+（支持 BuildKit named contexts）和以下源码目录。当前工作区已经具备这些目录；首次克隆 Octop 时，先将对应源码准备在其同级目录，也可以用环境变量指向已有目录。构建只显式复制运行源码与锁文件，不复制上游 `.env`、本地状态或 `node_modules`。

| 默认相对目录 | 可覆盖变量 | 本次集成版本 |
| --- | --- | --- |
| `../BeatDesign` | `BEATDESIGN_SOURCE` | `d2ef270174426da119574e9aa096e86edb777b5d` |
| `../rna_dev` | `RNA_SOURCE` | `d99dcdc86b66a56be92f97bdd89c50639f51c962` |
| `../semantica` | `SEMANTICA_SOURCE` | `ca6c9f1918ad05e2bbaa4922fb32f34e2a508bb9` |
| `../CommerceAgentBench` | `BENCHMARK_SOURCE` | `d38bd1711aa3926653452458a1be02c1efd12930` |

这些仓库分别来自 [BeatDesign](https://github.com/BeatAPI/BeatDesign)、[RNA](https://github.com/xfstudio/rna_dev)、[Semantica](https://github.com/semantica-agi/semantica) 和 [CommerceAgentBench](https://github.com/Accio-org/CommerceAgentBench)。RNA 私有仓库的访问权限由部署人员在宿主机准备，不将 Git 凭据传进镜像。要获得相同源码，应检出表中的提交；本地源码改动会参与镜像构建。

根目录的 `docker-compose.yml` 同时构建并启动 Octop、PostgreSQL/pgvector、Temporal、Temporal worker、BeatDesign Web/MCP、RNA 控制中心、Semantica Explorer、Hindsight API/UI、CommerceAgentBench 评测运行环境与报告控制台。Octop 和 worker 共享 PostgreSQL 和配置卷，应用数据库角色为 `NOSUPERUSER NOBYPASSRLS`。旧的 `docker/docker-compose*.yml` 保留为独立部署选项，不需要叠加使用。

| 控制台/接口 | 默认宿主端口 | 端口变量 | 登录 |
| --- | --- | --- | --- |
| Octop 工作台及后台 | 8088 | `MUSAE_PORT` | Octop 管理员 |
| Temporal UI / gRPC | 8233 / 7233 | `MUSAE_TEMPORAL_UI_PORT` / `MUSAE_TEMPORAL_PORT` | 本机开发控制台 |
| BeatDesign Web / MCP | 3020 / 3031 | `MUSAE_BEATDESIGN_PORT` / `MUSAE_BEATDESIGN_MCP_PORT` | Web Basic；MCP Bearer |
| RNA 控制中心 | 4317 | `MUSAE_RNA_PORT` | Basic 或 Bearer |
| Semantica Explorer | 8800 | `MUSAE_SEMANTICA_PORT` | Basic 或 Bearer / X-API-Key |
| Hindsight UI / API | 9998 / 8888 | `MUSAE_HINDSIGHT_UI_PORT` / `MUSAE_HINDSIGHT_API_PORT` | Basic 或 Bearer |
| CommerceAgentBench 报告 | 8091 | `MUSAE_BENCHMARK_PORT` | Basic 或 Bearer |

组件控制台的 Basic 用户名均为 `admin`，密码为对应组件的独立令牌。例如读取 BeatDesign 令牌：

```sh
docker compose exec octop cat /run/stack-secrets/beatdesign-token
```

其余文件名为 `rna-token`、`semantica-token`、`hindsight-token`、`benchmark-token`。Octop 在进程启动时从只读卷加载自己的集成凭据，HTTP 响应与运行日志不输出令牌。初始化器只在凭据不存在时生成；容器重建复用已有值，发现副本冲突会拒绝启动。管理员在应用内改密后，以应用内新密码为准，文件只是初始密码。

所有发布端口默认仅绑定 `127.0.0.1`。如端口占用，可在根目录 `.env` 设置端口变量后重跑同一启动命令，后台控制台链接会随之更新。`MUSAE_PUBLIC_HOST` 默认 `localhost`，用于生成浏览器链接和 BeatDesign/RNA 的合法来源。默认公开链接与来源使用本机 HTTP；远程 HTTPS 部署需另行配置受保护的反向代理，并同步调整来源校验及初始化清单中的完整控制台链接，本栈不会自动推断代理的协议和路径。Temporal 使用持久化开发服务器，正式生产应采用独立 Temporal 集群与相应访问控制。

## 后台启停与数据

进入 `/admin/components` 可以查看各服务状态和独立健康状态，启停组件并进入控制台。Temporal 条目同时管理 Temporal 和 worker，停止时先停 worker。首次 `up` 创建全部容器，后台只对已存在、且 Compose 项目和服务标签匹配的容器执行 start/stop，不构建镜像、不删除容器或数据卷。Octop 本身作为当前控制面使用集成开关，数据库不提供后台停止按钮。

为执行这些操作，只有 Octop 挂载 Docker socket；这赋予控制面宿主 Docker 的高权限，必须保护全局管理员账户和部署配置。浏览器请求只能选择组件及动作，不能指定容器 ID、命令或 Docker 地址。初始化器生成的运行清单通过只读卷交给 Octop。

PostgreSQL、Octop 配置、Temporal、BeatDesign、RNA、Semantica、Hindsight、评测报告及各服务凭据均使用独立命名卷。`docker compose down` 保留数据，重新 `up` 继续使用；`down -v` 会删除本项目全部持久数据和密码。备份时需同时保留数据库、应用数据及凭据卷。

## 默认功能边界

启动不会提交任务、调用模型、自动运行 CommerceAgentBench 或载入宿主机已有 RNA 状态。模型及供应商在工作台按租户配置。CommerceAgentBench 原项目提供 CLI，栈中额外提供只读报告控制台；容器健康表示运行环境与报告服务可用，不表示长程评测已通过。需要真实基准时，通过容器中的 `commerce-agent-bench` CLI 显式执行，结果写入 `/results`。控制台卷默认只读，可用临时执行容器获得结果卷写权限：

```sh
docker compose run --rm --volume musae_benchmark_results:/results --entrypoint commerce-agent-bench commerceagentbench --help
```

使用自定义 `COMPOSE_PROJECT_NAME` 时相应修改示例卷名。评测涉及隔离容器/模型时须按上游指南配置，默认不向评测容器授予宿主 Docker socket，也不透传模型密钥。

Hindsight 默认 `HINDSIGHT_API_LLM_PROVIDER=none`，支持分块存储和语义检索；事实提取、合并与 Reflect 需要另行配置模型。Semantica SDK 在 Octop 中为每个租户维护存证，Explorer 使用自己独立的数据卷；其默认图不等于所有租户存证的聚合视图。租户业务存证仍从 Octop 工作台授权查询，避免通过全局 Explorer 暴露多租户数据。

## 排查

```sh
docker compose config --quiet
docker compose logs --tail 100 octop worker
docker compose logs --tail 100 hindsight
docker compose restart octop worker
```

服务异常时先看对应日志和健康状态。不要通过删除卷修复数据库或凭据冲突。各组件服务在容器网络内使用固定 DNS 名，宿主端口变化不会改变 Octop 的内部集成地址。
