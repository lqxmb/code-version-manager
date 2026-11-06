# Code Version Manager 项目指南

## 项目概况

本项目是 Python 3、GTK 3 桌面应用，用于管理任意独立 Git 工作空间，提供本地离线发布、固定基线开发分支、手动目录合并、版本比较与 ZIP 导出，以及可选 SSH 当前分支同步和高级整仓覆盖。应用代码有意集中在 `src/code_version_manager.py`，不引入额外服务或 Python 包管理框架。

| 路径 | 职责 |
| --- | --- |
| `src/code_version_manager.py` | 版本标签解析、数据类型、GitRepository 后端、GTK 界面和应用入口；Git 操作与界面有意集中于单文件 |
| `tests/test_backend.py` | Git 后端、发布、同步、恢复和 GTK 回归测试 |
| `tests/test_workflow.py` | 临时工作仓库与 bare origin 的高级整仓覆盖流程测试 |
| `tests/test_scoped_sync.py` | 当前分支同步、双人协作、两端删除、超时与 GTK 回归 |
| `tests/test_merge_comparison.py` | 手动合并隔离副本、忽略规则、提交及取消测试 |
| `tests/verify_workspace_copy.py` | 仅在工作区隔离副本和临时远程中验证，不改来源和真实 origin |
| `README.md` / `INSTALL.md` | 用户说明与 Ubuntu 安装、故障排查 |
| `README_ai.md` | 历史讨论，不作为当前业务规则依据 |
| `VERSION` | 软件发行版本，与受管理工作空间的 Git 版本无关 |
| `data/`、`debian/`、`build_deb.sh` | 图标、启动器、Beyond Compare 配置、`workspace.gitignore` 默认工作区模板及 Debian 打包 |

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
4. 手动合并完成后由用户另行发布 main 正式版本。合并本身不发布、不打标签；完成后按 main 最近正式标签刷新本地生成文件。

- 正式标签形如 `v数字.数字.数字...`。Beta 标签实际引用形如 `branches/<URL编码分支名>/v0.0.3-beta.0.0.N`；显示名用未编码分支名。
- 用 `parse_version_tag()`、`canonical_version()`、`version_tag_name()` 处理标签格式，不手工拆分或编码分支名。
- 新 beta 只生成 `beta.0.0.N`，按数字递增；旧 `beta.1` 可读，不自动迁移。
- `create_branch(name, base_version)` 从所选正式标签提交创建分支，`branch_base()` 校验固定基线。缓存键为 `branch.<name>.codeVersionManagerBase`。
- 多基线、无共同历史、标签不属于分支历史、浅克隆等情况明确报错，不猜测或静默重写。
- 默认历史范围显示当前分支相关标签及其正式基线；其他分支版本和继承的正式版本只读。“全部分支版本”只改变展示范围。
- 工作区根目录 `VERSION`、`CHANGELOG.rst` 为软件生成的本地状态，不进入新发布提交、manifest、ZIP 或新的远程发布快照。软件自身项目根目录 `VERSION` 是 Debian 发行号，保持独立且仍受 Git 跟踪。
- `current_version_tag()` 优先查 HEAD 上属于当前分支的标签或正式标签，否则沿第一父链查最近正式标签或本分支 beta；无 beta 的开发分支校验固定正式基线。无标签返回空，不伪造版本。
- `workspace_version_value()` 将正式版转换成不带 v 的号（`0.0.3`），beta 转换成未编码分支名与标签（`feature/camera/v0.0.3-beta.0.0.1`）。
- `refresh_workspace_generated_files()` 统一刷新两个根目录文件。切换本地/远程分支、从 main 创建分支、发布、完成合并、删除分支、修改/删除标签及同步完成后调用，不在 GTK 普通刷新中隐式写文件。不创建提交或改变用户源文件的暂存选择。
- VERSION 采用当前分支最近已发布标签；根目录 CHANGELOG 只含当前分支可达的正式版本和本分支 beta，不含其他分支 beta。无标签时移除过期 VERSION，生成“尚无版本”的 CHANGELOG。内部 metadata CHANGELOG 仍含全仓库版本历史。
- `prepare_workspace_release_files()` 在启用和发布前准备说明模板与忽略规则。新 `.gitignore` 从 `data/workspace.gitignore` 复制；已有文件保留内容与换行，仅补齐 `/VERSION`、`/CHANGELOG.rst`。规则迁移自动纳入本次发布，即使用户未选 `.gitignore`。
- 默认忽略内容不写入 Python 多行常量。`workspace_gitignore_template_path()` 分别查源码 data 目录和安装目录；模板缺失明确报错。打包前可编辑模板，用户自定义默认规则只用于新建 `.gitignore`；两个生成文件的必要忽略项始终补齐。
- 发布使用临时 index，只提交所选文件、选定顶层删除范围和必要 `.gitignore` 迁移；显式从临时 index 移除历史跟踪的 VERSION、CHANGELOG，保留其他暂存内容。旧提交不重写，同步仍传输所选引用需要的旧历史；旧 manifest 和无 manifest 的导出也过滤两个根目录文件。
- 切换仍要求用户源文件干净。`_user_status()` 排除两个生成路径并关闭重命名折叠，避免隐藏涉及源文件的移动。`_generated_file_transition()` 临时规范旧树中的生成路径以安全切换，失败恢复原生成文件与 index；成功后从目标标签重新生成，不使用 assume-unchanged 或 skip-worktree。
- 发布始终离线。根目录 CHANGELOG 仍是发布说明输入；发布窗口读取匹配章节，最终以用户确认的改动内容写标签说明。发布后、分支切换及其他刷新点覆盖根目录文件；手写草稿须先保存在发布窗口或另行备份。

## 手动合并

合并业务规则由 `GitRepository` 执行，GTK 负责输入、确认和后台调度。

- `plan_merge()` / `BranchMergePlan` 固定来源最新 beta 标签与 OID、main 最新正式版本与 OID、完整受管引用快照。来源 HEAD 必须等于最新 beta；main HEAD 必须等于最新正式标签；要求干净工作区、完整历史和共同历史。
- 合并不 fetch、上传、切换分支或执行 `git merge`。确认后从来源 beta 与 main 正式提交建立 Git 元数据目录中的双目录隔离会话：来源侧只读，main 侧编辑。
- 合并输入是 Git 跟踪的完整树，不按 manifest 或 export-ignore 裁剪。符号链接、子模块和特殊文件使用只读说明，不跟随链接。
- 两侧树中的 `.gitignore` 按 Git 规则分别解析并取并集。命中路径从两边对比副本中隐藏并禁止写回；规则和受保护路径在会话建立时固定。Beyond Compare、Meld、KDiff3 接收相同的过滤后目录。
- `VERSION`、`CHANGELOG.rst` 和相同内容文件不可编辑。只有相对 main 有内容差异的普通文件可写回；验证拒绝额外路径、目录/链接替换、来源只读侧变化和确认后的输出变化。
- 会话记录位于 `code-version-manager/merge-review/<id>/`，`merge.json` 保存来源、基线、引用与工具关闭状态。合并期间禁止发布、切换和同步。工具退出后才显示“完成合并”“取消合并”。
- 完成前再次校验仓库、工作区、工具输出和 worktree 使用情况；把人工选择写成以原 main 提交为唯一父提交的普通提交，不执行 merge commit，不发布或创建标签，随后按 main 最近正式标签刷新本地 VERSION 和 CHANGELOG。默认删除来源本地分支及其全部 beta 标签；确认选项可关闭以保留分支。
- 提交和删除引用使用完成阶段生成的本地恢复点。清理只影响本地，远程来源分支须通过“删除分支”中的 origin 条目显式删除，日常同步 main 不传播其他分支删除。
- 取消删除临时对比副本和会话状态，不创建恢复引用、不改变原检出分支、工作树、index、提交、分支或标签。
- Beyond Compare 使用内置 `data/bcompare/BCSessions.xml`；普通历史目录比较配置保持独立。

## 日常同步与高级整仓覆盖

### 方向与范围

同步始终单向覆盖，不自动合并两个人对同一分支的修改。确认后允许新增、替换、回退和删除，不能用提交时间或版本号自动选择准本。

| 入口 / 当前分支 | 同步本地（origin 为准） | 同步远程（本地为准） |
| --- | --- | --- |
| 日常按钮 / main | 本地 main 与全部正式标签变为远程状态 | 远程 main 与全部正式标签变为本地状态 |
| 日常按钮 / 开发分支 | 本地当前分支与所属全部 beta 标签变为远程状态 | 远程当前分支与所属全部 beta 标签变为本地状态 |
| 高级整仓覆盖 | 全部本地受管分支与版本标签变为远程状态 | 全部远程受管分支与版本标签变为本地状态 |

- 来源缺少的范围内目标引用也删除；范围外分支及其标签不受影响。本地落后或从未下载其他分支，不能导致这些远程分支被覆盖或删除。
- `sync_snapshot()`、`sync_remote()` 默认 `scope="current"`；整仓调用必须显式传入 `scope="all"`。`RemoteSyncSnapshot.refs` 仅含写入范围，`dependencies` 记录只读正式基线，`branch_ref` 固定操作前工作分支。禁止将完整 `remote_refs` 直接作为日常操作目标。
- main 同步和整仓覆盖的来源必须有有效 main。开发分支同步、首次导入不补入或改写正式标签；固定基线必须属于两端相应历史，且完整正式标签 OID 一致，说明不同也算冲突。缺失、多基线或冲突时提示先处理 main，不能猜测或重写固定基线。
- 远程已删除当前开发分支时，同步本地确认后删除该本地分支及全部 beta，返回现有本地 main；main 本身不随此次下载更新。遗留远程 beta 不阻止清理本地。
- 顶部“高级整仓覆盖”独立执行：后台全量检查 → 用户选择本地或远程准本 → 完整覆盖、回退及删除清单 → 用户确认。其状态不替代日常按钮的当前范围状态。

### 安全、恢复与界面状态

- 仅管理 `refs/heads/*` 和可解析版本标签。非版本标签、其他 remote、stash、恢复引用不参与覆盖；禁止 `git push --mirror` 或泛化删除引用。
- 每次状态检查读取完整远程引用，用 `RemoteSyncStatus.remote_refs` 更新 origin 分支列表；对象获取和提交详情检查限定在当前范围及必要基线。读取失败清空远程选项，基线阻塞仍可展示已成功读取的 origin 列表。
- 确认后复核当前范围完整引用集合、OID、当前检出分支、origin 和正式基线；当前范围增删引用或改变 OID 时停止。其他分支正常更新不阻止日常同步。整仓确认仍校验全部受管引用。
- origin 使用非交互 SSH。读取/权限预检查 120 秒，传输默认 900 秒，仓库配置可设 60–3600 秒。推送必须原子更新并使用精确 `--force-with-lease`，refspec 不带 `+`；推送使用已确认的 origin 地址，避免命名 remote 的 fetch 映射或 mirror 配置隐式改动本地引用。本地事务使用旧 OID 校验。
- 同步本地先记录忽略文件冲突，再 stash 暂存、未暂存和未跟踪文件；可能被覆盖的忽略文件移入恢复目录。恢复用 `stash apply --index`，冲突不删除恢复点。其他 worktree 占用待更新分支或返回目标时停止。
- 覆盖前备份目标旧引用、配置、manifest 和内部 CHANGELOG。远程传输所选引用所需 Git 历史，不传 manifest；缺清单的远程版本从完整 Git 树导出并重建内部历史。
- 跟踪引用、manifest、分支配置和基线缓存的更新须遵守操作范围；开发分支同步保留固定基线，不能运行全量清理影响其他分支。
- 日常按钮禁用/着色以当前范围状态为准；成功后刷新分支、历史、修改、冲突、生成文件和按钮。generation、仓库实例及快照共同丢弃过期后台结果。

## 分支删除

### 选择与执行

- 选择器合并本地和 origin 同名条目，只提供非 main 分支；main 和正式标签不能通过此入口删除。
- 删除前后台检查 origin。远程存在同名分支时提供“同时删除远程分支及全部 beta 标签”，默认不勾选。仅删本地后保留 `分支名（origin）`；可选择 origin 独有条目明确确认删除远程及所属 beta。
- 无远程配置、离线或检查失败仍允许只删本地。删除不要求已合入或已发布。合并后来源清理始终只操作本地，远程清理由用户另行显式删除。
- 删除非当前本地分支不要求工作区干净，不切换分支，不改变当前工作树、index 或未跟踪文件。删除当前分支必须工作区干净、main 可安全切换；其他 worktree 使用目标分支时拒绝。远程独有删除不切换本地分支。
- `BranchDeletionPlan` 固定目标分支及 beta 的两端引用、origin、远程检查结果和本地切换保护；其他分支正常变化不使该清单失效。`BranchDeletionResult` 记录本地完成状态、远程结果与恢复位置。
- 两端删除顺序：本地安全预检及恢复点 → 远程旧引用备份和复核 → 原子远程删除及核验 → 本地旧 OID 校验删除。权限拒绝、确认后引用变化等非超时错误停止并保留本地；部分完成必须明确报告。

### 超时与重试

| 远程结果 | 本地处理 | 用户提示 |
| --- | --- | --- |
| 删除成功 | 继续删除本地 | 两端完成 |
| 复查或恢复备份超时，尚未发出删除 | 继续本地安全校验及删除 | 远程删除未发起，恢复连接后刷新重试 |
| 删除请求或后续核验超时 | 继续本地安全校验及删除 | 远程结果待确认 |
| 权限拒绝、引用变化等非超时错误 | 停止，保留本地 | 具体错误及恢复位置 |

- `_run_remote()` 以 `RemoteTimeoutError(GitError)` 标识超时并记录阶段；禁止匹配中文错误文字判断超时。
- 超时不伪造远程成功；刷新或重启后重新读取 origin，实际已删除的条目消失，仍存在的条目可再次显式删除。恢复记录保留远程是否发出删除、完成状态和错误。

## 元数据、GTK 与错误处理

- `code-version-manager/versions/` 存放按完整标签名分层的 manifest；内部 CHANGELOG 从标签说明生成；`recovery/` 保存配置、版本清单、引用恢复点和同步资料。
- 用户配置位于 `GLib.get_user_config_dir()/code-version-manager/settings.ini`，通常为 `~/.config/code-version-manager/settings.ini`。保存“设置”本身不改仓库身份；Git 账号对话框按姓名/邮箱分别预填非空用户默认值。
- 两个根目录生成目标仅允许普通文件或不存在；符号链接、目录、FIFO 等在发布、切换或其他引用变更前拒绝。生成使用同目录临时文件、fsync 与原子替换，保留权限；两文件替换中途失败时尝试恢复已替换文件。
- 发布前先准备两个输出临时文件，路径或写入准备失败不创建提交/标签；失败提交保留原生成文件、index，并还原本次 `.gitignore` 修改。若发布完成后最终替换失败，明确提示已发布，无需重复发布；修复后重新切换当前分支刷新两个本地生成文件。
- 旧分支尚无新 `.gitignore` 时，`info/exclude` 补齐两个生成路径作为本地兜底，避免额外修改分支文件。已跟踪的旧生成文件仅在后续发布中停止跟踪。
- 手动“下载 CHANGELOG”调用 `download_changelog(destination, all_versions=...)`，范围与历史选择器一致：`current` 导出 `version_tags()` 可见标签，`all` 导出本地全部正式版和 beta；不自动 fetch。GTK 保存对话框默认目录为当前工作空间、默认名称 `CHANGELOG.rst`，允许选择目录和文件名并启用覆盖确认；取消不写入，确认后经 `run_background()` 导出。后端校验所选目标为普通文件或不存在，保留原子替换、文件权限和失败清理。
- 根目录自动刷新仍使用 `current_branch_only=True`，只生成当前分支历史；手动下载额外接受历史范围选项。标签说明编辑和删除成功后，同时更新内部历史与根目录生成文件；发布窗口继续从根目录 CHANGELOG 读取匹配章节。
- `run()` / `run_bytes()` 使用 `git -C <目录>` 和参数列表。业务错误用具体中文 `GitError`，不把 traceback 展示为用户提示，不拼接不可信 shell 命令。
- 远程读取、发布、切换、合并、同步及分支删除都在后台线程运行；工作线程不得操作 GTK，通过 `GLib.idle_add` 回主线程。新建分支按钮只在当前分支为 main 时启用，后端 `create_branch()` 也独立校验。
- `run_background()` 负责忙碌状态、耗时提示和后台线程调度；成功、失败与特定结果回调经 `GLib.idle_add` 回主线程刷新界面。不要在 worker 内访问 GTK。
- branch generation、remote status generation、仓库实例和操作快照共同防止旧后台结果覆盖当前状态。刷新优先通过统一状态机重建 UI；同步按钮状态由 `RemoteSyncStatus` 驱动。
- 本地与 origin 同名分支合成一行；当前分支以绿色标识，不在标签文字添加“当前”。

## 测试、兼容与打包

- `tests/test_backend.py` 覆盖解析、仓库后端、发布、身份配置、远程验证、GTK 回调和文件安全；`tests/test_workflow.py` 使用临时工作仓库与 bare origin 覆盖高级整仓覆盖及并发变化；`tests/test_scoped_sync.py` 覆盖双人分支隔离、基线、两端删除、超时与 GTK 回归；`tests/test_generated_files.py` 覆盖生成文件、旧仓库迁移、分支范围、文件安全和独立忽略模板；`tests/test_merge_comparison.py` 覆盖隔离目录合并；`tests/verify_workspace_copy.py` 只操作验证副本和临时 origin。
- `legacy_test_*` 是历史流程用例，不会被 unittest 自动发现，不作为当前合并规则或验证通过的依据；新增用例应使用 `test_*`。配置相关测试应指向临时 settings.ini，避免依赖实际用户设置。
- 常规验证运行完整 unittest、源码 `py_compile` 和 `git diff --check`。实仓验证必须先复制 Git 数据与元数据，不得对来源目录或真实 origin 执行覆盖。
- `VERSION`（项目根目录）是 Debian 软件包版本；`<工作空间>/VERSION` 是被管理仓库的本地生成标签显示号，两者不能混用。`build_deb.sh` 从项目根 VERSION 命名 deb 并复制源码、README、INSTALL、图标、桌面入口、Beyond Compare 默认会话及 `data/workspace.gitignore`。模板安装到 `/usr/lib/code-version-manager/workspace.gitignore`，可在打包前直接修改，不改 Python 源码。
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
