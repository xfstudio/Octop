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

最终安装包为 `dist/longterm/octop-1.0.2-py3-none-any.whl`（13976354 字节），SHA256 为 `dc1dd5ecb20fe1a9853b37dd62c79fae4b6e40706906ef563e0fb2f7b6c42c94`。包内长程后端、SQL 仓储、API、服务接线与当前源码逐字节一致，包含两种 migration 22、RNA bridge、更新品牌后的前端资源，且不包含旧 WebM 品牌动画；本任务临时容器与辅助进程已清理。

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

## 仍需独立验收

Semantica 真实本地源码的 SQLite 写入与回读已验证；RNA 真实 SDK 通过本机合成 SSE 服务验证。Hindsight/BeatDesign 远端服务、真实模型、CommerceAgentBench 付费完整运行、生产 Temporal 集群及整套 Docker 镜像端到端尚未验证。

当前 agent 执行使用冻结技能与模型进行无工具分析。R1/R2 具体业务写入适配器、实际候选发布器、Shadow/Canary 运行、经营收益归因、WORM/外部时间戳、灾难恢复仍需逐项完成；候选状态迁移不等同于已部署新行为。组件接线与配置详见 [BM 平台说明](bm-platform.md) 和 [开发部署说明](../docker/README.longterm.md)。
