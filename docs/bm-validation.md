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

真正的进程启停只对宿主可信清单绑定的 `process`/`compose` 生效；内嵌 SDK、批处理结果适配器及未托管远端只提供集成开关，停用不撤销已经派发的操作。上游控制台保留自己的认证，未配置地址时进入 Octop 集成控制台。部署方法与已知边界见 [组件生命周期说明](component-runtime.md)。

## 仍需独立验收

Semantica 真实本地源码的 SQLite 写入与回读已验证；RNA 真实 SDK 通过本机合成 SSE 服务验证。Hindsight/BeatDesign 远端服务、真实模型、CommerceAgentBench 付费完整运行、生产 Temporal 集群及整套 Docker 镜像端到端尚未验证。

当前 agent 执行使用冻结技能与模型进行无工具分析。R1/R2 具体业务写入适配器、实际候选发布器、Shadow/Canary 运行、经营收益归因、WORM/外部时间戳、灾难恢复仍需逐项完成；候选状态迁移不等同于已部署新行为。组件接线与配置详见 [BM 平台说明](bm-platform.md) 和 [开发部署说明](../docker/README.longterm.md)。
