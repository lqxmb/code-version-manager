# Code Version Manager 安装、升级与卸载

面向 Ubuntu 20.04 及兼容环境中的安装和运维用户。应用帮助（界面操作、发布、分支、SSH 同步和恢复）请使用软件标题栏“关于”旁的“帮助”，或打开 deb 安装的 `/usr/share/doc/code-version-manager/HELP.md`。

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

安装升级包之前先退出正在运行的版本管理器，然后执行：

```bash
sudo apt install --reinstall ./code-version-manager_<新版本>_all.deb
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

## 卸载

```bash
sudo apt remove code-version-manager
```

卸载会移除应用程序，但不会删除工作空间中的 Git 历史、文件或用户配置。
