# 多租户长程任务开发环境

这份 Compose 使用同一个 PostgreSQL 控制库和同一个 `/data/.octop` 配置卷供 Octop API 与 Temporal worker 使用。SQLite 仍用于单进程本机模式；本文件不把一个 SQLite 控制库交给两个服务进程。Temporal 服务使用带持久卷的开发服务器，适合本机开发，生产环境可通过 `OCTOP_TEMPORAL_ADDRESS` 接入独立 Temporal 集群。

以下命令在 Octop 仓库根目录执行。先设置 `OCTOP_LONGTERM_DB_PASSWORD` 环境变量为自行选择的应用数据库密码；`OCTOP_LONGTERM_BOOTSTRAP_DB_PASSWORD` 是独立的数据库引导管理员密码，开发环境未设置时回退应用密码，生产环境必须分别设置不同密码。不要把真实密码提交到仓库。镜像需要使用当前源码构建。

```sh
docker build -f docker/Dockerfile -t octop:longterm-base .
docker build -f docker/Dockerfile.longterm -t octop:longterm .
docker compose -f docker/docker-compose.longterm.yml up -d postgres octop
```

打开 [Octop](http://localhost:8088) 完成数据库和管理员初始化，再创建租户、私有空间和 Agent、配置模型。向导口令可在数据卷的 `octop-login.txt` 中读取：

```sh
docker compose -f docker/docker-compose.longterm.yml exec octop cat /data/.octop/octop-login.txt
```

初始化完成后，显式启动开发 Temporal 服务和 worker：

```sh
docker compose -f docker/docker-compose.longterm.yml --profile temporal-server --profile worker up -d
docker compose -f docker/docker-compose.longterm.yml --profile temporal-server --profile worker ps
docker compose -f docker/docker-compose.longterm.yml logs --tail 100 worker
```

Temporal UI 位于 [localhost:8233](http://localhost:8233)。三类数据分别保存在 `control_db`、`octop_data`、`temporal_data` 命名卷中；普通 `down` 保留这些卷。API 和 worker 的数据库连接参数、任务队列、命名空间必须一致。worker 只初始化控制库和任务服务，不启动 IM 网关、cron 或主动关怀；健康检查会查询 Temporal 中属于本容器的实际活动轮询器。

新控制库由 `octop_bootstrap` 超级用户引导，初始化脚本另建 `octop` 应用角色，并显式设置 `NOSUPERUSER NOBYPASSRLS`。API 与 worker 只使用 `octop`，它拥有目标库的连接及 public schema 的 USAGE/CREATE 权限，迁移创建的表归其所有，`FORCE ROW LEVEL SECURITY` 对它生效。引导管理员凭据不传给 API 或 worker。

已有 `control_db` 卷不会重新执行 initdb 脚本，更新 Compose 环境变量也不会更改已有角色。数据库健康检查还会确认 `octop` 已降权；未完成迁移时会阻止依赖它的 API/worker 启动。升级旧配置时先备份数据库并停止 API/worker，用现有数据库管理员连接 `octop` 库，手动创建 `octop_bootstrap`，使用 psql 的 `\password octop_bootstrap` 设置与部署变量一致的密码，再以该新管理员执行以下权限迁移；如果管理员角色已存在，直接复用并核对密码。

```sql
-- 首次迁移：由原管理员执行，随后 \password octop_bootstrap
CREATE ROLE octop_bootstrap WITH LOGIN SUPERUSER;
-- 重新连接为 octop_bootstrap 后执行
ALTER DATABASE octop OWNER TO octop_bootstrap;
GRANT CONNECT ON DATABASE octop TO octop;
GRANT USAGE, CREATE ON SCHEMA public TO octop;
ALTER ROLE octop NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
```

原应用角色 `octop` 和已有表、证据、租户数据保持原有名称；若修改应用密码，另用 `\password octop` 更新后同步部署变量。核对 `SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('octop', 'octop_bootstrap');`，应用行后两项应为 false，再重启服务。不要通过删除数据卷来迁移角色。

已有外部 Temporal 集群时，设置 `OCTOP_TEMPORAL_ADDRESS`，按需设置 `OCTOP_TEMPORAL_NAMESPACE`、`OCTOP_TEMPORAL_TASK_QUEUE`、`OCTOP_TEMPORAL_TLS=true` 和 `OCTOP_TEMPORAL_API_KEY`，然后仅启用 `--profile worker`。这些凭据只保存在部署环境中。未启用 Temporal 的任务使用 Octop 本机运行模式。

镜像固定为经过镜像清单核对的 `temporalio/temporal:1.5.1` 与 `pgvector/pgvector:0.8.1-pg16`，并固定各自的多架构摘要；Python SDK 通过 `uv.lock` 固定为 `temporalio==1.34.0`。长程扩展镜像沿用已有 Octop runtime，使用锁文件增加 `longterm-temporal` 依赖；Semantica、Hindsight、Commerce Agent Bench 与 RNA 各自按集成文档配置，构建本镜像不会自动连接这些系统。

工作流每次 activity 调用同一套租约与证据恢复服务，最长运行七天，每 15 秒发送 heartbeat，失联 90 秒即判定超时。审批和暂停使用持久等待，定时唤醒使用 Temporal timer。SDK 的原 activity 设置 `maximum_attempts=1`，禁止自动重试原调用；活动失败后，Workflow 等待 10 秒再进入 SQL 恢复步骤。恢复会先检查租约并重建证据和预算，未确认的写入转为 `uncertain`，只读动作才允许在预算内重新尝试。

本镜像和 Compose 默认提供 API、控制库与 Temporal worker。Semantica/Hindsight Python extras、Node ≥22.19、RNA SDK，以及 BeatDesign、Hindsight 等上游服务需要各自准备；启用时用衍生镜像或 Compose override 添加依赖、设备本地凭据、对应环境变量与挂载。事实与演化报告目录也需显式挂载，并只允许可信报告生成器写入。宿主机仅设置这些可选变量不会自动传入当前 Compose 服务。

可单独验证编排文件，无需启动服务：

```sh
docker compose -f docker/docker-compose.longterm.yml --profile temporal-server --profile worker config --quiet
```

真实 SDK 与临时 Temporal 开发服务器的自动冒烟检查：

```sh
uv run --no-sync pytest tests/unit/longterm/test_runtime_temporal_live.py -m live -q
```

此检查分别验证审批等待跨 worker 重启恢复，以及运行中活动取消后更换 worker，通过真实 SQLite 证据恢复将未确认写入置为 `uncertain`，并断言合成写入适配器只调用一次；另覆盖唤醒 signal、持久 timer 与 `queued` 并发命令状态。不验证真实模型、付费生成、生产 Temporal 集群或整套容器构建。
