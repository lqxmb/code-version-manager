# Code Version Manager 安装、升级与卸载

面向 Ubuntu 20.04 及兼容环境中的安装和运维用户。应用帮助（界面操作、发布、分支、SSH 同步和恢复）请使用软件标题栏“帮助”菜单中的“使用帮助”，或打开 deb 安装的 `/usr/share/doc/code-version-manager/HELP.md`。

## 系统要求

软件包依赖由 APT 根据 deb 元数据声明并自动安装，包括 Python 3.8 以上、GTK 3、Git 和 OpenSSH 客户端。目录比较与手动合并需要额外安装 Beyond Compare、Meld 或 KDiff3；默认使用 Beyond Compare。

确认当前系统的基础工具：

```bash
python3 --version
git --version
ssh -V
```

## 安装

取得发行版提供的 `code-version-manager_$(cat VERSION)_all.deb` 后，在其所在目录运行：

```bash
sudo apt update
sudo apt install ./dist/code-version-manager_$(cat VERSION)_all.deb
```

APT 会处理软件包声明的依赖。需要访问 Ubuntu 软件源以获取尚未安装的依赖；离线安装时请预先准备这些依赖。安装后从应用菜单打开 Code Version Manager，或运行：

```bash
code-version-manager
```

安装后的用户文档位于 `/usr/share/doc/code-version-manager/`，其中 `HELP.md` 是应用操作帮助，`INSTALL.md` 是本安装指南。

## 升级或重装

从 0.4.9 开始，软件支持从发布者在构建时确定的公开 GitHub 仓库检查并下载升级包，默认仓库为 [lqxmb/code-version-manager](https://github.com/lqxmb/code-version-manager/releases)。安装包携带更新配置，安装并重启软件后生效。已有旧版本须先手动安装一次 0.4.9 或更新版本。联网升级的界面操作见 [HELP.md](HELP.md#软件更新)。升级包和同名 `.sha256` 文件来自同一次正式 Release；只有校验通过的包才能在软件内安装。管理员授权由系统 PolicyKit 提供，依赖 `policykit-1`；APT 负责安装及依赖处理。

无法访问 GitHub 或离线时，可在其他联网电脑下载 deb 后拷贝过来，仍按下述方式安装。

安装升级包之前先退出正在运行的版本管理器，然后执行：

```bash
sudo apt install --reinstall ./dist/code-version-manager_$(cat VERSION)_all.deb
```

升级不会删除受管理工作空间的 Git 历史和文件。若远程连接、分支或版本操作遇到问题，先点击应用“刷新”核对当前状态，再查看“帮助”和应用日志；不要因网络超时重复发布同一版本。

## 安装故障排查

### 找不到 deb 文件

确认终端当前目录正确，或在命令中使用 deb 的完整路径。APT 安装本地包时，文件名前需要 `./` 或完整路径。

### 依赖缺失或安装中断

确认网络和 Ubuntu 软件源可用，然后执行：

```bash
sudo apt update
sudo apt --fix-broken install
```

再重新安装本地 deb。不要用 `dpkg -i` 代替 APT 处理依赖。

### 应用无法启动

从终端运行 `code-version-manager` 并查看错误信息；确认软件包安装成功以及系统 GTK 3、Python GI 和 Git 依赖可用。完整应用操作与远程 SSH 排障见 `HELP.md`。

### 联网更新失败

“暂无已发布升级包”表示项目还没有正式 Release；只有代码或 Git 标签不能提供升级包。连接超时或 GitHub API 限流时可稍后手动检查；不会影响本地版本管理。缺少附件或校验失败时请重新下载完整发行包，不要安装未完成文件。

提示“软件更新配置无效”时，按上述重装步骤恢复安装包中的配置文件，并重启软件后重试；本地版本管理仍可使用。

取消管理员授权会保留现有安装。APT 锁占用时先等待系统其他安装任务结束再重试；依赖或安装中断按上面的修复步骤处理。安装错误窗口包含安装日志位置，下载包和日志位于用户缓存目录 `code-version-manager/updates/`（通常在 `~/.cache/` 下）。

## 可选：安装 GitHub Release 发布工具

使用“发布软件到 GitHub”向任意 GitHub 发行仓库上传 deb 时，需要 GitHub CLI（`gh`）和可用的桌面终端。按 [GitHub CLI 官方 Linux 安装说明](https://github.com/cli/cli/blob/trunk/docs/install_linux.md) 安装；系统软件源提供 `gh` 时也可通过 APT 安装。该工具列为建议依赖，不影响普通用户检查、下载和安装本软件更新。发布目标、多架构包及私有源码搭配公开发行仓库的操作见 [HELP.md](HELP.md#发布软件升级包到-github)。

发布窗口会检查 CLI 是否支持浏览器登录及 API 上传；旧版本不支持时请先升级 CLI。首次登录由 CLI 打开浏览器授权，凭据的存储方式由 CLI 和系统凭据服务决定。登录账号须有目标仓库写权限；空仓库初始化和缺失标签创建同样使用 CLI 的 HTTPS 授权。Git 的 SSH 授权用于单独的代码推送，不替代 Release 发布授权。

## 卸载

```bash
sudo apt remove code-version-manager
```

卸载会移除应用程序，但不会删除工作空间中的 Git 历史、文件或用户配置。
