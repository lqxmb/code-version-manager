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
| `tests/test_recovery_cleanup.py` | 恢复文件清理、stash 保护、文件安全、并发变化、失败报告及 GTK 清理回调测试 |
| `tests/test_ui_layout.py` | 工具栏顺序、帮助菜单和内置阅读器、文档路径、最近两项选择、动态比较、独立日志、窗口尺寸记忆及忙碌状态回归 |
| `tests/test_application_updates.py` | 软件发行检查、下载校验、取消、模拟授权安装与 GTK 更新流程 |
| `tests/test_build_config.py` | 更新仓库构建配置、配置异常、下载来源限制及隔离 deb 打包验证 |
| `tests/test_software_releases.py` | 任意软件 Release 目标配置、多架构附件、初始化、预检查、续传、取消及 GTK 发布窗口 |
| `tests/test_version_remarks.py` | 独立版本备注、编辑快照、定向同步、分支隔离、删除恢复及 GTK 备注入口 |
| `tests/test_provider_and_drop.py` | 工作空间文件夹拖放、GitHub/GitLab 地址与主机校验、仓库隔离配置及 GTK 服务商切换 |
| `tests/verify_workspace_copy.py` | 仅在工作区隔离副本和临时远程中验证，不改来源和真实 origin |
| `HELP.md` | 软件使用者帮助：界面操作、版本管理、同步、SSH、恢复和常见问题 |
| `INSTALL.md` | 安装、升级、启动、卸载和安装故障排查 |
| `README.md` | 项目开发、源码运行、测试、隔离验证和 Debian 打包 |
| `README_ai.md`（若存在） | 历史讨论，不作为当前业务规则依据 |
| `VERSION` | 软件发行版本，与受管理工作空间的 Git 版本无关 |
| `build_config.json` | 构建配置，作为本软件更新仓库的唯一配置来源 |
| `data/`、`debian/`、`build_deb.sh` | 图标、启动器、Beyond Compare 配置、`workspace.gitignore` 默认工作区模板及 Debian 打包 |

入口为 `main()`，应用 ID 为 `cn.local.CodeVersionManager`。安装启动器 `/usr/bin/code-version-manager` 加载 `/usr/lib/code-version-manager/code_version_manager.py`。启动工作空间依次读取 `CODE_VERSION_MANAGER_REPOSITORY`、GTK 用户配置 `code-version-manager/settings.ini`、`~/catkin_ws`。最近工作空间最多保存 10 个。GTK 初始化、窗口、对话框及主线程事件回调均位于同一源码文件；后端操作由 `GitRepository` 负责。标题栏唯一“帮助”菜单提供使用帮助、检查更新和关于；内置帮助优先读取 `/usr/share/doc/code-version-manager/HELP.md`，源码运行时回退到项目根目录 `HELP.md`。

## 文档职责与更新规则

- `HELP.md` 是普通软件使用者的唯一应用操作帮助，说明界面、发布、分支、合并、版本删除、恢复、同步、SSH 和常见操作问题；不放构建、测试或开发者打包命令。
- `INSTALL.md` 面向安装、升级和卸载软件的用户或运维人员，只记录系统依赖、预构建 deb 安装、启动、升级、卸载和安装故障排查；应用功能细节链接到 `HELP.md`。
- `README.md` 面向项目开发者和维护者，记录项目简介、源码运行、测试、隔离工作区验证、构建 deb、打包结构和开发入口；不作为应用内帮助的唯一来源。
- `AGENTS.md` 面向编程代理和维护者，保存详细业务规则、源码导航、存储边界、并发约束、测试边界和实现注意事项；不安装到用户 deb 文档目录。
- `CHANGELOG.rst` 只记录版本变更；`VERSION` 只记录软件包版本。不要把用户工作空间的生成文件说明混入软件包版本文件。
- 新增或修改用户可见行为时，先更新 `HELP.md`；涉及安装或升级时更新 `INSTALL.md`；涉及源码、测试或打包流程时更新 `README.md`；业务规则和实现约束同步更新本文件。避免在多个文件复制同一段操作说明。
- `build_deb.sh` 将 `README.md`、`INSTALL.md` 和 `HELP.md` 安装到 `/usr/share/doc/code-version-manager/`；源码运行时帮助从项目根目录回退读取。帮助在应用自己的 GTK 滚动文本窗口中显示，不调用系统 URI 阅读器。

所选目录必须是独立 Git 根目录。初始化用 `git init`，默认分支为 main。已管理仓库的 Git 身份、唯一 origin、服务商、自建 GitLab 主机、推送超时、比较工具和分支基线都保存在仓库本地配置。用户级 `code-version-manager/settings.ini` 另存 GitHub 账号、默认提交姓名和邮箱，作为新启用工作空间的默认值；打开已有仓库的“Git 账号”时，非空默认姓名和邮箱逐项预填，只有保存该窗口才写入仓库本地配置，单独保存设置不会修改已有仓库。应用元数据保存在实际 Git 目录下 `code-version-manager/`，不进入用户工作树或远程同步。

## 源码导航与配置

- 源码依次组织为标签解析与数据类型、用户配置及 SSH 辅助函数、`GitRepository`、发布文件准备及打包选择辅助函数、`FileSelectionDialog`、`VersionManagerWindow` 和应用入口。优先在相应现有方法中扩展业务，不拆分服务或引入包管理框架。
- Git 交互统一经过 `GitRepository.run()` / `run_bytes()`；远程操作用 `_run_remote()` 和 `run_remote_process()` 管理非交互 SSH、超时及传输子进程组清理。
- 用户配置中的 `[git_defaults]` 保存 `github_account`、`user_name` 和 `user_email`；读写设置须保留最近工作空间等其他配置节。GitHub 账号只用于地址示例，不自动设置 origin，也不代表 SSH 登录身份。
- Git 服务商和 GitLab 主机分别保存在仓库本地 `codeversionmanager.gitProvider`、`codeversionmanager.gitlabHost`；服务商取 `github` / `gitlab`，GitLab 主机只接受不含路径的 HTTPS 主机地址。无旧配置时仅从明确的 GitHub/GitLab.com origin 推断，其余默认 GitHub。服务商切换不得自动改写现有 origin。
- 用户配置中的 `[window]` 保存主窗口普通状态的 `width`、`height` 及独立的 `maximized`；通过 `window_settings()` / `save_window_settings()` 读写，不能写入仓库 Git 配置。记录是应用级的，不按工作空间区分；未配置或尺寸无效时回退为 1120 × 720，不按屏幕占比重新计算。
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
| 恢复文件清理 | `plan_recovery_cleanup()`、`clear_recovery_files(expected)`；GTK 的 `on_clear_recovery_files()` 及清理结果回调 |
| 主窗口布局与版本选择 | `_build_header()`、`_build_content()`、`on_history_selection_changed()`、`_update_history_actions()`、`on_compare_selected()` |
| 工作空间拖放打开 | `_install_workspace_drop_target()`、`on_workspace_drop()`、`load_workspace()` |
| Git 服务商与 SSH 地址 | `validate_git_provider()`、`validate_gitlab_host()`、`provider_*()`；仓库 `git_provider()` / `configure_remote()`；GTK `on_git_account()` |
| 版本备注 | `version_remark()`、`preview_version_remark()`、`update_version_remark()`；GTK 的 `prompt_version_remark()` 及备注后台回调 |
| 主窗口尺寸记忆 | `window_settings()`、`save_window_settings()`；GTK 的 `_on_window_configure()`、`_on_window_state()`、`_flush_window_settings()` |
| 操作状态与独立日志 | `_set_operation_status()`、`on_operation_log()`、`_refresh_operation_log()`、`_operation_log_text()` |

- 用户 settings.ini 中 `[repository] path` 记录上次工作空间，`[recent_workspaces] paths` 保存 JSON 数组；路径会展开、去重并过滤不存在目录。删除最近记录只改列表，不删除工作空间或改写上次启动路径。
- 对比工具默认 Beyond Compare，通过“设置”保存到仓库本地配置；只接受预设 Beyond Compare、Meld、KDiff3。GitHub 创建入口仅打开网页，默认建议私有仓库，不代用户创建或授权远程。
- SSH 公钥辅助只读取有效普通 `.pub` 文件，拒绝符号链接；生成 Ed25519 密钥在临时目录完成，再以不覆盖方式发布成对文件。不得记录私钥或口令。
- 公钥窗口的发现、复制和生成成功提示均按所选 GitHub/GitLab 服务商显示；选择 `.pub` 仅控制展示和复制，不设置 SSH 默认身份。首次主机信任由用户核对官方或实例管理员公布的指纹，软件不自动信任或关闭主机校验。
- 主窗口拖放只接受单个 `file:` URI 对应的本地目录；文件、多项、非本地 URI 或 busy 状态必须拒绝并调用 `Gtk.drag_finish()`，不切换工作空间。
- “Git 账号”的服务商和 GitLab HTTPS 主机按当前仓库保存；旧配置通过 GitHub/GitLab.com origin 推断。更改服务商只改变建议、创建页和公钥设置入口，不得自动改写 origin；远程认证错误按实际远程主机和已配置 GitLab 主机判断。
- 版本清单按完整标签名分层存为 `versions/<tag>.manifest`；beta 路径包含 `branches/<编码分支名>/...`。标签对象更新后只失效受影响清单，避免继续使用旧清单。
- `manifest()` 返回 `None` 表示没有可用清单，应使用完整 Git 树；返回 `[]` 表示明确的空清单，应导出空包。不得用同一个真假判断合并两种含义。
- 恢复引用在 `refs/code-version-manager/recovery/<id>/<操作>/before/`，配套资料在实际 Git 目录 `code-version-manager/recovery/<id>/`，包括 git-config、state.json、versions 和内部 CHANGELOG。它们仅存在本机，不随日常或高级同步上传。
- 发布选择器按顶层目录分组，后端展开成文件；删除范围另经 `selected_scopes` 明确传入。导出优先使用 manifest，否则用 Git 完整树；大 ZIP 通过磁盘临时文件和流复制过滤，避免整包载入内存。
- `storage_size()` 统计实际 Git 存储及元数据，`allocated_disk_usage()` 按 inode 去重并优先使用已分配块大小；它不是工作树源文件总大小。

### 存储范围速查

| 数据 | 保存位置与生命周期 | 修改约束 |
| --- | --- | --- |
| 上次工作空间、最近列表、Git 默认身份、主窗口尺寸 | 用户级 `code-version-manager/settings.ini`，跨启动保留 | 修改一个节时保留其他节；不修改已有仓库身份 |
| origin、比较工具、同步超时、分支基线 | 当前仓库本地 Git 配置 | 通过现有后端入口读写；不是用户级默认值 |
| 清单、内部 CHANGELOG、恢复资料、手动合并会话 | 实际 Git 目录下 `code-version-manager/` | 使用 `metadata_path` / `_git_path()`，不假定工作空间 `.git` 一定是目录 |
| 工作空间 `VERSION`、`CHANGELOG.rst` | 工作树根目录，按当前分支重新生成 | 不进入新发布提交或导出；普通界面刷新不隐式写入 |
| 操作日志 | 主窗口 `operation_logs` 内存，按工作空间路径分组 | 每组最多 200 条；关闭日志窗口保留，退出应用丢弃 |
| 版本备注 | `refs/notes/code-version-manager/remarks/<完整规范版本标签>`，独立 Git notes 树 | 显式参与所属分支同步；不进入工作树或代码发布提交 |

## 版本备注

- `HistoryEntry.remark` 默认为空；“提交”右侧的“备注”列双击编辑，支持多行自由文字、快捷填入和清空。列表压为单行、悬停显示全文；其他分支或继承版本双击只读查看。
- main 可编辑全部正式版备注，包括共享正式基线；开发分支仅可编辑自己的 beta。备注校验不复用禁止共享正式标签改写的 `_assert_version_editable()`。
- `VersionRemarkSnapshot` 固定仓库路径、分支、完整标签及对象 OID、代码提交 OID、备注引用 OID 和原文字；执行重新校验，并用旧 OID 事务更新。通过 HEAD.lock 保护编辑最后阶段的当前分支，不改工作树或 index，允许普通未提交修改。
- 每版本有独立引用，notes 树将文字关联代码提交。同一提交的多个标签分别记录；标签说明变化不丢备注，指向其他提交时不读取旧备注。写入使用固定的应用身份和时间构造无父 notes 提交，使相同版本、提交及文字得到相同 OID；这些提交不进入任何代码分支。
- `_managed_ref()` 仅额外接纳上述命名空间内可解析版本的备注；`_owned_ref()` 仍只判断版本标签，备注使用 `_owned_remark_ref()`。其他 notes、恢复引用、stash 不参与同步。日常 scope 仅同步本分支备注，all 同步全部；恢复引用保留 notes 原命名路径。
- 仅备注变化时“覆盖本地”直接使用旧 OID 事务更新引用，不切换、stash 或重新生成文件；“覆盖远程”仍使用原子 push 和逐引用精确租约。备注差异有独立比较项，展示两端文字及新增、覆盖或清空，不以 notes 提交判断代码新旧。
- 导入远程分支同时获取对应版本备注；删除版本、撤回提交、删除本地/远程分支及手动合并清理来源时处理所属备注，引用恢复点包含已删除备注。清空备注删除整个该版本备注引用；清空也参与同步。
- 旧仓库无引用时视为空，不迁移；不写 CHANGELOG、manifest、ZIP 或根目录生成文件。默认 clone/push 不传 notes，由应用显式 fetch/push，两端需要新版应用。

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
- `history()` 基于 `version_tags()` 构造已发布标签记录，默认最多 500 项，按数字版本排序，不是完整 Git 提交日志。每条 `HistoryEntry` 保留引用和只读标记；列表重新排序或重建时同步处理选择状态，不把行号当作持久化版本标识。
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
- 删除及撤回只改变本地；之后在该分支选择“覆盖远程”，才按现有原子推送及精确租约规则回退远程同名分支、删除所属版本标签。日常同步不会传播其他分支的删除。

## 恢复文件清理

- 主窗口底部“版本存储”旁的“清空恢复文件”只清理当前工作空间实际 Git 目录下 `code-version-manager/recovery/` 中的历史恢复点及对应的 `refs/code-version-manager/recovery/<id>/` 引用。使用实际 Git 路径，兼容 linked worktree；不接受用户输入任意清理路径。
- 后台 `plan_recovery_cleanup()` 返回不可变 `RecoveryCleanupPlan`，包含根路径及目录身份、可清理项快照、备份占用和保留原因。确认框显示目录、数量和备份文件占用；清理后这些历史恢复点无法再用于恢复。空间估算只包含备份文件，不承诺 Git 对象空间立即释放，不运行 gc、prune、reflog expire 或远程命令。
- 仅处理名称为“时间戳-8 位小写十六进制”的普通恢复点目录；有 `/before/worktree` 引用、仍用于当前同步恢复、或有 `ignored-worktree` 的恢复点保守保留。保护依据来自持久化引用及磁盘，即使重启或已恢复但无法确认也保留。未知名称、符号链接和特殊文件保留并说明原因；仅有引用而无本地备份目录的旧记录不清理。
- `clear_recovery_files(expected)` 重新检查所选目录、文件元信息及引用 OID；变化后要求重新确认，确认后新增的恢复点留待下次。通过 `O_NOFOLLOW` 和目录文件描述符固定根路径及子目录，拒绝根路径替换，递归删除不跟随链接。
- 每个恢复点的引用用旧 OID 和 `option no-deref` 事务删除，再删除备份文件；文件删除失败尝试以 create 事务恢复原引用，保留尚存文件并报告残留位置。部分删除不可声称已完整回滚；结果报告成功、保留、失败及未处理数量，失败后停止继续清理。
- 不更改工作树、index、正常分支、版本标签、版本清单、内部 CHANGELOG 或用户 stash；允许普通未提交修改，存在未完成 Git／手动合并操作时拒绝清理。操作忙碌或无仓库时禁用按钮；无可清理项时说明保留原因。
- 预检查、执行及错误回调校验仓库实例和 generation；后台线程只进行后端操作，主线程确认和刷新存储。清理后的 UI 刷新不启动远程检查。`run_background()` 的可选 `error_callback` 用于此流程，避免旧错误影响新工作空间。

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
- 忽略规则由隔离副本中的临时 Git 仓库执行 `check-ignore --no-index --stdin -z` 解析；禁用系统、用户和全局 excludes，随后删除临时 `.git`。不要改成 Python 自行匹配通配符，否则会改变嵌套规则和反选语义。

### 普通比较与手动合并的差别

- 普通历史比较通过 `materialize()` / `_archive_bytes()` 物化历史版本，有 manifest 时使用清单；无清单才使用完整树。与当前版本比较时右侧是实际可编辑工作空间；两个历史版本比较时使用两个临时副本。
- 手动合并通过 `_review_git_files()` 读取完整跟踪树，之后按两侧忽略规则过滤；不能复用按 manifest 裁剪的导出路径。特殊路径、只读侧和写回输出均有单独校验。
- `prepare_bcompare_config()` 将用户比较工具配置复制到临时配置目录，再替换内置会话；手动合并另用二进制内容比较和空过滤规则。不要改写用户原有 `~/.config/bcompare`。

## 分支删除

- “删除分支”同时列出本地和已检查到的 origin 非 main 分支，同名分支合成一项，远程独有项显示 `retreat_teb（origin）`。main 在本地和远程都禁止通过分支删除入口删除。
- 选择分支后，`preview_branch_deletion()` 在后台校验本地删除条件并检查 origin；以仓库连接结果判断远程可用性，不以通用联网检测代替。GTK 再显示删除确认。
- 本地和远程同名分支都存在时，默认只删除本地；用户可勾选“同时删除 origin 同名分支及其全部 beta 标签”。未勾选后远程条目仍可选择、导入或删除。远程不可读取时仍可只删本地，不推测远程已删除。
- 删除非当前本地分支不要求工作区干净，不切换分支，不改动当前 index 和未跟踪文件。删除当前分支时先检查 main 可安全切换及干净工作区；目标被其他 worktree 使用时拒绝本地删除。
- `plan_branch_deletion()` / `BranchDeletionPlan` 固定本地引用；`delete_branch()` 保留引用和元数据恢复点，再用旧 OID 校验批量删除分支及所属 beta。
- `plan_remote_branch_deletion()` / `RemoteBranchDeletionPlan` 固定远程地址、分支和全部所属 beta 的 OID；`delete_remote_branch()` 下载旧对象并保存恢复引用，复查后以原子推送和精确租约删除这些引用。复查时远程新增 beta、分支变化或 origin 改变则需重新确认；非版本标签、main 和其他分支不参与。
- `delete_branch_with_remote()` 先校验两端，远程成功后再删本地。远程权限拒绝、引用变化等非超时错误不删本地；远程删除超时仍继续删除本地，弹窗提示远程结果待确认；远程成功而本地失败时，明确报告两端结果及恢复位置，不声称两端具有共同事务。原子推送只覆盖确认时列出的引用，无法锁住整个 beta 命名空间；删除后若发现远程又出现该分支或 beta，提示刷新核对，保留新引用。`RemoteTimeoutError` 区分进程超时及 SSH 连接超时，不靠中文报错匹配判断降级；超时后重新打开软件，只要远程仍存在就能重新选择 origin 条目删除，无需内存中的待办记录。
- 手动合并默认只清理本地来源分支。删除本地整个分支后，切换到 main 再点日常“覆盖远程”不会删除远程来源分支；使用上述显式删除或高级整仓覆盖。

## 当前分支同步与高级整仓覆盖

发布始终离线。两个日常同步按钮保留“以来源为准覆盖”的语义，允许回退、分叉覆盖、标签说明替换及删除范围内独有标签，不执行自动 merge/rebase，也不按提交作者确定所有权。

| 当前分支 | 日常受管引用 |
| --- | --- |
| main | `refs/heads/main` 和全部可解析正式标签 |
| 任意开发分支 | 该分支的 `refs/heads/<name>` 及其全部 beta 标签；分支名通过现有解析函数处理 |

- “覆盖本地”以 origin 为准；“覆盖远程”以本地为准。悬停提示明确来源、目标及仅作用于当前分支和所属版本标签、备注；其他分支及所属标签、备注不受影响，不因本地缺失而删除。开发分支继承的正式标签是基线依赖，不是本次写入范围。
- `RemoteSyncSnapshot.scope` 默认为 `branch`，只有高级入口显式选择 `all`。`refs` 始终保留全部本地受管引用，`branch_ref` 固定当前分支。所有实际读写范围统一经 `_sync_refs()` / `_owned_ref()` 计算，不能把完整快照直接作为日常覆盖集合。
- `RemoteSyncStatus.remote_refs` 保留完整远程引用，供分支选择器显示；`local_changes`、`remote_changes` 和 `comparisons` 仅包含本次范围。不能因为当前分支一致而误报整仓一致。
- 确认及完成校验只比较本次范围和必要正式基线；其他开发分支在后台变化不阻止当前分支同步。当前分支、所属标签、基线或 origin 在确认后变化时拒绝沿用旧确认。
- 开发分支要求两端有 main，正式基线标签对象一致且属于分支和 main 历史。远程空仓库必须先同步 main；缺基线或基线冲突提示先处理 main，不隐式上传 main 或替换正式标签。无 beta 时从创建缓存或第一父链唯一正式版本恢复基线，不猜测多标签创建点。
- 远程缺少当前开发分支时，“覆盖本地”会明确确认删除本地该分支及远程不存在的所属 beta，并返回本地 main；main 的内容和正式标签不随这次同步更新，且须可安全切换。
- 高级“整仓覆盖”保留旧功能：全部受管分支和版本标签以所选来源为准，来源独有项新增，目标独有项删除。必须有有效 main，展示完整覆盖清单，不与日常按钮共用隐含的整仓默认值。
- 高级窗口的两个覆盖按钮仅使用自身 `scope="all"` 的 `RemoteSyncStatus`，不读取主窗口当前分支状态：`synced` 为绿色禁用，悬停只提示“无需覆盖”；`pending` 为红色，分别按 `can_sync_local` / `can_sync_remote` 启用，检查失败的方向保持红色禁用并显示方向原因；检查中或 `unavailable` 为灰色禁用并显示检查原因。弹窗正文保留一条简短范围说明和一条状态结果，不重复展示下载、上传状态。复用红绿灰 CSS 时同时覆盖主要操作行和高级对话框，日常按钮的启用规则保持原值。比较包含全部受管分支、版本标签和备注引用，不包含工作树未提交修改。
- 仅管理 `refs/heads/*`、可解析版本标签及上述版本备注引用；非版本标签、其他 notes、其他 remote、stash、恢复引用均不参与覆盖。禁止 `git push --mirror` 或无范围的引用删除。
- origin 用非交互 SSH。连接/权限检查 120 秒，传输默认 900 秒，可在仓库本地配置为 60–3600 秒。推送使用 `--atomic`、逐引用精确 `--force-with-lease`，refspec 不得带 `+`；不支持原子更新的服务端直接报错，不退回逐项修改。
- `_run_remote()` 仅在默认 SSH 明确报告连接阶段 `Network is unreachable` 时，对同一命令使用 IPv4 重试一次，保留原子推送、精确租约、agent 和主机校验；不修改用户 SSH 配置，不改写自定义 `GIT_SSH_COMMAND`。超时、认证、主机校验和权限错误不自动重试。GitLab 受保护分支拒绝强制推送时提供分支规则及账号权限指引，不自动修改远程规则；dry-run 通过不保证实际覆盖获准。排查步骤统一放在 INSTALL 常见问题。
- 本地引用通过带旧 OID 的 `update-ref --stdin` 事务更新。下载当前分支时，其他本地分支及 tracking、基线缓存和 manifest 保持原值；返回 main 时仅验证其 OID，不将远程 main 写入。
- “覆盖本地”先记录忽略文件冲突，再 stash 暂存、未暂存和未跟踪文件；可能被覆盖的忽略文件移至恢复目录。恢复使用 `stash apply --index`，冲突不删除恢复点。只检查本次会更新或检出的分支是否被其他 worktree 占用。
- 覆盖前备份目标范围的引用、配置、manifest 和内部 CHANGELOG。远程只传 Git 对象和引用，不传 manifest；缺 manifest 的版本从完整 Git 树导出并重建内部 CHANGELOG。
- 每次状态检查同时读取完整远程分支列表。没有独立刷新远程分支按钮；检查失败清空远程选项。generation、仓库实例和操作快照共同丢弃旧结果。
- 同步成功后统一刷新分支、历史、修改、冲突、生成文件和按钮状态。只按当前分支结果设置日常按钮颜色和提示。
- 当前分支定向覆盖仍可回退该分支；main 正式标签改写仍可能破坏其他开发分支基线。精确租约防止确认后的变化，不替用户判断覆盖方向。超时后远程可能已接收更新，先刷新核对再重试，无需重新发布版本。

## 元数据、GTK 与错误处理

### 主窗口、选择与状态

- 标题栏只放应用名称、唯一“帮助”菜单及窗口控制；工作空间工具行放打开、最近、刷新、Git 账号、设置及高级操作菜单。新版提示位于窗口底部状态区。主要操作行按发布、分支选择、新建、合并、删除排序，右侧为单行远程摘要及同步。高级整仓覆盖从高级操作菜单进入。
- 主窗口的 `configure-event` 用 `Gtk.Window.get_size()` 记录普通窗口宽高，避免把客户端装饰尺寸重复计入；`window-state-event` 单独记录最大化。最大化、全屏、最小化的配置事件不覆盖普通尺寸。连续拖动合并为停止 400 毫秒后保存，销毁时移除定时器并补存最新值；只保存变化，写入失败不阻断主窗口关闭。
- 窗口设置通过同目录临时文件、`fsync` 和原子替换保存，保留原文件权限和其他配置节；不记录窗口位置，也不把日志、对话框的临时尺寸混入主窗口记录。
- 历史操作条按范围、动态比较、删除版本、下载版本、修改说明、查看 CHANGELOG、下载 CHANGELOG 排列；保持单行并在窄窗口允许横向滚动，显示“已选 N / 2”。单选比较当前工作空间，双选比较两个版本；删除、下载和修改说明仅单选启用。最近两项按选中时间维护，第三项淘汰最早项，取消重选视为最新，一次加入多项按列表顺序处理；重建历史时清空选择顺序，保留 Ctrl／Shift 的 GTK 选择行为。
- 底部路径和状态必须单行、省略且允许缩小，长结果不得增加窗口最小尺寸。所有操作结果通过 `_set_operation_status()` 更新，完整详情记录为 `OperationLogEntry`，日常刷新和耗时计时使用 `record=False`。非模态独立日志窗口按时间倒序展示、自动换行滚动、支持选择和复制全部，同一主窗口复用日志窗口。日志按工作空间路径在内存隔离，每个最多 200 条，关闭日志窗口保留，退出应用不落盘；失败记录完整错误并保留既有错误、恢复及确认弹窗。
- `run_background()` 的禁用范围须包括工作空间工具行、主要操作行和历史操作条；底部日志入口保持可用。移动同步按钮后 CSS 不得依赖 headerbar 父级；禁用条件继续由仓库、分支、合并状态和 `RemoteSyncStatus` 决定。

### 元数据、生成文件与后台回调

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

### 应用自身联网更新

- 更新源取自项目根目录 `build_config.json` 的 `update_repository`，默认 `lqxmb/code-version-manager`，使用该公开 GitHub 仓库的最新正式 Release。标签为 `v<Debian软件版本>`，附件为 `code-version-manager_<版本>_all.deb` 和同名 `.sha256`。软件发行号仅从应用 VERSION 读取，不能复用工作空间版本标签解析或 Git origin。
- `build_deb.sh` 使用 Python 标准库校验配置为 JSON 对象且仓库为有效 `owner/repo`，缺失或无效时在打包前终止；每次以 `0644` 权限安装配置到 `/usr/lib/code-version-manager/build_config.json`。`application_build_config_path()` 按源码 `src/` 目录或安装程序目录定位唯一文件，不依赖当前工作目录，也不在安装文件缺失时寻找其他配置。应用启动时集中读取配置；API、发行页、附件及重定向校验共用该仓库值，Python 中不得保留备用默认仓库。读取失败保存配置错误，仅更新检查报错，沿用自动静默、手动提示的机制；本地功能保持可用。
- 更新逻辑保留在现有单文件，独立于 GitRepository。使用标准库 HTTPS、证书校验和严格的 GitHub 资产重定向校验；下载流式写入用户缓存中的私有临时目录，校验大小、SHA-256、deb 包名、版本及架构后才提供安装。缓存和安装日志不进入工作树或 Git 元数据。
- 用户 settings.ini 的 `[updates]` 保存 `auto_check` 与 `last_success`，默认启用；显示主窗口 5 秒后检查，成功间隔至少 24 小时。手动检查绕过间隔，自动失败静默；只有成功联网取得有效检查结果才更新检查时间。无 Release 不是“已是最新”。
- 标题栏只保留“帮助”菜单，依次提供“使用帮助”“检查更新”和“关于”；F1 绑定使用帮助。“关于”窗口提供帮助及手动检查更新快捷按钮。帮助在 GTK 可滚动文本窗口中打开，读取系统文档路径并回退源码 HELP.md，不调用系统 URI 阅读器；新版入口显示在主窗口底部，不另占标题栏。
- 更新检查、下载与安装均在后台，GTK 和配置保存回主线程；销毁时取消下载、移除定时器并丢弃回调。工作空间切换不重新检查。检查和下载不占用仓库 busy 状态，安装使用 busy 保护并阻止关闭窗口；后台任务或手动合并未完成时拒绝安装。
- 仅安装版允许调用参数列表形式的 pkexec/apt-get，系统负责管理员授权，不记录密码。执行前重新校验包，安装后核对 dpkg-query 状态和版本，成功才提示重启；失败保留日志和包，不声称回滚系统安装。重启必须先退出旧 Gtk.Application 实例再启动安装入口。
- build_deb.sh 同时生成 deb 和 SHA-256 附件；维护者可用发布窗口或网页上传 Release。VERSION 继续被忽略。更新测试只用模拟网络与模拟安装，不修改实际用户配置、系统包或真实工作空间。

### 任意软件 GitHub Release 发布

- 发布入口位于高级操作，主机固定 github.com，目标接受 owner/repo、仓库或 Releases HTTPS 链接。`GitRepository.release_repository()` 优先读取本地 `codeversionmanager.releaseRepository`，未配置时从明确 GitHub origin 预填，否则为空；`configure_release_repository()` 仅写该本地配置。有效目标在启动发布预检查或仓库创建结果确认后保存。窗口固定打开时的仓库实例与工作空间，不因主窗口切换改变目标或配置保存位置。发布器独立于源码 Git 操作，不读取或上传源码树和历史，不改变 origin。本软件自身更新源只读构建配置，不受发布窗口目标影响。
- gh 为建议依赖；浏览器授权通过系统终端运行 gh auth login --hostname github.com --web --git-protocol ssh --skip-ssh-key。不调用 auth token，不在应用配置、日志或命令行保存访问令牌，不输出 auth status 的原始内容。
- 发布窗口初始化仅通过 `shutil.which()` 检查 gh 及受支持终端，不启动子进程或联网；终端检测与登录共用逻辑，依次使用 gnome-terminal、x-terminal-emulator。缺少任一依赖时，详情区立即显示具体缺项和可复制的 GitHub CLI 官方 Linux 安装说明完整地址；gh 缺失、版本过旧及终端缺失报错也使用源码统一的地址，不指引用户查阅 INSTALL.md。终端缺失另提示安装终端并保留手动登录命令，不阻止已有授权的检查登录和发布。
- 目标输入框旁“创建发行仓库”只要求有效目标，不要求 deb。`preview_repository_creation()` 只读检查登录、CLI 创建能力及目标存在性；不可变 `SoftwareReleaseRepositoryCreationPlan` 固定仓库、账号和可见性，确认框默认公开且允许改为私有。`create_repository()` 复核账号与目标后，经现有 `_run()` 使用 `gh repo create <owner/repo> --public/--private` 创建空仓库，不传 source、push、clone 或初始化参数。当前账号及组织创建权限由 GitHub 校验；已有仓库停止创建，HTTP 404 明确说明“不存在或当前账号无权访问”，不将私有仓库不可访问当作确定不存在。
- 创建请求结束、失败、取消或超时后，用新的取消事件只读核实目标及实际可见性，不重复发送创建请求；`SoftwareReleaseRepositoryCreationResult` 仅在已确认仓库可发布且可见性匹配时携带 target，否则报告结果待确认或实际差异及仓库链接，不删除已创建仓库。确认结果在后台保存到窗口绑定的工作空间，保存配置失败须保留远程结果并明确提示。GTK 保留已选 deb、标题和说明，创建后由用户主动点击“检查并发布”；README 初始化、标签及 Release 仍经原发布确认。
- 后台准备任意有效包名及架构的普通 deb 私有副本及 SHA-256，`SoftwareReleaseBatch` 固定一批相同包名和版本、架构各异的包；附件为 `<包名>_<版本>_<架构>.deb` 及各自 `.sha256`。存在相邻校验文件时必须核对；任一准备失败清理该批临时副本。标签默认 v<Debian版本>，须通过 Git 引用校验，不能复用工作空间标签解析。
- 不可变发布预检查固定目标仓库路径、ID、可见性、必要默认分支提交、已有标签对象及提交、整批附件、草稿、最新正式发行版、标题及说明。发布时目标必须已存在并有写权限；已有空仓库在发布确认后通过 contents API 创建仅说明发行用途的 README 和初始提交，缺少标签时在已确认的目标提交上创建引用，不改写已有标签。失败和取消保留已发生的初始化、标签和草稿，报告阶段及链接；预检查不写远程，执行及发布前重查目标与相关状态。
- 先草稿后公开；只续传唯一同版本草稿，同名附件内容相同才复用，缺失则上传，额外或冲突附件拒绝。已公开版本禁止覆盖，正式新版必须高于当前最新版本。上传失败保留草稿，不删除或 clobber。公开请求失败后复查状态及最新接口，无法核实则报告结果待确认，不重复发送公开请求。
- 公开目标额外通过匿名 HTTPS 检查该目标的最新正式发行、附件大小及精确下载地址，拒绝重定向到其他地址；私有目标仅使用 gh 授权核实并提示客户端下载需要授权。不得调用固定本软件更新源来核实其他软件的发行，不放宽本软件更新下载及安装的校验。
- 发布/登录/准备在独立后台任务执行，GTK 回主线程，固定输入并支持取消；取消子进程时清理进程组。发布活动阻止应用退出和自身更新安装，取消或失败报告已完成阶段及草稿地址。测试仅模拟 gh 与临时文件，禁止创建真实测试 Release。

- `tests/test_backend.py` 覆盖解析、仓库后端、发布、身份配置、远程验证、GTK 回调和文件安全；`tests/test_workflow.py` 使用临时工作仓库与 bare origin 覆盖当前分支同步、显式远程删除、高级整仓覆盖及并发变化；`tests/test_generated_files.py` 覆盖生成文件、旧仓库迁移、分支范围、文件安全和独立忽略模板；`tests/test_merge_comparison.py` 覆盖隔离目录合并；`tests/test_version_deletion.py` 覆盖版本撤回和故障恢复；`tests/test_recovery_cleanup.py` 覆盖恢复清理及受保护备份；`tests/verify_workspace_copy.py` 只操作验证副本和临时 origin，包括清理副本中的恢复目录并核对来源未变。
- `tests/test_ui_layout.py` 使用临时用户配置和真实 GTK 控件，覆盖工具栏顺序、最近两项选择与比较调度、日志隔离和复制、尺寸记忆、最大化保护、写入失败以及忙碌状态。没有显示服务时 GTK 用例会跳过，配置读写用例仍可运行；验证时检查实际跳过情况。
- `legacy_test_*` 是历史流程用例，不会被 unittest 自动发现，不作为当前合并规则或验证通过的依据；新增用例应使用 `test_*`。配置相关测试应指向临时 settings.ini，避免依赖实际用户设置。
- 测试通过 `importlib.util.spec_from_file_location()` 直接加载单文件源码，各测试模块可拥有独立的 `MODULE`；mock 要作用于被测模块，不能假定所有用例共享一次导入。补丁生命周期须覆盖 GTK 销毁及延迟定时器，防止尺寸保存写入真实用户配置。
- 常规验证运行完整 unittest、源码 `py_compile` 和 `git diff --check`。实仓验证必须先复制 Git 数据与元数据，不得对来源目录或真实 origin 执行覆盖。
- `VERSION`（项目根目录）是 Debian 软件包版本；`<工作空间>/VERSION` 是被管理仓库的本地生成标签显示号，两者不能混用。`build_deb.sh` 从项目根 VERSION 命名 deb 并复制源码、`build_config.json`、README、INSTALL、HELP、图标、桌面入口、Beyond Compare 默认会话及 `data/workspace.gitignore`。模板安装到 `/usr/lib/code-version-manager/workspace.gitignore`，用户文档安装到 `/usr/share/doc/code-version-manager/`，可在打包前直接修改模板，不改 Python 源码。
- Debian 入口为 `data/code-version-manager`，包控制文件为 `debian/control`，安装/卸载脚本只更新桌面数据库和图标缓存，不清理工作空间或用户配置。
- `build_deb.sh` 使用 `dpkg-deb --root-owner-group` 生成 `Architecture: all` 包，当前版本的暂存目录为 `build/code-version-manager_<VERSION>_all/`，产物为 `dist/code-version-manager_<VERSION>_all.deb`。默认比较工具列为建议依赖；SVG 图标、桌面入口和启动脚本均来自 `data/`。
- 兼容基线为 Git 2.25、Python 3.8、GTK 3；避免使用这些版本不支持的 Git 参数、Python 语法或 GTK API。

## 开发与验证

依赖来自系统 APT：python3、python3-gi、gir1.2-gtk-3.0、git、openssh-client；比较工具另装。不使用 pip/venv 作为项目运行要求。无显示会话时部分 GTK 用例会跳过，需要界面回归时可使用已安装的 Xvfb。

开始先运行 `git status --short`。保留用户已有源码、文档、测试和生成文件，不 reset、checkout 或清理实际用户仓库。业务规则变化时更新 README、INSTALL、AGENTS 和对应测试。

- 以当前工作树为准核对实现，不能因改动尚未提交就将其视为可丢弃；用户已经回退代码时，先重读当前文件，再叠加新需求。
- `tests/verify_workspace_copy.py` 先计算来源指纹，在独立 Git 副本及临时 origin 中操作，完成后再核对来源指纹；素材须有 main，元数据含链接时拒绝复制。脚本中的 reset、整仓覆盖和恢复清理只允许作用于验证副本。
- 窗口与配置修改可先运行 `python3 -m unittest discover -s tests -p 'test_ui_layout.py'`；涉及提交、同步、合并、恢复或生成文件时选择对应专项测试，再执行完整验证。测试命令、交互启动、隔离实仓验证和 Debian 打包是不同步骤，不要把启动主窗口或构建 deb 当作单元测试。

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m py_compile src/code_version_manager.py
python3 tests/verify_workspace_copy.py /path/to/workspace
python3 src/code_version_manager.py
sh build_deb.sh
```

本项目根目录的 VERSION、CHANGELOG.rst、build/、dist/ 和 Python 缓存被 Git 忽略。更新这些本地文件后不会出现在普通 `git status` 中；不要因此强制纳入 Git，也不要清理用户已有的发行包或生成文件。
