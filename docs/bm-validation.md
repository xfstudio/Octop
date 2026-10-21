# BM 集成验证记录

验证日期：2026-10-19。环境为 Windows、Python 3.12、Octop 现有 `.venv` 和 dashboard 依赖。以下结果验证本地实现及指定边界，不代表生产业务效果。

## 后端完整回归

```powershell
$env:PYTHONUTF8 = '1'
$env:PATH = 'C:\Program Files\Git\bin;' + $env:PATH
uv run --no-sync pytest -n 4 -m 'not live' -q --tb=short
```

结果：**4975 passed，225 skipped**，耗时 453.63 秒。首次运行误用了 Windows 的 WSL Bash，Docker 入口与打包脚本测试失败；改用 Git Bash 后全部通过。新增 migration 22 对应的备份版本断言和测试包重名问题也已修正。仍存在上游依赖弃用提示及一条原有浏览器安装测试的子进程解码 warning。

完整回归之后补充的当轮事实传递、人工恢复刷新事实、禁止恢复旧 replan、异步操作撤权及存证导出边界，另由长程平台专项回归验证。

最终组合专项命令及结果：

```powershell
uv run --no-sync pytest tests/unit/longterm tests/unit/db tests/unit/i18n tests/unit/backup/test_system_archive.py -m 'not live' -q --tb=short
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src/octop
uv lock --check
```

结果：**431 passed，1 skipped，6 deselected**，耗时 29.94 秒；Ruff 无问题，1268 个文件格式通过，mypy 检查 612 个源文件通过，锁文件检查通过。该轮包含最后新增的 10 项权限与异步撤权测试。Semantica 导出专项确认分页完整覆盖 1006 条记录；RNA 实际子进程冒烟确认在发送输入前撤权会阻止调用并清理进程树，没有调用远端模型。

最终提交前，仓库原生 **`make all` 已通过**，包含全仓格式化、Ruff、612 个源文件的 mypy 和完整后端回归：**4990 passed，225 skipped，18 warnings**，耗时 951.28 秒。首次直接在 PowerShell 调用时存在 `pwd` 和可执行路径解析问题，最终改为在 Git Bash 中运行原命令，没有跳过检查：

```bash
set -o igncr
export SHELLOPTS PYTHONUTF8=1
export MAKEFLAGS='SHELL=C:/Progra~1/Git/bin/bash.exe RUN=uv\ run\ --no-sync PYTEST_JOBS=4'
make all
```

## 数据库与持久调度

- PostgreSQL 17 临时容器：4 项真实集成测试通过，覆盖 migration 22 残缺恢复、非超级用户 RLS、跨租户拒绝、配置 CAS、并发哈希链、完整本机任务执行，以及成员撤权和 admin 降级后的事务提交拒绝。测试自行创建并清理独立数据库和角色。
- PostgreSQL 部署初始化：实际执行双角色脚本，确认应用角色 `NOSUPERUSER/NOBYPASSRLS`，应用拥有表时 `FORCE RLS` 仍拒绝无授权写入。临时容器已清理。
- Temporal Python SDK：2 项真实开发服务器测试通过，覆盖审批等待跨 worker 生命周期、Signal、Timer、`queued` 竞态，以及活动中断后更换 worker，恢复未确认写入为 `uncertain`，写适配器只调用一次。写适配器为合成实现，未执行真实业务写入。
- `uv lock --check` 通过；wheel 构建及 `rna_bridge.mjs` 打包检查通过。

本机运行验收安装包为 `dist/local-smoke/wheel/octop-1.0.2-py3-none-any.whl`（13976403 字节），SHA256 为 `74724c7b50d53ed089aab22e0d3304f3246bd8ba4aafa6b0592d4bec16d3e49d`。包内长程后端、SQL 仓储、API、服务接线与当前源码逐字节一致，包含两种 migration 22、RNA bridge、更新品牌后的前端资源，且不包含旧 WebM 品牌动画；本任务临时容器与辅助进程已清理。

重跑 PostgreSQL 测试需提供允许创建测试数据库和角色的管理员 DSN；测试不会重置已有应用库：

```powershell
# OCTOP_TEST_BM_PG_ADMIN_URL 由本机环境提供，不在仓库保存凭证。
uv run --no-sync pytest tests/unit/longterm/test_postgresql_live.py -m live -q
uv run --no-sync pytest tests/unit/longterm/test_runtime_temporal_live.py -m live -q
```

## 前端与浏览器

- `npm run build` 成功，长程页面资源进入 `src/octop/dashboard` 和 PWA 预缓存。
- 22 项定向测试通过；TypeScript、改动文件 ESLint、17 个改动文件的 Prettier 检查、国际化同树检查通过。
- 真实浏览器验证：密码登录/JWT、租户与私有空间、BeatDesign 服务端导入、上传版本 1、下载独立副本、本机任务完成、证据链 valid、跨设备版本 409 冲突保留本机数据。
- 离线编辑刷新恢复、390px 页面布局及无控制台错误已检查。
- 全量 ESLint 有 3 项既有错误，涉及 Channels 常量测试、Chat store 和模型列表编辑器；相关文件与本次开发前的基线一致。本次未修改这些文件。

## dev-musae 品牌验证

原图存放在 `branding/musae-logo.png`，与上传文件逐字节一致。Web 启动、登录、侧栏、引导、空状态和等待页面均统一使用原图；旧品牌动画已移除。浏览器隔离外部 API 后实测启动、登录、离线工作台和离线 fallback，显示比例、透明背景和无滤镜均符合预期。新 PWA maskable 图标使用独立白底安全区域。

- 品牌相关前端测试：15 项通过；TypeScript、定向 ESLint、Prettier 和最终前端构建通过，PWA 预缓存包含新资源。
- 桌面 `go test ./...` 通过；15 个桌面/FnOS/Linux PNG 的尺寸与透明度、Windows ICO 的 6 种尺寸及 macOS ICNS 解码通过。macOS 原生打包仍需对应系统工具链验收。
- 统一生成器重复执行后，检查的 115 个图像、SVG、HTML 等文件内容无变化；生成器与壁纸 helper 的 Ruff 和格式检查通过。
- 推送前只读检查未发现真实凭证、私钥、数据库、日志、缓存或临时数据混入新增文件。代码自定义改动集中在 `dev-musae`。

## 本机编译与实际运行验收

在 `dev-musae` 上重新执行本机编译、安装与运行，日志和测试数据位于忽略目录 `dist/local-smoke/`：

- 前端 `npm run build`（含 TypeScript）通过；6 个专项文件的 27 项 Vitest 通过，PWA 预缓存 106 项。
- 后端长程平台、数据库、权限、迁移、国际化与备份专项：431 passed、1 skipped、6 deselected。真实 Temporal 开发服务器：2 passed。
- 新 wheel 通过 `uv pip install --no-deps --target dist/local-smoke/packages` 安装；运行时确认 `octop.__file__` 来自该安装目录。独立 `OCTOP_HOME=dist/local-smoke/runtime`，SQLite 与管理员均新建，监听 `127.0.0.1:8099`，原有 `8088` 服务保持运行。
- 实际 HTTP 冒烟 24 项通过：JWT、租户与空间、BeatDesign 导入、显式上传下载、409 并发冲突、凭证拒绝、双节点本机执行、幂等命令、SSE 游标恢复、11 条有效哈希链记录，以及非成员和同租户私有数据隔离。非成员返回 403；同租户非所有者返回 404，避免透露私有资源是否存在。
- 真实浏览器通过页面滑块和密码登录，完成 Canvas 本地预览与导入。发现并修复首次断网访问未访问过的 `/offline-workbench` 失败：现在单独预缓存离线入口 HTML，摘要覆盖 HTML 和当轮资源；普通页面仍使用 NetworkFirst，更新仍由用户确认。
- 清除该测试源的 SW 与浏览器缓存，仅在线打开登录页安装资源，确认普通 HTML 缓存为空后断网首次进入离线入口，成功编辑并刷新恢复两节点配置；390px 视口无横向溢出。离线时初始化状态/应用清单等网络探测可能报告断网，不影响本地编辑。
- Windows `go test -count=1 ./...` 通过，`go build -mod=readonly -trimpath -buildvcs=false -ldflags='-H windowsgui' -o bin/Octop-local-smoke.exe .` 成功。产物位于 `desktop/src/bin/Octop-local-smoke.exe`，为 Windows AMD64 开发桌面壳，本机 WebView2 可用。完整安装包仍需 Wails/NSIS 与 portable runtime。

本机测试管理员为 `musae-local`，随机口令仅保存在忽略的 `dist/local-smoke/access.json`；不包含真实模型或云服务凭证。测试过程未调用付费模型。服务使用受管理终端前台运行，重启命令见本机 `dist/local-smoke/run-server.ps1`。

## 统一组件工作台验收（2026-10-20）

入口为 `/workbench/platform`，原 `/longterm` 保留查询参数和片段并跳转至新入口。组件管理中心覆盖 Octop、Semantica、BeatDesign、CommerceAgentBench、RNA、Hindsight 和 Temporal；各操作复用当前租户、私有空间和任务，不默认选择其他空间。

- 前端 8 个专项测试文件共 **63 项通过**，包含组件配置、操作表单、项目绑定、候选历史、权限与路由；TypeScript、定向 ESLint 和生产构建通过。
- 仓库完整 `make all` 通过：**5012 passed、225 skipped、18 warnings**，耗时 1471.68 秒；包含全仓格式化、Ruff、615 个源文件的 mypy 和完整非 live 后端测试。仍有上游弃用提示和既有 Windows 浏览器安装测试子进程解码 warning。
- 提交钩子首次运行的受影响测试为 1239 passed、1 skipped、1 failed：既有备份删除并发测试在覆盖跟踪下超过 5 秒启动等待。单独普通运行通过、开启 testmon 可复现。测试改用线程安全的异步事件通知与有界等待，保留 busy、409、204 及释放状态全部断言；testmon 单项复验通过（50.09 秒）。此处只修复测试同步，未改备份业务实现。
- 本机新 wheel 的真实 HTTP 检查 **29 项通过**，覆盖登录要求、七组件目录、脱敏配置、真实数据库探测、凭证写入不回显/空白保留/显式清除、本机任务与有效证据链、手工候选与历史、项目绑定 CAS、项目覆盖与写工具拒绝、普通成员管理拒绝及私有空间隔离。
- 浏览器确认旧路由兼容跳转、七组件卡片、配置保存与凭证空白、数据库连接检查、当前任务候选表单和历史可用。隐藏工作台时操作弹窗消失；390px 视口的页面与文档宽度均为 390px。
- 发现并修复统一入口引起的离线依赖拆分：预缓存覆盖全部构建 JavaScript（181 个模块，含 6 个语言包），共 251 项、约 15.94 MiB。全新浏览器环境只访问登录页，等待 SW 激活后清空 HTTP 缓存并断网，首次打开未访问过的离线入口成功；修改配置名称并刷新仍保留，没有页面脚本异常。
- 最终 wheel 为 `dist/local-smoke/workbench-wheel/octop-1.0.2-py3-none-any.whl`，14018258 字节，SHA256 `6e183a1352744925cb35e64446f0830a8f8611d24cc9209e32225fbba68747b4`。安装至 `dist/local-smoke/workbench-packages` 并使用原独立测试数据目录在 8099 运行；重新执行 29 项 HTTP 检查通过。原 8088 服务未停止。
- 连接检查只做只读探测，不触发付费生成。Temporal 修改连接、命名空间或队列后需单独重启 worker；项目读取不会执行媒体生成或基因激活。候选状态迁移仍不代表部署生产行为。

## 管理后台组件启停与控制台验收（2026-10-20）

新增系统管理员入口 `/admin/components`，覆盖七组件的集成配置、运行状态、确认启停及控制台链接；工作台组件中心可直接进入后台，后台也可返回当前组件中心。租户 owner/admin 不具有宿主服务管理权限。

- 最终代码完整 `make all` 通过：**5035 passed、226 skipped、18 warnings**，耗时 603.04 秒；包含全仓格式化、Ruff、616 个源文件的 mypy 和完整非 live 后端测试。warning 为既有依赖弃用与 Windows 浏览器安装测试子进程解码提示。
- 前端五个专项文件 **45 项通过**，包含启停确认、单次 POST、409 不重试、失败后重新读取实际状态、权限/账号切换、离线锁定及控制台链接；TypeScript、定向 ESLint、Prettier、最终生产构建通过。运行信息在卡片中分行显示，普通模式说明使用次要文字。
- 后端生命周期专项 **23 passed、1 skipped**；与原组件配置和 HTTP 合同联合验证 **50 passed、1 skipped**。覆盖真实私有 Python 进程、跨管理器 CAS 竞争、审计失败不派发、执行前撤权、确认与版本冲突、固定 Compose 参数、输出脱敏、父进程提前退出及操作取消后的 uncertain 审计。当前 Windows 跳过 POSIX 进程组回归，该平台仍需实际执行该项。
- 编译后的 wheel 在本机 8099 完成 **17 项生命周期 HTTP 检查**：原生进程真启动/停止、HTTP 端口开放/关闭、过期版本 409、未确认/任意命令字段拒绝、Compose 健康与停止、内嵌集成停用/恢复及控制面持续可用。进程目标为无模型调用的合成 HTTP 服务，Compose 目标为专用 Redis 测试服务；它们只验证运行器，不能算作 Semantica/Temporal 业务服务验收。
- 最终包重新执行原有工作台与权限 HTTP 冒烟，扩展为 **32 项通过**，包括普通租户读取宿主进程/控制台和启停操作返回 403，以及最终服务不存在测试托管绑定。测试进程、容器、匿名测试卷和测试网络均已清理；最终后台只展示真实嵌入/外部接入状态。
- 浏览器通过“确认启动→打开原生控制台新标签→确认停止”的真实流程，控制台链接包含 `noopener noreferrer`；验证后台和工作台双向导航。390px 视口和文档宽度均为 390px，七卡片运行信息无横向溢出；恢复 1440px 视口后无页面脚本错误。
- 最终 PWA 预缓存 **255 项，约 15.96 MiB**，包含新管理页面及全部 JavaScript 分块。全新浏览器环境只在线访问登录页，等待 SW 激活、清空 HTTP 缓存后断网，首次进入未访问过的离线工作台成功，无页面脚本错误。最终 wheel 位于 `dist/local-smoke/component-runtime-final-wheel/octop-1.0.2-py3-none-any.whl`，14034335 字节，SHA256 `bd2d2054d92caf5864f494d481751cfdd88861a337a688fe1923fa6e628badb7`；独立安装目录为 `dist/local-smoke/component-runtime-final-packages`。8099 继续使用原专用测试数据，8088 原服务保持可用。

真正的服务启停只对宿主可信清单绑定的 `process`/`compose`/`docker` 生效；内嵌 SDK、批处理结果适配器及未托管远端只提供集成开关，停用不撤销已经派发的操作。上游控制台保留自己的认证，未配置地址时进入 Octop 集成控制台。部署方法与已知边界见 [组件生命周期说明](component-runtime.md)。

## 单文件容器栈验证（2026-10-21）

根目录 `docker-compose.yml` 默认启动全部服务，不依赖额外 Compose 文件或 profiles；启动方法、源码版本、密码读取和功能边界见 [统一容器部署](../docker/README.stack.md)。本机使用独立项目 `musae-unified-smoke`，主程序端口为 18088，原有 8088/8099 服务保留。

- 最终源码完整 `make all` 通过：**5098 passed、226 skipped、18 warnings**，耗时 2600.74 秒；Ruff、格式检查与 616 个源文件的 mypy 通过。warning 为既有依赖弃用及 Windows 浏览器安装测试子进程解码提示。统一栈专项 **20 passed**，Temporal poller 与组件探测专项 **40 passed**；`docker/stack` 的全部 Python 文件单独通过 Ruff 与格式检查。
- BeatDesign 和 RNA 的生产镜像通过真实页面、静态资源、Basic/Bearer、匿名拒绝、来源校验、内部 DNS、数据重启和正常退出验证；BeatDesign MCP 初始化及工具目录可读，RNA 原生终端能执行合成输出。未提交生成任务或载入宿主 RNA 状态。
- Semantica Explorer 实际写入独立 SQLite 存证并通过其 API 回读，重启后仍可读取；页面和私有 API 的鉴权通过。Octop 中的租户 SDK 存证与 Explorer 独立数据卷保持各自授权范围。
- Hindsight 固定官方镜像，在未配置云模型密钥及 Hugging Face 离线环境中，通过真实 Next 控制台、服务端鉴权转发、Basic/Bearer、记忆 retain/recall、重启持久化及 45 秒内正常退出验证。默认 `none` 仅提供 chunks 记忆；Reflect 仍需实际 LLM。
- CommerceAgentBench 实际 CLI 目录、只读报告索引及认证通过；报告读取拒绝路径穿越、符号链接和特殊文件。未执行付费基准或 judge。PostgreSQL 应用角色实际为 `NOSUPERUSER/NOBYPASSRLS`，Linux 凭据目录和文件权限、重启复用检查通过，未输出口令。
- 通过根 Compose 的 `up -d --build --wait --wait-timeout 900`，九个长驻服务均达到 healthy，初始化器正常退出 0。最终镜像的 **64 项实际 HTTP 检查全部通过**：包括七组件连接检查、本机与 Temporal 的实际 echo 执行、输出校验、有效证据哈希链、完整 Semantica 索引回读、普通租户拒绝管理宿主服务，以及六个外部组件的真实停止、过期版本 409、启动和健康恢复。Temporal 条目同时管理 worker，停止期间 Octop 控制面持续可用。
- 整栈联调发现并修复 Temporal 连接检查误报：后台应接受所配置队列上的远端 worker，worker 自身健康检查仍须匹配本容器身份。浏览器确认容器生产登录页、离线工作台及七组件入口可渲染；认证后的后台生命周期使用上述真实 HTTP 验收。

## 主项目组件互调与源码联调验收（2026-10-21）

按 main 稳定线同步 upstream：已执行 fetch 和 merge，`upstream/main` 最新为 `dd9dce47`，本地 main 与 `dev-musae` 已包含该提交。功能改动仅在 `dev-musae`；未合入 upstream/develop。工作流操作及输入引用合同见 [组件工作流](component-workflows.md)。

- 最终源码完整 `make all` 通过：**5179 passed、227 skipped、17 warnings**，耗时 575.00 秒；格式检查、Ruff 和 **618 个源文件**的 mypy 均通过。warning 为既有第三方依赖弃用提示。组件节点专项 65 passed、1 skipped；输入引用与运行器回归 38 passed；统一 Compose 专项 22 passed。Windows 跳过的目录符号链接项仍需在支持的平台运行。
- 主项目提供 13 项注册组件操作，工作台支持组件节点、离线操作目录及成功前驱的 JSON Pointer 引用。本机与 Temporal 运行同一套分发代码；服务端冻结项目绑定版本和调用目标摘要，执行前后再次校验授权。Semantica 索引使用 R1 精确审批；输入预算在复制前累计，重复引用也受 128 KiB 聚合上限约束。
- 最终容器源码的真实跨组件冒烟 **106 项通过**。独立调试租户下，本机和 Temporal 各完成七节点 DAG，覆盖 BeatDesign canvas、RNA genes/observability、CommerceAgentBench report、Hindsight recall、结果传递和 Semantica index。Hindsight 服务端审计中的完整 query 均匹配 BeatDesign 实际 MCP 输出；两条任务的模型 token 均为 0，业务结果保持 inconclusive。
- 两条任务的 R1 索引均先实际等待，再通过主 API 携带当前 revision、node_id 和 plan_digest 精确审批；分别核对 25/26 条事件的完整哈希链，并从真实 Semantica SQLite 回读 21/22 条已索引证据。未绑定项目、伪造 snapshot、降低索引风险、缺少 consent、绑定版本变化和调用目标变化均被拒绝，临时连接配置已恢复。
- RNA 使用上游 demo/manual 项目，关闭 initiative/evolution；Bench 使用独立私有目录的 synthetic-debug fixture，保持 unverified、无评分。上述结果验证真实服务接线和数据传递，未调用付费模型、未运行真实基准，也不代表进化行为已发布。
- 根 Compose 完整重建并通过 `--wait`，九个常驻服务均为 healthy，初始化器正常退出。真实运行 `watch --no-up --prune=false`，确认唯一 Python 探针的新增和删除均同步到 Octop/worker，两者自动重启并可导入新增源码；探针和本次 watch 进程已移除，后台容器保留运行。8088/8099 原服务保持可用。
- 前端定向 **17 项 Vitest**、TypeScript 与所改文件 ESLint 通过；浏览器实际验证容器工作台的组件类型、操作目录、Semantica index 默认输入及锁定的 R1 风险。使用独立只读预览核对 `/api/docs` 的新接口摘要、认证要求和具名响应结构；预览进程已停止，容器默认仍关闭 API 文档。
- 全量检查中发现原 CLI 懒加载测试删除模块缓存后污染后续测试；将该验证隔离到独立 Python 子进程，原复现顺序 3 项及两个测试文件组合 50 项均通过，生产 CLI 代码无需修改。

## 组件结果回填与评测闭环验收（2026-10-22）

功能改动位于 `dev-musae`；再次 fetch 确认 `main`、`origin/main`、`upstream/main` 均为 `dd9dce470844c25d59effb50615bb3c667ea25cd`，当前分支已包含该稳定版。操作流程、评分合同与课程参考范围见 [评测体系说明](evaluation-system.md)。

- 七组件卡片、工作流组件节点、项目读取结果、旧组件操作及任务节点结果均有操作会话入口。外部控制台操作后返回工作台，通过白名单适配器读取、核对摘要并确认保存，再显式回填本机草稿；运行任务的冻结 DAG、revision 和 checkpoint 不被回填改写。
- 使用根目录同一个 `docker-compose.yml`，在独立项目 `musae-evaluation-debug` 真实重建并启动。主程序为 `http://127.0.0.1:18088`，Temporal UI 为 `http://127.0.0.1:18233`；九个常驻服务均为 healthy，初始化器正常退出 0。主服务与 worker 的 11 个相关后端源码文件 SHA256 均与当前工作区一致；旧服务和其他 Compose 项目保持运行。
- 最终容器的七组件操作会话真实 HTTP 检查 **75 项通过**，覆盖创建、读取、摘要确认、持久化、任务关联存证、手工导入不提升信任、本机与 Temporal 任务的 revision 保持及完整哈希链。真实 PostgreSQL 应用角色为非 superuser、非 bypass RLS，原始 SQL 验证会话表的 tenant、space、owner 隔离；跨私有空间读取被拒绝。
- 真实评测闭环通过：本机与 Temporal 各完成三用例 × 两次独立任务，保留实际轨迹计时；故意错误基线与修复版本各执行两次，配对提升为 1，比较结论为 GO，但 `release_eligible` 仍为 false。失败案例经同例重评后才能验证并显式复制进独立回归套件，原失败分数保持不变。
- CAB 合成报告没有 verifier/reward 时保持 unverified、覆盖率为 0，不生成虚构奖励。Linux 容器中实际创建跨私有空间目录符号链接，通过 API 导入返回 422，补充 Windows 跳过项。Hindsight 使用私有空 bank，服务端记录的 LLM 请求为 0；未调用付费模型、未运行真实 CAB 基准、未发布 RNA 行为。
- 独立审查修复权限变化后前端保留旧会话的问题；全局角色和租户角色变化会关闭旧会话并重置评测状态，相同权限的普通刷新保留会话。另修复执行前控制检查异常被计时字段遮蔽的问题：现在记录 `action_not_dispatched` 与原异常类型，不记为已执行操作、不扣模型用量；运行器专项 **29 项通过**。
- 最终源码完整 `make all` 通过：**5228 passed、228 skipped、18 warnings**，耗时 3257.64 秒；包含全仓格式化、Ruff、624 个源文件的 mypy，以及完整 5456 项非 live 后端测试。使用四个 worker，并以 `--no-testmon` 关闭覆盖跟踪，未缩小测试范围。warning 为既有依赖弃用及 Windows 浏览器安装测试子进程解码提示。
- 前端十个专项文件 **75 项通过**，TypeScript 和改动文件 ESLint 通过。本机 jsdom 的样式计算使多步骤测试超过原有 15 秒上限；逐步骤诊断确认权限用例完整断言通过、约 75% 时间消耗在样式计算。最终保留全部断言，以 60 秒单例上限串行复验，未修改产品行为或默认测试配置。
- 浏览器确认最终容器的评测中心、套件版本、配对比较、失败案例及七组件入口可渲染，离线/未登录状态正确禁用执行。未完成本轮带登录的浏览器回填全流程；认证和回填行为由真实 API 与前端行为测试验证。另以独立只读预览检查 `/api/docs` 的新增摘要、鉴权要求和具名结构，预览进程已停止。
- 课程只读取公开第一章及十章目录，其余正文需要登录。章节映射是项目实现说明，没有把未读取的课程正文当作已复现功能；失败规则和真实轨迹支持人工定位原因，尚无自动首错标注。

## 仍需独立验收

真实模型、CommerceAgentBench 付费完整运行、生产 Temporal 集群仍需独立验收。RNA 上游前端锁文件的依赖审计仍报告 1 项 high，本轮保留上游锁文件，尚未完成该依赖升级验证。

当前 agent 执行使用冻结技能与模型进行无工具分析，可消费已授权组件节点的结果。除已接入的 Semantica R1 索引以外，R1/R2 具体业务写入适配器、实际候选发布器、Shadow/Canary 运行、经营收益归因、WORM/外部时间戳、灾难恢复仍需逐项完成；候选状态迁移不等同于已部署新行为。组件接线与配置详见 [BM 平台说明](bm-platform.md) 和 [开发部署说明](../docker/README.longterm.md)。
