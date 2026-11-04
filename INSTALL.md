# Code Version Manager 安装与故障排查

适用于 Ubuntu 20.04 及具备相应依赖的环境。支持本地离线发布，远程 SSH 同步为可选功能，不要求 ROS、Catkin 或 VS Code。

## 1. 依赖

| 必需软件 | 用途 |
| --- | --- |
| python3 ≥ 3.8 | 运行程序 |
| python3-gi、gir1.2-gtk-3.0 | GTK 3 界面 |
| git | 提交、标签、分支、手动合并提交和导出 |
| openssh-client | 可选 SSH 远程同步 |

依赖在 debian/control 中声明，APT 可自动补齐。目录比较和双栏冲突处理可选择 Beyond Compare、Meld、KDiff3，需要另行安装；默认 Beyond Compare。

```bash
python3 --version
git --version
ssh -V
python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; print('GTK 3 可用')"
```

## 2. 构建、安装与启动

在项目目录执行；版本号从 VERSION 读取：

```bash
sh build_deb.sh
sudo apt install ./dist/code-version-manager_$(cat VERSION)_all.deb
code-version-manager
```

安装或升级前退出已运行的版本管理器。使用 APT 安装可自动检查依赖，缺失依赖需要可访问 Ubuntu 软件源；离线安装需事先准备相应软件包。不要依赖 dpkg -i 自动下载依赖。

安装后也可在应用菜单打开 Code Version Manager。源码启动：

```bash
python3 src/code_version_manager.py
```

## 3. 正确的版本管理流程

先切换到当前检出的 main，再从 main 的某个正式版本创建开发分支，固定 beta 基线。例如 branch1 从 v0.0.3 创建，即使 main 已到 v0.0.6，仍只生成 branch1/v0.0.3-beta.0.0.N。只能从 main 新建分支。切换和新建要求工作区干净；有未提交、暂存或未跟踪修改时，先提交或另行保存后再切换。

合并前显示最新已发布 beta 和 main 正式版本/提交；开发分支有新提交时先发布 beta。确认后直接打开目录对比工具：左侧 beta 只读，右侧为 main 编辑副本。逐项手动同步并保存，关闭工具后在应用中选择“完成合并”或“取消合并”。

两侧 `.gitignore` 规则按 Git 语义分别解析并取并集。忽略路径在对比两侧都隐藏，也不能写回。手动合并期间工作区、暂存区、提交和分支不会变化；取消会清理本次副本而不更改仓库内容。

**完成合并会把选择的文件写成 main 的普通单父提交，不执行 Git merge，不发布、不创建 Tag。** VERSION 保留 main 原正式号。完成确认默认勾选删除来源本地分支及其全部 beta 标签，可取消勾选保留；用户之后另点“发布正式版本”才更新 VERSION 和正式标签。

“删除分支”选择目标后会立即删除任意非 main 本地分支及其全部 beta 标签，不要求已合并或已正式发布。删除其他分支不要求工作区干净；如果删除当前检出的分支，工作区必须干净，软件才能安全切换到 main 后删除。其他 worktree 正在使用的分支不能删除。删除只影响本地；后续“同步远程”才传播。

旧标签仍可查看。多套 beta 基线、与 main 无共同历史或浅克隆会阻止继续发布/合并，应先检查、选准本整仓同步，或从 main 正式版本创建新分支，不要手动强制移动旧标签。

## 4. 配置 SSH 与整仓同步

“Git 账号”保存当前仓库唯一 origin SSH 地址、姓名和邮箱。提交身份不等于 SSH 身份；GitHub 网页克隆地址应转换为 SSH。支持本地绝对路径作为远程。

使用“SSH 公钥设置”生成或复制 .pub 公钥，并授权给正确账号；私钥只保存在本机。已有同名文件时生成新名称。首次 SSH 连接需在终端核对官方主机指纹，加密密钥先通过 ssh-add 解锁；不要关闭主机检查。

- “保存配置”仅写本地，不连接远程。
- “保存并验证连接”读取并执行推送 dry-run，不上传提交或标签。
- “同步本地”以 origin 为准覆盖全部本地分支和版本标签。
- “同步远程”以本地为准覆盖全部远程分支和版本标签。

**整仓覆盖包括改写共享正式标签，以及删除来源端已经不存在的分支/版本标签。** 非版本标签、其他远程和恢复引用不参与同步；来源必须有有效 main。时间或版本号不能决定哪端更新，双方分叉时由用户核对提交并选择准本。

操作前备份目标引用、配置和版本元数据。同步本地会保存暂存/未暂存修改及未跟踪文件，移动可能被覆盖的忽略文件；完成后可恢复。当前分支被来源删除时转到 main；其他 worktree 占用待更新分支时停止。

两端一致时按钮绿色禁用；有差异时显示清单；无法执行时悬停查看原因。远程使用原子推送、精确租约，并重新验证快照。服务器分支保护和权限规则仍可能拒绝正式写入。

## 5. 常见问题

### 缺少依赖或安装中断

```bash
sudo apt update
sudo apt --fix-broken install
sudo apt install ./dist/code-version-manager_$(cat VERSION)_all.deb
```

### 点击比较或完整目录对比失败

确认“设置”选定的 Beyond Compare、Meld 或 KDiff3 已安装且命令可启动。Beyond Compare 可使用下载的 Ubuntu 安装包安装，例如 `sudo apt install ./bcompare-4.*_amd64.deb`。

对比工具必须能以前台双目录模式运行。左侧 beta 只读，右侧 main 编辑副本；只有不同的普通文件可以写入，相同文件、VERSION 和特殊文件受保护。工具关闭后应用显示“完成合并”和“取消合并”。

隔离副本位于实际 Git 目录的 `code-version-manager/merge-review/`。如果提示分支引用、原工作区或对比输出在会话期间变化，先核对是否有其他 Git/文件管理程序操作，再取消或重新开始。对比失败会保留副本，取消则删除本次副本且不改工作区。

### main 同步被旧正式标签冲突阻挡

新逻辑以整个受管仓库为单位确认覆盖，允许将共享正式标签一并统一为所选来源。先查看所有分支和标签差异，再选“同步本地”或“同步远程”。不要单独删除共享正式标签来绕过问题。

### 远程失败或超时

- Permission denied (publickey)：核对公钥所属账号、授权和 SSH agent。
- Host key verification failed：在终端连接并核对官方指纹。
- 无写权限或服务器拒绝：检查仓库权限、分支保护和组织授权。
- 不支持原子推送：必须使用支持原子更新的 Git 服务，程序不会退回逐项覆盖。

读取/权限预检查 120 秒；传输默认 900 秒，可在 Git 账号高级设置改为 60–3600 秒。超时保留本地版本，可能已被远程接收；修复网络后刷新核对，不要重复发布同一版本。

### 恢复点在哪里

实际 Git 目录的 code-version-manager/recovery 保存配置、清单、CHANGELOG 和忽略文件备份；refs/code-version-manager/recovery 保存旧 Git 引用及工作区 stash。恢复暂存内容使用 stash apply --index。恢复冲突时保留恢复点，不要直接清理 Git 私有目录。

本地 manifest 不上传。远程版本没有清单时按完整 Git 快照导出；整仓同步上传完整提交历史，不只是所选封装文件或 ZIP。

### 如何验证升级

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m py_compile src/code_version_manager.py
python3 tests/verify_workspace_copy.py ~/branch_testing
```

最后一条命令只在隔离副本及临时 bare 远程中执行覆盖验证，检查来源在验证前后未变化，不操作真实 origin。

## 6. 卸载

```bash
sudo apt remove code-version-manager
```

卸载不会删除工作空间 Git 历史及文件。
