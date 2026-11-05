# Code Version Manager 项目指南

## 项目概况

本项目是 Python 3、GTK 3 桌面应用，用于管理任意独立 Git 工作空间，提供本地离线发布、固定基线开发分支、手动目录合并、版本比较与 ZIP 导出，以及可选 SSH 整仓同步。应用代码有意集中在 `src/code_version_manager.py`，不引入额外服务或 Python 包管理框架。

| 路径 | 职责 |
| --- | --- |
| `src/code_version_manager.py` | 版本标签解析、数据类型、GitRepository 后端、GTK 界面和应用入口；Git 操作与界面有意集中于单文件 |
| `tests/test_backend.py` | Git 后端、发布、同步、恢复和 GTK 回归测试 |
| `tests/test_workflow.py` | 临时工作仓库与 bare origin 的完整流程测试 |
| `tests/test_merge_comparison.py` | 手动合并隔离副本、忽略规则、提交及取消测试 |
| `tests/verify_workspace_copy.py` | 仅在工作区隔离副本和临时远程中验证，不改来源和真实 origin |
| `README.md` / `INSTALL.md` | 用户说明与 Ubuntu 安装、故障排查 |
| `README_ai.md` | 历史讨论，不作为当前业务规则依据 |
| `VERSION` | 软件发行版本，与受管理工作空间的 Git 版本无关 |
| `data/`、`debian/`、`build_deb.sh` | 图标、启动器、Beyond Compare 配置及 Debian 打包 |

入口为 `main()`，应用 ID 为 `cn.local.CodeVersionManager`。安装启动器 `/usr/bin/code-version-manager` 加载 `/usr/lib/code-version-manager/code_version_manager.py`。启动工作空间依次读取 `CODE_VERSION_MANAGER_REPOSITORY`、GTK 用户配置 `code-version-manager/settings.ini`、`~/catkin_ws`。最近工作空间最多保存 10 个。GTK 初始化、窗口、对话框及主线程事件回调均位于同一源码文件；后端操作由 `GitRepository` 负责。

所选目录必须是独立 Git 根目录。初始化用 `git init`，默认分支为 main。已管理仓库的 Git 身份、唯一 origin、推送超时、比较工具和分支基线都保存在仓库本地配置。用户级 `code-version-manager/settings.ini` 另存 GitHub 账号、默认提交姓名和邮箱，作为新启用工作空间的默认值；打开已有仓库的“Git 账号”时，非空默认姓名和邮箱逐项预填，只有保存该窗口才写入仓库本地配置，单独保存设置不会修改已有仓库。应用元数据保存在实际 Git 目录下 `code-version-manager/`，不进入用户工作树或远程同步。

## 源码导航与配置

- 源码依次组织为标签解析与数据类型、用户配置及 SSH 辅助函数、`GitRepository`、发布文件准备及打包选择辅助函数、`FileSelectionDialog`、`VersionManagerWindow` 和应用入口。优先在相应现有方法中扩展业务，不拆分服务或引入包管理框架。
- Git 交互统一经过 `GitRepository.run()` / `run_bytes()`；远程操作用 `_run_remote()` 和 `run_remote_process()` 管理非交互 SSH、超时及传输子进程组清理。
- 用户配置中的 `[git_defaults]` 保存 `github_account`、`user_name` 和 `user_email`；读写设置须保留最近工作空间等其他配置节。GitHub 账号只用于地址示例，不自动设置 origin，也不代表 SSH 登录身份。
- 仓库本地配置键包括 `user.name`、`user.email`、`codeversionmanager.pushTimeout`、`codeversionmanager.compareTool`、`codeversionmanager.lastWorkingBranch` 及各分支的 `codeVersionManagerBase`。提交身份记录作者；SSH 密钥负责远程授权。

## 分支、标签与发布

1. main 发布正式版本，例如 `v0.0.3`。
2. 只能从当前检出的 main 新建开发分支，并选择 main 历史中的正式标签；基线固定不随 main 后续发布改变。切换与新建要求工作区干净。
3. 开发分支发布自己的 beta，例如 `branch1/v0.0.3-beta.0.0.1`。合并只采用最新已发布 beta；来源分支有未发布提交时必须先发 beta。
4. 手动合并完成后由用户另行发布 main 正式版本。合并本身不发布、不打标签、不更新 VERSION。

- 正式标签形如 `v数字.数字.数字...`。Beta 标签实际引用形如 `branches/<URL编码分支名>/v0.0.3-beta.0.0.N`；显示名用未编码分支名。
- 用 `parse_version_tag()`、`canonical_version()`、`version_tag_name()` 处理标签格式，不手工拆分或编码分支名。
- 新 beta 只生成 `beta.0.0.N`，按数字递增；旧 `beta.1` 可读，不自动迁移。
- `create_branch(name, base_version)` 从所选正式标签提交创建分支，`branch_base()` 校验固定基线。缓存键为 `branch.<name>.codeVersionManagerBase`。
- 多基线、无共同历史、标签不属于分支历史、浅克隆等情况明确报错，不猜测或静默重写。
- 默认历史范围显示当前分支相关标签及其正式基线；其他分支版本和继承的正式版本只读。“全部分支版本”只改变展示范围。
- 正式版发布将根目录 `VERSION` 写为不带 `v` 的正式号，例如 `0.0.3`；开发分支 beta 发布写入显示标签，例如 `branch1/v0.0.3-beta.0.0.1`。两者都纳入发布提交、manifest 和 ZIP。
- VERSION 在每个发布提交中固定该次标签号；后续未发布提交继续显示最近已发布版本，干净切换分支则从目标提交恢复对应值。合并保留 main 原 VERSION，不发布、不打标签、不更新 VERSION。
- `current_version_tag()` 优先查当前 HEAD 上属于当前分支的版本标签或正式标签；没有精确标签时按第一父链找最近正式标签或本分支 beta，无 beta 时校验固定正式基线。无版本标签时返回空值，不生成假版本。`workspace_version_value()` 统一转换文件显示值。
- 升级前的旧 beta 提交保留原 VERSION，切换、同步和查看历史不会重写旧提交或额外修改 VERSION；下一次 beta 发布后开始记录新的 beta 标签显示号。
- 发布时 VERSION 必须是普通文件路径，不能是符号链接、目录或被忽略。版本管理自动维护它，不因 beta 发布前的用户改动拒绝发布；使用临时 index，只提交所选文件、选定顶层删除范围及必要 VERSION，并保留其他暂存内容。
- 发布始终离线。根目录 `CHANGELOG.rst` 是说明输入和下载目标，不进入提交、manifest 或 ZIP。成功发布后按全部版本标签重新生成并覆盖该文件；发布前拒绝符号链接、目录和特殊文件目标。首次启用版本管理准备 `.gitignore` 和 CHANGELOG 模板；已有忽略规则保留。

## 手动合并

合并业务规则由 `GitRepository` 执行，GTK 负责输入、确认和后台调度。

- `plan_merge()` / `BranchMergePlan` 固定来源最新 beta 标签与 OID、main 最新正式版本与 OID、完整受管引用快照。来源 HEAD 必须等于最新 beta；main HEAD 必须等于最新正式标签；要求干净工作区、完整历史和共同历史。
- 合并不 fetch、上传、切换分支或执行 `git merge`。确认后从来源 beta 与 main 正式提交建立 Git 元数据目录中的双目录隔离会话：来源侧只读，main 侧编辑。
- 合并输入是 Git 跟踪的完整树，不按 manifest 或 export-ignore 裁剪。符号链接、子模块和特殊文件使用只读说明，不跟随链接。
- 两侧树中的 `.gitignore` 按 Git 规则分别解析并取并集。命中路径从两边对比副本中隐藏并禁止写回；规则和受保护路径在会话建立时固定。Beyond Compare、Meld、KDiff3 接收相同的过滤后目录。
- `VERSION` 和相同内容文件不可编辑。只有相对 main 有内容差异的普通文件可写回；验证拒绝额外路径、目录/链接替换、来源只读侧变化和确认后的输出变化。
- 会话记录位于 `code-version-manager/merge-review/<id>/`，`merge.json` 保存来源、基线、引用与工具关闭状态。合并期间禁止发布、切换和同步。工具退出后才显示“完成合并”“取消合并”。
- 完成前再次校验仓库、工作区、工具输出和 worktree 使用情况；把人工选择写成以原 main 提交为唯一父提交的普通提交，不执行 merge commit，不发布或创建标签，VERSION 取 main 原值。默认删除来源本地分支及其全部 beta 标签；确认选项可关闭以保留分支。
- 提交和删除引用使用完成阶段生成的本地恢复点。清理只影响本地，后续“同步远程”才传播删除。
- 取消删除临时对比副本和会话状态，不创建恢复引用、不改变原检出分支、工作树、index、提交、分支或标签。
- Beyond Compare 使用内置 `data/bcompare/BCSessions.xml`；普通历史目录比较配置保持独立。

## 分支删除

- “删除分支”只列出本地非 main 分支。选择目标后直接在后台删除，不再弹出清单确认框；不要求已合入或已发布，并删除该分支及所属 beta 标签，不删除 main、正式标签或远程引用。
- 删除非当前分支不要求工作区干净，不切换分支，也不改动当前工作区、index 或未跟踪文件。main 有本地修改时仍可删除未检出的开发分支。
- 删除当前检出的分支时，必须先检查工作区干净、main 可安全切换，再切到 main 后删除。其他 worktree 正在使用目标分支时仍拒绝删除。
- `plan_branch_deletion()` 校验分支和引用快照；`delete_branch()` 保留旧引用与元数据恢复点，并用旧 OID 校验批量删除。失败保留恢复资料；远程删除由后续整仓同步传播。

## 整仓同步

| 操作 | 来源 | 目标 |
| --- | --- | --- |
| 同步本地 | origin | 所有本地分支、正式标签、beta 标签变为远程状态 |
| 同步远程 | 本地 | 所有远程分支、正式标签、beta 标签变为本地状态 |

- 两个方向都新增、替换、删除受管分支和可解析版本标签；来源不存在的目标引用也删除。来源必须有有效 main。
- 不按时间或版本号自动选方向。检查时逐引用展示领先、落后、分叉、无共同历史及标签变化；整仓差异可能有多个方向。
- 只管理 `refs/heads/*` 与可解析版本标签。非版本标签、其他 remote、stash、恢复引用不参与覆盖；不得使用 `git push --mirror` 或泛化的引用删除。
- `RemoteSyncSnapshot.refs` 是全部受管引用；`branch_ref` 只记录操作前工作分支，不能缩小覆盖范围。所有同步入口共用状态、快照校验、备份和恢复流程。
- 每次同步状态检查读取远程完整引用并同步更新分支选择器中的 origin 分支。没有独立“刷新远程分支”按钮；检查失败清空远程分支选项，避免显示过期信息。generation、仓库实例和快照用于丢弃旧后台结果。
- origin 使用非交互 SSH。连接/权限检查 120 秒，传输默认 900 秒，可在仓库配置 60–3600 秒。推送原子更新并使用精确 `--force-with-lease`，refspec 不得带 `+`；本地批量更新使用旧 OID 校验。
- 同步本地先记录忽略文件冲突，再 stash 暂存、未暂存和未跟踪文件；可能被覆盖的忽略文件移入恢复目录。恢复用 `stash apply --index`，冲突不删除恢复点。其他 worktree 占用待更新分支时阻止覆盖。
- 覆盖前备份受管引用、配置、manifest 和内部 CHANGELOG。远程传输 Git 历史和引用，不传 manifest；缺 manifest 的远程版本从 Git 完整树导出并重建内部 CHANGELOG。
- 远程状态来自 `RemoteSyncStatus.remote_refs`。同步按钮禁用/着色应通过统一状态机更新，成功后刷新分支、历史、修改、冲突和按钮状态。

## 元数据、GTK 与错误处理

- `code-version-manager/versions/` 存放按完整标签名分层的 manifest；内部 CHANGELOG 从标签说明生成；`recovery/` 保存配置、版本清单、引用恢复点和同步资料。
- 用户配置位于 `GLib.get_user_config_dir()/code-version-manager/settings.ini`，通常为 `~/.config/code-version-manager/settings.ini`。保存“设置”本身不改仓库身份；Git 账号对话框按姓名/邮箱分别预填非空用户默认值。
- CHANGELOG 发布前先校验路径类型、生成同目录临时文件；成功后用实际标签历史原子替换根目录文件并保留权限。手动下载仍保留现有文件覆盖确认；标签说明编辑和标签删除只重建内部 CHANGELOG，根目录文件可手动重新下载。
- 旧仓库没有根目录 CHANGELOG 忽略规则时，发布会在 Git `info/exclude` 补充 `/CHANGELOG.rst`，不改用户 `.gitignore`，避免自动生成文件阻挡下一次分支切换。已跟踪的旧 CHANGELOG 在新发布中停止跟踪。
- 发布前 CHANGELOG 路径非法或临时文件准备失败时不创建发布提交、标签，不改原 VERSION 和 index；若最终替换发生异常，会明确提示版本已发布及重新下载入口，保留原 CHANGELOG，避免误导用户重复发布。
- `run()` / `run_bytes()` 使用 `git -C <目录>` 和参数列表。业务错误用具体中文 `GitError`，不把 traceback 展示为用户提示，不拼接不可信 shell 命令。
- 远程读取、发布、切换、合并、同步及分支删除都在后台线程运行；工作线程不得操作 GTK，通过 `GLib.idle_add` 回主线程。新建分支按钮只在当前分支为 main 时启用，后端 `create_branch()` 也独立校验。
- `run_background()` 负责忙碌状态、耗时提示和后台线程调度；成功、失败与特定结果回调经 `GLib.idle_add` 回主线程刷新界面。不要在 worker 内访问 GTK。
- branch generation、remote status generation、仓库实例和操作快照共同防止旧后台结果覆盖当前状态。刷新优先通过统一状态机重建 UI；同步按钮状态由 `RemoteSyncStatus` 驱动。
- 本地与 origin 同名分支合成一行；当前分支以绿色标识，不在标签文字添加“当前”。

## 测试、兼容与打包

- `tests/test_backend.py` 覆盖解析、仓库后端、发布、身份配置、远程验证、GTK 回调和文件安全；`tests/test_workflow.py` 使用临时工作仓库与 bare origin 覆盖整仓同步及并发变化；`tests/test_merge_comparison.py` 覆盖隔离目录合并；`tests/verify_workspace_copy.py` 只操作验证副本和临时 origin。
- `legacy_test_*` 是历史流程用例，不会被 unittest 自动发现，不作为当前合并规则或验证通过的依据；新增用例应使用 `test_*`。配置相关测试应指向临时 settings.ini，避免依赖实际用户设置。
- 常规验证运行完整 unittest、源码 `py_compile` 和 `git diff --check`。实仓验证必须先复制 Git 数据与元数据，不得对来源目录或真实 origin 执行覆盖。
- `VERSION`（项目根目录）是 Debian 软件包版本；`<工作空间>/VERSION` 是被管理仓库中的发布标签显示号，两者不能混用。`build_deb.sh` 从项目根 VERSION 命名 deb 并复制源码、README、INSTALL、图标、桌面入口和 Beyond Compare 默认会话。
- Debian 入口为 `data/code-version-manager`，包控制文件为 `debian/control`，安装/卸载脚本只更新桌面数据库和图标缓存，不清理工作空间或用户配置。
- 兼容基线为 Git 2.25、Python 3.8、GTK 3；避免使用这些版本不支持的 Git 参数、Python 语法或 GTK API。

## 开发与验证

开始先运行 `git status --short`。保留用户已有源码、文档、测试和生成文件，不 reset、checkout 或清理实际用户仓库。业务规则变化时更新 README、INSTALL、AGENTS 和对应测试。

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m py_compile src/code_version_manager.py
python3 tests/verify_workspace_copy.py ~/branch_testing
python3 src/code_version_manager.py
sh build_deb.sh
```

本项目根目录的 CHANGELOG.rst、build/、dist/ 和 Python 缓存被 Git 忽略。更新 CHANGELOG 后不会出现在普通 `git status` 中；不要因此强制纳入 Git，也不要清理用户已有的发行包或生成文件。
