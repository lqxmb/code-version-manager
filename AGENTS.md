
# Code Version Manager 项目指南

## 项目定位

这是一个使用 Python 3 和 GTK 3 编写的桌面版 Git 工作空间版本管理器。它面向任意工作空间，支持完全离线的本地版本发布，也可以通过 SSH 与 `origin` 远程仓库同步。项目当前没有 Python 包管理配置，主要运行代码集中在一个模块中。

`VERSION` 是软件本身的版本号，当前文件内容为 `0.3.0`。工作空间的版本号是 Git 标签，两者不要混淆。

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `src/code_version_manager.py` | 主程序、Git 后端、GTK 界面、比较/导出逻辑和应用入口。 |
| `tests/test_backend.py` | `unittest` 后端测试；大多数测试在临时目录中创建真实 Git 仓库和远程仓库。 |
| `README.md` | 面向用户的功能、分支版本、远程同步和 CHANGELOG 说明。修改行为后应同步更新。 |
| `INSTALL.md` | Ubuntu 安装、依赖、SSH 和故障排查说明。 |
| `README_ai.md` | 历史开发记录和补充背景；当前使用方法以 `README.md`、代码和 `INSTALL.md` 为准。 |
| `VERSION` | 软件发行版本，供程序和 Debian 打包脚本读取。 |
| `build_deb.sh` | 组装并生成 `dist/code-version-manager_<版本>_all.deb`。 |
| `debian/` | Debian 控制文件和安装/卸载脚本。 |
| `data/` | `.desktop` 启动文件、图标和 Beyond Compare 默认会话配置。 |

## 运行入口和配置

- 源码入口是 `src/code_version_manager.py`，脚本末尾调用 `main()`，创建 `Gtk.Application`，应用 ID 为 `cn.local.CodeVersionManager`。
- 已安装的软件入口是 `data/code-version-manager`，它调用 `/usr/bin/python3 /usr/lib/code-version-manager/code_version_manager.py`。
- 启动工作空间优先级：环境变量 `CODE_VERSION_MANAGER_REPOSITORY`，再是 GTK 配置目录下 `code-version-manager/settings.ini` 保存的路径，最后是 `~/catkin_ws`。
- 最近打开的工作空间最多保存 10 个，配置文件还保存当前工作空间路径。
- 工作空间必须是所选目录自己的 Git 根目录。`GitRepository` 会拒绝打开父仓库中的子目录，启用版本管理时使用 `git init` 并把初始分支设为 `main`。
- 远程地址、Git 提交身份、推送超时、比较工具和 VERSION 同步开关保存于当前仓库的本地 Git 配置，不写入全局 Git 配置。

## 代码分层和重要类型

`src/code_version_manager.py` 是有意保持的单文件结构，修改时按现有区域组织代码：常量/解析函数、数据类、`GitRepository`、文件/工作空间辅助函数、文件选择对话框、`VersionManagerWindow` 和应用入口。

- `GitError` 是面向用户的业务错误类型。Git 命令失败、校验失败和操作前置条件失败应转换为清楚的中文 `GitError`，由 GTK 层显示；不要把原始 traceback 直接展示给用户。
- `BranchInfo` 表示本地或远程分支；`BranchChoice` 将同名本地/远程引用合成一个界面条目，并保留两边的 OID；`VersionTagInfo` 表示完整标签引用、所有者、类型、目标提交和只读状态；`MergeState` 表示合并冲突状态；`BranchDeletionPlan` 表示删除确认前的引用快照。
- `RemoteSyncSnapshot`、`RemoteSyncStatus`、`RefDifference`、`RefComparison` 和 `SyncCommit` 共同表示同步前快照、差异、提交历史关系和确认窗口内容。
- `HistoryEntry` 是版本历史表的行模型，`read_only=True` 表示继承的正式版本或其他分支版本，只能比较和导出。
- `GitRepository.run()`/`run_bytes()` 统一通过 `git -C <工作空间>` 执行 Git。命令参数使用列表传递，不要拼接未经校验的 shell 命令。

## 分支和版本标签规则

- `main` 发布正式版本，标签为全仓库格式，例如 `v0.3.1`。
- 非 `main` 分支只发布自己的预发布版本。正式版为 `v0.3.0` 时，新版本显示为 `test1/v0.3.0-beta.0.0.1`，实际 Git 标签为 `branches/test1/v0.3.0-beta.0.0.1`；beta 只递增最后一段。分支名中的 `/` 使用 URL 编码，解析必须经过 `parse_version_tag()`，不要手工拆字符串。
- 旧的未加前缀标签视为正式标签，不批量迁移；旧 `beta.1` 预发布标签继续可读，但新发布只生成 `beta.0.0.N`。正式版本的下一版本号按全仓库正式标签计算，开发预发布沿用最新正式版本号；没有正式标签时不能创建开发预发布版本。
- `本分支相关版本` 是默认范围：`main` 显示正式标签；开发分支显示自己命名空间内的预发布标签，以及其提交历史可追溯到的正式标签。继承的正式版本和其他分支标签是只读的。
- `全部分支版本` 显示仓库内所有符合规则的正式标签和预发布标签，不代表当前分支拥有这些版本，也不改变 GitHub 标签页的仓库级显示范围。
- 分支下拉框每个分支只显示一行：例如 `main（当前，本地+origin）`、`feature（本地）`、`test1（origin）`。当前分支行使用绿色字体。同名本地/远程引用只是两个 Git 引用层次，不是两个开发分支；选择同名条目优先切换本地分支，选择远程独有条目才下载并建立同名本地跟踪分支。

## 发布、切换和合并

- 发布只写入本地：创建提交、标签、版本清单和内部 CHANGELOG，不自动上传。`save_version()` 使用临时 index，只把用户选择的文件、顶层删除范围和必要的 VERSION 文件加入新提交，同时保留未选择的现有暂存内容。
- 发布前禁止 detached HEAD、未解决冲突、未完成 merge/cherry-pick/revert 等 Git 操作；版本号必须与当前分支的标签类型匹配。
- 创建或切换分支使用 Git 原生 `check-ref-format` 校验，要求已有提交、工作区干净，并使用 `--no-overwrite-ignore` 防止覆盖被忽略文件。切换和创建前会拒绝未暂存、已暂存、未跟踪文件以及未完成的 Git 操作。
- “返回工作分支”使用本地配置 `codeversionmanager.lastWorkingBranch` 记住的最近分支；不存在时打开分支选择，而不是假定永远返回 `main`。
- “合并到 main”会先读取 `origin/main` 和 `origin/<source>`，只允许安全快进本地分支；远程分叉或标签冲突时停止。正式合并使用 `git merge --no-ff --no-commit`，成功后提交合并提交，保留两边历史，原有正式标签不移动。
- 冲突期间禁止切换、发布和同步。`merge_state()` 合并 Git 未解决文件与 `merge.json` 的人工确认路径；已识别的 main 正式 VERSION 优先于开发 beta VERSION，main 缺 VERSION 时移除来源 beta 文件，无法判断时将 VERSION 加入人工待解决清单。Beyond Compare、Meld 或 KDiff3 的三方输入允许缺失 stage 用空文件表示；工具退出不会自动标记已解决，必须检查文件后调用 `resolve_merge_file()`，最后 `complete_merge()` 或 `abort_merge()`。
- 只允许在 `main` 上删除已经完全合入的开发分支。删除前生成引用快照；默认保留该分支预发布标签，可选清理本地/远程分支前缀标签。删除前后的引用和元数据都会保存恢复点。

## 远程同步和保护规则

- 远程同步固定使用唯一的 `origin` SSH 地址和当前分支同名远程分支。远程读取使用非交互环境和超时控制；读取失败、浅克隆或历史不足时不允许危险覆盖。
- 管理范围只包括当前分支和其所属版本标签：`main` 为正式标签，开发分支为自身 `branches/<owner>/...` 标签。其他分支、非版本标签和工作区未提交内容不属于引用同步范围。
- 同步状态必须用提交祖先关系判断本地领先、远程领先、分叉、无共同历史或未知；时间和版本号只用于展示核对，不能用来决定新旧。
- 执行确认后会重新校验本地和远程引用快照。以本地为准使用原子推送、精确 `--force-with-lease`，并先保存远程旧引用；以远程为准先抓取到临时恢复引用，备份本地引用、内部清单和 CHANGELOG，再更新工作区和标签。
- 同步本地时，未提交修改和未跟踪文件会进入 stash 恢复点；可能被远程目标覆盖的被忽略文件会移到恢复目录。同步成功后可选择恢复修改，恢复点不会自动删除。
- 正式标签是仓库级共享引用。若另一个活动分支的提交历史也包含该标签，改写或删除方向必须禁用；可以补齐缺失的共享正式标签。无法确认共享范围时停止同步，不能把未知当成不存在。
- 远程同步不会自动携带范围外标签，也不会自动更新标签。GitHub/Git 服务的 Tags 页面仍按整个仓库列出标签，软件只能在本地历史中按归属筛选。

## 工作空间文件、清单和 CHANGELOG

- 版本管理元数据放在 `.git/code-version-manager/`：`versions/<标签>.manifest` 保存每个版本的文件清单，内部 `CHANGELOG.rst` 由标签说明生成，`recovery/` 保存同步/合并/删除操作的恢复资料。
- 根目录 `CHANGELOG.rst` 只作为发布说明输入和下载目标，不纳入版本提交、清单或 ZIP；启用版本管理时缺失该文件会生成模板，已有内容保留。根目录 `.gitignore` 缺失时会生成 Catkin、缓存、日志、ZIP 和 CHANGELOG 忽略规则，已有规则不覆盖。
- VERSION 同步是按仓库保存的开关。开启时发布会把根目录 `VERSION` 写为不带 `v` 的标签版本号并加入版本包；必须拒绝符号链接、目录和被 `.gitignore` 忽略的 VERSION。
- 版本导出和比较以 manifest 为准，使用 Git archive 生成完整 ZIP 或临时目录；比较当前工作区时当前目录可编辑，历史快照应保持只读。

## GTK 界面和线程约定

- `VersionManagerWindow` 负责布局、按钮敏感状态、历史/修改列表和对话框。主窗口包含工具栏、分支栏、合并冲突栏、远程状态、版本历史/当前修改标签页和底部状态栏。
- 远程分支读取、远程状态检查、发布、分支切换、合并、同步和删除等可能阻塞的 Git 操作统一使用 `run_background()` 在线程中执行；工作线程不能直接操作 GTK，完成后使用 `GLib.idle_add()` 回主线程。
- `branch_generation`、`remote_status_generation` 和快照比较用于丢弃旧的后台结果，切换工作空间、分支或开始新操作时必须使旧请求失效，避免错误分支重新启用同步按钮。
- `refresh()` 应在成功操作后刷新分支、历史、当前修改、合并状态和远程同步状态。同步状态刷新期间按钮应禁用；不要绕过统一状态机直接改变同步按钮。
- GTK 窗口当前使用内嵌 CSS；比较工具设置默认 Beyond Compare，也支持 Meld/KDiff3。Beyond Compare 启动时使用 `data/bcompare/BCSessions.xml` 临时配置，并排除 `.git`、ZIP 和 `__pycache__`。

## 开发和验证

```bash
# 运行全部后端测试
python3 -m unittest discover -s tests -p 'test*.py'

# 只做语法检查，不启动 GTK
python3 -m py_compile src/code_version_manager.py

# 从源码启动（需要 GTK 运行环境）
python3 src/code_version_manager.py

# 构建 Debian 包；输出到 dist/
./build_deb.sh
```

测试应优先扩展 `tests/test_backend.py` 的真实 Git 场景。涉及远程同步时使用临时 bare 仓库和本地 `origin`，并验证引用、标签、工作区、恢复点和失败后的不变性；不要依赖开发机现有仓库或网络。涉及界面状态时，至少覆盖后台结果过期、分支切换后历史范围和同步按钮刷新。

## 修改注意事项

- 开始工作前先执行 `git status --short`，当前工作树可能包含用户未提交的文档、源码和测试改动；不要用 `reset --hard`、`checkout` 或清理命令覆盖它们。
- 业务约束应放在 `GitRepository`，GTK 层只负责输入、确认、后台调度和展示；不要在按钮回调中重复实现 Git 安全检查。
- 修改版本标签格式、同步范围、恢复点或共享标签保护时，必须同时检查 README、INSTALL 和对应后端测试，避免文档与引用实际行为不一致。
- 用户可见错误和状态文字保持中文、具体并说明处理方法。远程错误需要保留经过脱敏的 Git 输出，不要泄露账号、私钥或认证信息。
- 新增 Git 操作时优先复用现有快照、精确租约、恢复引用和 `GitError` 机制；任何会覆盖工作区、分支或标签的操作都必须在执行前重新校验引用。
