# Code Version Manager 项目指南

## 项目概况

本项目是 Python 3、GTK 3 桌面应用，用于管理任意独立 Git 工作空间，提供本地离线发布、固定基线开发分支、手动目录合并、版本比较与 ZIP 导出，以及可选 SSH 当前分支同步和高级整仓覆盖。应用代码有意集中在 `src/code_version_manager.py`，不引入额外服务或 Python 包管理框架。

| 路径 | 职责 |
| --- | --- |
| `src/code_version_manager.py` | 版本标签解析、数据类型、GitRepository 后端、GTK 界面和应用入口；Git 操作与界面有意集中于单文件 |
| `tests/test_backend.py` | Git 后端、发布、同步、恢复和 GTK 回归测试 |
| `tests/test_workflow.py` | 临时工作仓库与 bare origin 的完整流程测试 |
| `tests/test_generated_files.py` | 生成文件、旧仓库迁移、文件安全、忽略模板及 CHANGELOG 下载测试 |
| `tests/test_merge_comparison.py` | 手动合并隔离副本、忽略规则、提交及取消测试 |
| `tests/test_version_deletion.py` | 末尾版本撤回、暂存保护、固定基线、确认快照、恢复及 GTK 删除回调测试 |
| `tests/verify_workspace_copy.py` | 仅在工作区隔离副本和临时远程中验证，不改来源和真实 origin |
| `README.md` / `INSTALL.md` | 用户说明与 Ubuntu 安装、故障排查 |
| `README_ai.md`（若存在） | 历史讨论，不作为当前业务规则依据 |
| `VERSION` | 软件发行版本，与受管理工作空间的 Git 版本无关 |
| `data/`、`debian/`、`build_deb.sh` | 图标、启动器、Beyond Compare 配置、`workspace.gitignore` 默认工作区模板及 Debian 打包 |

入口为 `main()`，应用 ID 为 `cn.local.CodeVersionManager`。安装启动器 `/usr/bin/code-version-manager` 加载 `/usr/lib/code-version-manager/code_version_manager.py`。启动工作空间依次读取 `CODE_VERSION_MANAGER_REPOSITORY`、GTK 用户配置 `code-version-manager/settings.ini`、`~/catkin_ws`。最近工作空间最多保存 10 个。GTK 初始化、窗口、对话框及主线程事件回调均位于同一源码文件；后端操作由 `GitRepository` 负责。

所选目录必须是独立 Git 根目录。初始化用 `git init`，默认分支为 main。已管理仓库的 Git 身份、唯一 origin、推送超时、比较工具和分支基线都保存在仓库本地配置。用户级 `code-version-manager/settings.ini` 另存 GitHub 账号、默认提交姓名和邮箱，作为新启用工作空间的默认值；打开已有仓库的“Git 账号”时，非空默认姓名和邮箱逐项预填，只有保存该窗口才写入仓库本地配置，单独保存设置不会修改已有仓库。应用元数据保存在实际 Git 目录下 `code-version-manager/`，不进入用户工作树或远程同步。

## 源码导航与配置

- 源码依次组织为标签解析与数据类型、用户配置及 SSH 辅助函数、`GitRepository`、发布文件准备及打包选择辅助函数、`FileSelectionDialog`、`VersionManagerWindow` 和应用入口。优先在相应现有方法中扩展业务，不拆分服务或引入包管理框架。
- Git 交互统一经过 `GitRepository.run()` / `run_bytes()`；远程操作用 `_run_remote()` 和 `run_remote_process()` 管理非交互 SSH、超时及传输子进程组清理。
- 用户配置中的 `[git_defaults]` 保存 `github_account`、`user_name` 和 `user_email`；读写设置须保留最近工作空间等其他配置节。GitHub 账号只用于地址示例，不自动设置 origin，也不代表 SSH 登录身份。
- 仓库本地配置键包括 `user.name`、`user.email`、`codeversionmanager.pushTimeout`、`codeversionmanager.compareTool`、`codeversionmanager.lastWorkingBranch` 及各分支的 `codeVersionManagerBase`。提交身份记录作者；SSH 密钥负责远程授权。

## 常见修改入口与存储边界

| 修改内容 | 优先定位 |
| --- | --- |
| 分支同步和高级覆盖 | `sync_snapshot()`、`remote_sync_status()`、`sync_local_from_remote()`、`sync_remote_from_local()`；GTK 的 `_sync_direction()`、`on_advanced_sync()` |
| 分支选择和删除 | `combine_branch_choices()`、`switch_branch()`、`preview_branch_deletion()`；GTK 的 `_refresh_branch_choices()`、`on_delete_branch()` |
| 发布和打包文件选择 | `save_version()`、`prepare_workspace_release_files()`、`top_level_package_groups()`、`expand_package_groups()` |
| 根目录生成文件 | `_prepare_generated_files()`、`_install_generated_files()`、`_generated_file_transition()`、`refresh_workspace_generated_files()` |
| 比较、导出和 CHANGELOG | `materialize()`、`compare()`、`export()`、`changelog_text()`、`download_changelog()` |
| 手动合并 | `plan_merge()`、`merge_to_main()`、`prepare_merge_comparison()`、`complete_merge()`、`abort_merge()` |
| 版本删除与提交撤回 | `preview_version_deletion()`、`delete_version()`、`delete_latest_version_commit()`；GTK 的 `on_delete_version()`、`_background_version_deletion_ready()` |

- 用户 settings.ini 中 `[repository] path` 记录上次工作空间，`[recent_workspaces] paths` 保存 JSON 数组；路径会展开、去重并过滤不存在目录。删除最近记录只改列表，不删除工作空间或改写上次启动路径。
- 对比工具默认 Beyond Compare，通过“设置”保存到仓库本地配置；只接受预设 Beyond Compare、Meld、KDiff3。GitHub 创建入口仅打开网页，默认建议私有仓库，不代用户创建或授权远程。
- SSH 公钥辅助只读取有效普通 `.pub` 文件，拒绝符号链接；生成 Ed25519 密钥在临时目录完成，再以不覆盖方式发布成对文件。不得记录私钥或口令。
- 版本清单按完整标签名分层存为 `versions/<tag>.manifest`；beta 路径包含 `branches/<编码分支名>/...`。标签对象更新后只失效受影响清单，避免继续使用旧清单。
- 恢复引用在 `refs/code-version-manager/recovery/<id>/<操作>/before/`，配套资料在实际 Git 目录 `code-version-manager/recovery/<id>/`，包括 git-config、state.json、versions 和内部 CHANGELOG。它们仅存在本机，不随日常或高级同步上传。
- 发布选择器按顶层目录分组，后端展开成文件；删除范围另经 `selected_scopes` 明确传入。导出优先使用 manifest，否则用 Git 完整树；大 ZIP 通过磁盘临时文件和流复制过滤，避免整包载入内存。
- `storage_size()` 统计实际 Git 存储及元数据，`allocated_disk_usage()` 按 inode 去重并优先使用已分配块大小；它不是工作树源文件总大小。

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
- 工作区根目录 `VERSION`、`CHANGELOG.rst` 为软件生成的本地状态，不进入新发布提交、manifest、ZIP 或新的远程发布快照。软件自身项目根目录 `VERSION` 是独立的 Debian 发行号；当前项目 `.gitignore` 忽略 `/VERSION`，该文件未受 Git 跟踪，但启动和打包仍读取它。不得把两种 VERSION 混用，也不要擅自改变其跟踪状态。
- `current_version_tag()` 优先查 HEAD 上属于当前分支的标签或正式标签，否则沿第一父链查最近正式标签或本分支 beta；无 beta 的开发分支校验固定正式基线。无标签返回空，不伪造版本。
- `workspace_version_value()` 将正式版转换成不带 v 的号（`0.0.3`），beta 转换成未编码分支名与标签（`feature/camera/v0.0.3-beta.0.0.1`）。
- `refresh_workspace_generated_files()` 统一刷新两个根目录文件。切换本地/远程分支、从 main 创建分支、发布、完成合并、删除分支、修改/删除标签及分支同步或整仓覆盖完成后调用，不在 GTK 普通刷新中隐式写文件。不创建提交或改变用户源文件的暂存选择。
- VERSION 采用当前分支最近已发布标签；根目录 CHANGELOG 只含当前分支可达的正式版本和本分支 beta，不含其他分支 beta。无标签时移除过期 VERSION，生成“尚无版本”的 CHANGELOG。内部 metadata CHANGELOG 仍含全仓库版本历史。
- `prepare_workspace_release_files()` 在启用和发布前准备说明模板与忽略规则。新 `.gitignore` 从 `data/workspace.gitignore` 复制；已有文件保留内容与换行，仅补齐 `/VERSION`、`/CHANGELOG.rst`。规则迁移自动纳入本次发布，即使用户未选 `.gitignore`。
- 默认忽略内容不写入 Python 多行常量。`workspace_gitignore_template_path()` 分别查源码 data 目录和安装目录；模板缺失明确报错。打包前可编辑模板，用户自定义默认规则只用于新建 `.gitignore`；两个生成文件的必要忽略项始终补齐。
- 发布使用临时 index，只提交所选文件、选定顶层删除范围和必要 `.gitignore` 迁移；显式从临时 index 移除历史跟踪的 VERSION、CHANGELOG，保留其他暂存内容。旧提交不重写，同步仍传输所选引用可达的旧历史；旧 manifest 和无 manifest 的导出也过滤两个根目录文件。
- 切换仍要求用户源文件干净。`_user_status()` 排除两个生成路径并关闭重命名折叠，避免隐藏涉及源文件的移动。`_generated_file_transition()` 临时规范旧树中的生成路径以安全切换，失败恢复原生成文件与 index；成功后从目标标签重新生成，不使用 assume-unchanged 或 skip-worktree。
- 发布始终离线。根目录 CHANGELOG 仍是发布说明输入；发布窗口读取匹配章节，最终以用户确认的改动内容写标签说明。发布后、分支切换及其他刷新点覆盖根目录文件；手写草稿须先保存在发布窗口或另行备份。

## 版本删除与提交撤回

| 操作 | 适用条件 | 分支、文件与暂存区 |
| --- | --- | --- |
| 仅删除版本 | 当前分支拥有的版本；共享正式标签仍受保护 | 仅删除标签及对应 manifest，不回退分支，保留 Git 提交及源文件 |
| 删除版本及提交 | main 的正式版或当前开发分支自己的 beta，且标签指向当前 HEAD | 当前分支退回唯一父提交，保留源文件及未跟踪文件；有暂存修改时原 index 保持不变，否则 index 更新到父提交，被撤回提交的改动成为未暂存或未跟踪文件 |

- “末尾”按提交关系判断，不按版本号最大值或历史列表行号判断。只从当前分支历史撤回一个提交，不清除其他引用、恢复点或 reflog 中的 Git 对象。
- `preview_version_deletion()` 返回不可变 `VersionDeletionPlan`，固定分支名、标签对象 OID、HEAD、父提交及开发分支正式基线；`can_delete_commit` 和 `commit_error` 决定按钮状态及中文禁用原因。标签允许删除但提交不允许撤回时，仍可选择“仅删除版本”。
- 两种删除方法均接受可选 `expected` 快照，执行前重新核对；分支、标签对象（包括仅说明变化）、HEAD 或必要基线变化后重新确认。其他分支标签及继承或共享的正式版本仍禁止单独删除。
- 提交撤回另拒绝历史中间提交、首个或多父提交、同提交多标签、浅克隆、冲突 index、未完成 Git/手动合并操作及其他 worktree 占用。
- 提交撤回使用 `delete-version-commit` 恢复点，保存旧引用、配置、manifest、内部 CHANGELOG、原始 index 和 `version-deletion.json`；其中 `index_exists` 区分原 index 是否存在。持有真实 `index.lock` 防止暂存区并发写入；无暂存修改时先用临时 index 准备父提交树，再以带旧 OID 的引用事务回退分支并删除标签，最后安装 index，不检出或覆盖源文件。
- index 安装失败时用精确 OID 尝试回滚引用；并发变化导致回滚失败时保留新引用并报告恢复位置。引用已完成后元数据或生成文件失败，明确提示“已删除”及刷新方法，不要求用户重复删除。
- 撤回开发分支最后一个 beta 时，分支继续存在，保留固定正式基线；导入分支缺缓存时补存原基线，再刷新 VERSION 与 CHANGELOG。不存在 manifest 也能删除；其他版本的 manifest 不清理。
- GTK 先经 `run_background()` 做预检查，回主线程确认实际分支及回退 OID；执行仍在后台。确认回调和确认结束后都校验仓库实例，取消操作只刷新界面。
- 删除及撤回只改变本地；之后在该分支选择“同步远程”，才按现有原子推送及精确租约规则回退远程同名分支、删除所属版本标签。日常同步不会传播其他分支的删除。

## 手动合并

合并业务规则由 `GitRepository` 执行，GTK 负责输入、确认和后台调度。

- `plan_merge()` / `BranchMergePlan` 固定来源最新 beta 标签与 OID、main 最新正式版本与 OID、完整受管引用快照。来源 HEAD 必须等于最新 beta；main HEAD 必须等于最新正式标签；要求干净工作区、完整历史和共同历史。
- 合并不 fetch、上传、切换分支或执行 `git merge`。确认后从来源 beta 与 main 正式提交建立 Git 元数据目录中的双目录隔离会话：来源侧只读，main 侧编辑。
- 合并输入是 Git 跟踪的完整树，不按 manifest 或 export-ignore 裁剪。符号链接、子模块和特殊文件使用只读说明，不跟随链接。
- 两侧树中的 `.gitignore` 按 Git 规则分别解析并取并集。命中路径从两边对比副本中隐藏并禁止写回；规则和受保护路径在会话建立时固定。Beyond Compare、Meld、KDiff3 接收相同的过滤后目录。
- `VERSION`、`CHANGELOG.rst` 和相同内容文件不可编辑。只有相对 main 有内容差异的普通文件可写回；验证拒绝额外路径、目录/链接替换、来源只读侧变化和确认后的输出变化。
- 会话记录位于 `code-version-manager/merge-review/<id>/`，`merge.json` 保存来源、基线、引用与工具关闭状态。合并期间禁止发布、切换和同步。工具退出后才显示“完成合并”“取消合并”。
- 完成前再次校验仓库、工作区、工具输出和 worktree 使用情况；把人工选择写成以原 main 提交为唯一父提交的普通提交，不执行 merge commit，不发布或创建标签，随后按 main 最近正式标签刷新本地 VERSION 和 CHANGELOG。默认删除来源本地分支及其全部 beta 标签；确认选项可关闭以保留分支。
- 提交和删除引用使用完成阶段生成的本地恢复点。合并清理只影响本地；远程来源分支及 beta 需从“删除分支”选择 origin 条目后显式删除，日常同步 main 不再传播该删除。
- 取消删除临时对比副本和会话状态，不创建恢复引用、不改变原检出分支、工作树、index、提交、分支或标签。
- Beyond Compare 使用内置 `data/bcompare/BCSessions.xml`；普通历史目录比较配置保持独立。

## 分支删除

- “删除分支”同时列出本地和已检查到的 origin 非 main 分支，同名分支合成一项，远程独有项显示 `retreat_teb（origin）`。main 在本地和远程都禁止通过分支删除入口删除。
- 选择分支后，`preview_branch_deletion()` 在后台校验本地删除条件并检查 origin；以仓库连接结果判断远程可用性，不以通用联网检测代替。GTK 再显示删除确认。
- 本地和远程同名分支都存在时，默认只删除本地；用户可勾选“同时删除 origin 同名分支及其全部 beta 标签”。未勾选后远程条目仍可选择、导入或删除。远程不可读取时仍可只删本地，不推测远程已删除。
- 删除非当前本地分支不要求工作区干净，不切换分支，不改动当前 index 和未跟踪文件。删除当前分支时先检查 main 可安全切换及干净工作区；目标被其他 worktree 使用时拒绝本地删除。
- `plan_branch_deletion()` / `BranchDeletionPlan` 固定本地引用；`delete_branch()` 保留引用和元数据恢复点，再用旧 OID 校验批量删除分支及所属 beta。
- `plan_remote_branch_deletion()` / `RemoteBranchDeletionPlan` 固定远程地址、分支和全部所属 beta 的 OID；`delete_remote_branch()` 下载旧对象并保存恢复引用，复查后以原子推送和精确租约删除这些引用。复查时远程新增 beta、分支变化或 origin 改变则需重新确认；非版本标签、main 和其他分支不参与。
- `delete_branch_with_remote()` 先校验两端，远程成功后再删本地。远程权限拒绝、引用变化等非超时错误不删本地；远程删除超时仍继续删除本地，弹窗提示远程结果待确认；远程成功而本地失败时，明确报告两端结果及恢复位置，不声称两端具有共同事务。原子推送只覆盖确认时列出的引用，无法锁住整个 beta 命名空间；删除后若发现远程又出现该分支或 beta，提示刷新核对，保留新引用。`RemoteTimeoutError` 区分进程超时及 SSH 连接超时，不靠中文报错匹配判断降级；超时后重新打开软件，只要远程仍存在就能重新选择 origin 条目删除，无需内存中的待办记录。
- 手动合并默认只清理本地来源分支。删除本地整个分支后，切换到 main 再点日常“同步远程”不会删除远程来源分支；使用上述显式删除或高级整仓覆盖。

## 当前分支同步与高级整仓覆盖

发布始终离线。两个日常同步按钮保留“以来源为准覆盖”的语义，允许回退、分叉覆盖、标签说明替换及删除范围内独有标签，不执行自动 merge/rebase，也不按提交作者确定所有权。

| 当前分支 | 日常受管引用 |
| --- | --- |
| main | `refs/heads/main` 和全部可解析正式标签 |
| 任意开发分支 | 该分支的 `refs/heads/<name>` 及其全部 beta 标签；分支名通过现有解析函数处理 |

- “同步本地”以 origin 为准；“同步远程”以本地为准。其他分支及所属标签不更新、不因本地缺失而删除。开发分支继承的正式标签是基线依赖，不是本次写入范围。
- `RemoteSyncSnapshot.scope` 默认为 `branch`，只有高级入口显式选择 `all`。`refs` 始终保留全部本地受管引用，`branch_ref` 固定当前分支。所有实际读写范围统一经 `_sync_refs()` / `_owned_ref()` 计算，不能把完整快照直接作为日常覆盖集合。
- `RemoteSyncStatus.remote_refs` 保留完整远程引用，供分支选择器显示；`local_changes`、`remote_changes` 和 `comparisons` 仅包含本次范围。不能因为当前分支一致而误报整仓一致。
- 确认及完成校验只比较本次范围和必要正式基线；其他开发分支在后台变化不阻止当前分支同步。当前分支、所属标签、基线或 origin 在确认后变化时拒绝沿用旧确认。
- 开发分支要求两端有 main，正式基线标签对象一致且属于分支和 main 历史。远程空仓库必须先同步 main；缺基线或基线冲突提示先处理 main，不隐式上传 main 或替换正式标签。无 beta 时从创建缓存或第一父链唯一正式版本恢复基线，不猜测多标签创建点。
- 远程缺少当前开发分支时，同步本地会明确确认删除本地该分支及远程不存在的所属 beta，并返回本地 main；main 的内容和正式标签不随这次同步更新，且须可安全切换。
- 高级“整仓覆盖”保留旧功能：全部受管分支和版本标签以所选来源为准，来源独有项新增，目标独有项删除。必须有有效 main，展示完整覆盖清单，不与日常按钮共用隐含的整仓默认值。
- 仅管理 `refs/heads/*` 和可解析版本标签；非版本标签、其他 remote、stash、恢复引用均不参与覆盖。禁止 `git push --mirror` 或无范围的引用删除。
- origin 用非交互 SSH。连接/权限检查 120 秒，传输默认 900 秒，可在仓库本地配置为 60–3600 秒。推送使用 `--atomic`、逐引用精确 `--force-with-lease`，refspec 不得带 `+`；不支持原子更新的服务端直接报错，不退回逐项修改。
- 本地引用通过带旧 OID 的 `update-ref --stdin` 事务更新。下载当前分支时，其他本地分支及 tracking、基线缓存和 manifest 保持原值；返回 main 时仅验证其 OID，不将远程 main 写入。
- 同步本地先记录忽略文件冲突，再 stash 暂存、未暂存和未跟踪文件；可能被覆盖的忽略文件移至恢复目录。恢复使用 `stash apply --index`，冲突不删除恢复点。只检查本次会更新或检出的分支是否被其他 worktree 占用。
- 覆盖前备份目标范围的引用、配置、manifest 和内部 CHANGELOG。远程只传 Git 对象和引用，不传 manifest；缺 manifest 的版本从完整 Git 树导出并重建内部 CHANGELOG。
- 每次状态检查同时读取完整远程分支列表。没有独立刷新远程分支按钮；检查失败清空远程选项。generation、仓库实例和操作快照共同丢弃旧结果。
- 同步成功后统一刷新分支、历史、修改、冲突、生成文件和按钮状态。只按当前分支结果设置日常按钮颜色和提示。
- 当前分支定向覆盖仍可回退该分支；main 正式标签改写仍可能破坏其他开发分支基线。精确租约防止确认后的变化，不替用户判断覆盖方向。超时后远程可能已接收更新，先刷新核对再重试，无需重新发布版本。

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

- `tests/test_backend.py` 覆盖解析、仓库后端、发布、身份配置、远程验证、GTK 回调和文件安全；`tests/test_workflow.py` 使用临时工作仓库与 bare origin 覆盖当前分支同步、显式远程删除、高级整仓覆盖及并发变化；`tests/test_generated_files.py` 覆盖生成文件、旧仓库迁移、分支范围、文件安全和独立忽略模板；`tests/test_merge_comparison.py` 覆盖隔离目录合并；`tests/test_version_deletion.py` 覆盖版本撤回和故障恢复；`tests/verify_workspace_copy.py` 只操作验证副本和临时 origin。
- `legacy_test_*` 是历史流程用例，不会被 unittest 自动发现，不作为当前合并规则或验证通过的依据；新增用例应使用 `test_*`。配置相关测试应指向临时 settings.ini，避免依赖实际用户设置。
- 常规验证运行完整 unittest、源码 `py_compile` 和 `git diff --check`。实仓验证必须先复制 Git 数据与元数据，不得对来源目录或真实 origin 执行覆盖。
- `VERSION`（项目根目录）是 Debian 软件包版本；`<工作空间>/VERSION` 是被管理仓库的本地生成标签显示号，两者不能混用。`build_deb.sh` 从项目根 VERSION 命名 deb 并复制源码、README、INSTALL、图标、桌面入口、Beyond Compare 默认会话及 `data/workspace.gitignore`。模板安装到 `/usr/lib/code-version-manager/workspace.gitignore`，可在打包前直接修改，不改 Python 源码。
- Debian 入口为 `data/code-version-manager`，包控制文件为 `debian/control`，安装/卸载脚本只更新桌面数据库和图标缓存，不清理工作空间或用户配置。
- 兼容基线为 Git 2.25、Python 3.8、GTK 3；避免使用这些版本不支持的 Git 参数、Python 语法或 GTK API。

## 开发与验证

依赖来自系统 APT：python3、python3-gi、gir1.2-gtk-3.0、git、openssh-client；比较工具另装。不使用 pip/venv 作为项目运行要求。无显示会话时部分 GTK 用例会跳过，需要界面回归时可使用已安装的 Xvfb。

开始先运行 `git status --short`。保留用户已有源码、文档、测试和生成文件，不 reset、checkout 或清理实际用户仓库。业务规则变化时更新 README、INSTALL、AGENTS 和对应测试。

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m py_compile src/code_version_manager.py
python3 tests/verify_workspace_copy.py /path/to/workspace
python3 src/code_version_manager.py
sh build_deb.sh
```

本项目根目录的 VERSION、CHANGELOG.rst、build/、dist/ 和 Python 缓存被 Git 忽略。更新这些本地文件后不会出现在普通 `git status` 中；不要因此强制纳入 Git，也不要清理用户已有的发行包或生成文件。
