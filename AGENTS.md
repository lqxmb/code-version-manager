# Code Version Manager 项目指南

## 1. 项目与目录

Python 3 + GTK 3 桌面 Git 工作空间管理器，支持离线发布、分支开发、版本比较/导出和可选 SSH 整仓同步。运行代码有意集中在一个模块，不引入额外服务或 Python 包管理框架。

| 路径 | 职责 |
| --- | --- |
| `src/code_version_manager.py` | 数据模型、GitRepository、辅助函数、GTK 窗口、应用入口 |
| `tests/test_backend.py` | 后端、比较工具和 GTK 行为回归测试 |
| `tests/test_workflow.py` | 固定基线、合并不发布、整仓覆盖及恢复专项测试 |
| `tests/verify_workspace_copy.py` | 实仓隔离副本验证工具，不修改来源及其真实 origin |
| `README.md` / `INSTALL.md` | 当前使用说明 / Ubuntu 安装与故障排查 |
| `README_ai.md` | 历史讨论，不作为当前业务规则依据 |
| `VERSION` | 软件发行版本，以文件实时内容为准，不等同于被管理工作空间的版本 |
| `build_deb.sh` / `debian/` | Debian 打包、依赖和安装脚本 |
| `data/` | 启动器、图标、desktop 文件和 Beyond Compare 默认配置 |

源码入口为 `main()`，应用 ID 为 `cn.local.CodeVersionManager`。安装入口是 `/usr/bin/code-version-manager`，加载 `/usr/lib/code-version-manager/code_version_manager.py`。启动工作空间优先级为 `CODE_VERSION_MANAGER_REPOSITORY`、GTK 配置目录的 `code-version-manager/settings.ini`、`~/catkin_ws`。最近工作空间最多 10 个。

工作空间必须是所选目录自己的 Git 根目录。初始化使用独立 `git init`，初始分支为 main。身份、origin、推送超时、比较工具、分支基线缓存均只写仓库本地配置。旧 `codeversionmanager.syncVersionFile` 不再控制发布行为。

## 2. 固定的开发流程

1. main 发布正式标签，例如 `v0.0.3`。
2. 新建开发分支时明确选择 main 的正式标签，作为固定基线。
3. branch1 依次发布 `branch1/v0.0.3-beta.0.0.1`、`...2`、`...3`；main 后续发布到 `v0.0.6` 不改变开发分支基线。
4. 将 branch1 的最新已发布 beta 三方合并到 main；用户处理冲突、标记解决并完成合并。
5. **完成合并只创建 main 合并提交，不更新 VERSION，不发布、不创建任何标签。**
6. 用户另行点击“发布正式版本”，才生成 `v0.0.7` 并写入 `VERSION=0.0.7`。
7. 新正式版本包含分支全部提交后，经确认清理本地开发分支及其全部 beta 标签。下一次整仓同步远程传播删除。

### 标签和基线

- 正式标签是 `v数字.数字.数字...`；预发布实际引用为 `branches/<URL编码的分支名>/v0.0.3-beta.0.0.N`，界面显示 `branch1/v0.0.3-beta.0.0.N`。
- 必须用 `parse_version_tag()`、`canonical_version()`、`version_tag_name()`，不要手工拆分标签或 URL 编码分支名。
- 新发布只允许 `beta.0.0.N`；旧 `beta.1` 可读，不自动迁移。序号按数字递增。
- `create_branch(name, base_version)` 从所选正式标签提交创建分支；`branch_base()` 校验固定基线。
- 基线缓存键是 `branch.<name>.codeVersionManagerBase`。同步本地后清理旧缓存，从唯一 beta 基线恢复；无 beta 时从第一父链最近的 main 正式标签恢复。
- 多基线、标签不属于分支历史、与 main 无共同历史、浅克隆等均应明确报错，不能猜测或静默重写。
- 默认历史显示本分支标签及可达的正式基线。继承的正式版本和其他分支版本只读；全部版本范围仅改变展示。

### VERSION 与发布

- main 正式发布必须生成/更新根目录 VERSION，内容是不带 v 的正式版本号并换行，自动加入提交、清单及 ZIP。
- 开发分支不改写 VERSION；保留创建时继承的正式号。拒绝发布包含 VERSION 修改、删除或特殊类型变更的开发版本。
- main 的 VERSION 必须是普通文件，不得是符号链接、目录或被忽略；失败时恢复原文件和未选择的暂存内容。
- `save_version()` 使用临时 index，只提交所选文件、选定顶层删除范围及必要的 VERSION；保留其他暂存内容。发布从不访问远程。
- 根目录 CHANGELOG.rst 仅作说明输入和下载目标，不纳入新提交、manifest 或 ZIP。首次发布补齐模板和 .gitignore；已有 .gitignore 保留并补充 CHANGELOG 忽略规则。

## 3. 合并与清理

业务约束集中在 GitRepository；GTK 只采集输入、展示确认、调度后台任务。

- `plan_merge()` / `BranchMergePlan` 固定来源 beta 标签及 OID、目标 main 正式版本及 OID、引用快照，确认后重新校验。
- 来源分支 HEAD 必须等于最新 beta 提交；有未发布尾部提交时要求先发布 beta。main 有未发布提交时先发布，不能回退到旧正式标签。
- 合并仅使用本地引用，不隐式 fetch、快进或上传。需要更新远程状态时由用户先整仓同步。
- 使用 `git merge --no-ff --no-commit` 保留双方历史；干净合并可完成合并提交，有冲突则等待人工处理。VERSION 保留 main 的值；旧 main 缺 VERSION 时不引入来源 beta 文件。
- Beyond Compare、Meld、KDiff3 三方输入允许缺失 stage 用空文件表示；退出工具不代表解决，必须明确标记文件，随后 `complete_merge()` 或 `abort_merge()`。
- `merge.json` 同时记录人工待确认文件。合并期间禁止发布、切换和同步。
- 清理要求位于 main、工作区干净、分支全部合入且 main 最新正式标签已包含结果。默认清理分支及全部所属 beta 标签，仅改本地，保存引用和元数据恢复点。
- 单独编辑/删除共享正式标签仍受保护；整仓覆盖有独立的显式确认语义。

## 4. 整仓同步的唯一语义

| 入口 | 来源准本 | 目标行为 |
| --- | --- | --- |
| 同步本地 | origin | 全部本地分支、正式标签、beta 标签变为远程状态 |
| 同步远程 | 本地 | 全部远程分支、正式标签、beta 标签变为本地状态 |

- 两个方向都新增、替换和删除引用；来源不存在的目标分支或版本标签也删除。来源必须有有效 main。
- 同步不根据时间或版本号自动选方向。逐引用按提交祖先关系展示领先、落后、分叉、无共同历史和标签信息变更；整仓可能存在不同方向的差异。
- 用户确认整仓覆盖后，共享正式标签也采用来源状态，不再因某个开发分支共享旧标签而禁用整个方向。
- 只管理 `refs/heads/*` 与可解析的版本标签；非版本标签、其他 remote、stash、工具恢复引用不参与覆盖。不要使用 `git push --mirror` 或泛化的全部引用删除。
- `RemoteSyncSnapshot` 的 refs 是整仓受管快照；branch_ref 只记录操作前工作分支，不能用于缩小同步范围。`RemoteSyncStatus` 汇总全部差异并绑定快照。
- 所有同步入口共用状态检查、快照校验和恢复流程，不留绕过校验的快速推送路径。
- 远程使用非交互 SSH、唯一 origin；读取/权限预检查 120 秒，传输默认 900 秒，仓库可配置为 60–3600 秒。失败提示包含脱敏 Git 输出。
- 确认后及下载后重新核对两端引用；推送使用原子更新与精确 `--force-with-lease`；推送 refspec 不得再加 `+`，否则可能绕过租约。本地批量更新使用旧 OID 校验，任何锁定失败都不应用引用批次。
- 待更新/删除分支被其他 worktree 使用时阻止同步本地。当前分支被来源删除时回到 main。
- 覆盖前保存全部目标受管引用、配置和元数据。同步本地先 stash 暂存/未暂存修改及未跟踪文件；可能被覆盖的忽略文件移入恢复目录。记录忽略冲突必须早于 stash，以免 .gitignore 恢复改变分类。
- 恢复暂存内容使用 `stash apply --index`；恢复冲突不删除恢复点。超时不能证明远程未接收，先刷新再重试。

## 5. 元数据、界面和错误约定

- 元数据位于实际 Git 目录的 `code-version-manager/`；versions 下的 manifest 按完整标签名分层，内部 CHANGELOG 从标签说明生成，recovery 下保存操作资料和 Git 配置。
- 远程同步的是 Git 提交和引用，不传输本地 manifest。标签改变时失效对应清单；没有清单的远程版本按 Git 完整目录导出，并重建内部 CHANGELOG。
- 历史目录比较保持只读，当前工作区可编辑。Beyond Compare 使用 data/bcompare/BCSessions.xml，排除 .git、ZIP、__pycache__。
- `run()` / `run_bytes()` 统一使用 `git -C <目录>` 和参数列表。业务错误用具体中文 GitError，不把 traceback 当用户提示，不拼接不可信 shell 命令。
- 远程读取、发布、切换、合并、同步、删除预检都放后台线程；工作线程不操作 GTK，通过 GLib.idle_add 回主线程。
- branch_generation、remote_status_generation、仓库实例及整仓快照共同丢弃旧后台结果。成功后 refresh 分支、历史、修改、冲突状态和同步按钮；不要直接绕过统一状态机启用按钮。
- 同名本地/origin 分支合并显示一行，当前分支绿色。选择已有本地分支优先普通安全切换。

## 6. 开发与验证

开始先执行 `git status --short`。保留用户源码、文档和测试改动，不 reset、checkout 或清理实际用户仓库。

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m py_compile src/code_version_manager.py
python3 tests/verify_workspace_copy.py ~/branch_testing
python3 src/code_version_manager.py
sh build_deb.sh
```

测试优先用临时工作仓库和 bare origin，覆盖完整 0.0.3→beta→main 0.0.6→仅合并→手动发布 0.0.7→清理→同步流程；覆盖无共同历史、标签改写、同步删除、并发变化、原子失败、恢复资料、worktree 占用及旧后台结果。实仓验证只读取来源，复制 Git 和元数据，在隔离副本和临时 origin 中执行；不得指向真实远程推送。

修改业务规则必须同时更新 README、INSTALL、AGENTS 和对应测试。Git 2.25 / Python 3.8 / GTK 3 为兼容基线；不要使用新版 Git 才有的事务子命令或未经兼容验证的 Python API。
