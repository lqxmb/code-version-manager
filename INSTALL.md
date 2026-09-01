# Code Version Manager - 安装说明

本文档适用于 Ubuntu 20.04。软件完全在本地管理版本，不要求安装 ROS、Catkin、VS Code，也不要求注册或连接 GitHub、Gitee 等网络代码托管平台。

## 1. 安装前需要的软件

### 必须安装

| 软件包 | 最低要求 | 用途 |
| --- | --- | --- |
| `python3` | 3.8 | 运行软件 |
| `python3-gi` | Ubuntu 20.04 软件源版本 | Python 的 GTK 接口 |
| `gir1.2-gtk-3.0` | Ubuntu 20.04 软件源版本 | GTK 3 图形界面 |
| `git` | Ubuntu 20.04 软件源版本 | 保存、切换和导出版本 |

这些依赖已经写入 `.deb` 安装包的 `Depends` 字段。使用 APT 安装本软件时，系统会自动执行以下流程：

1. 检查 Python 版本以及 `python3-gi`、GTK 3、Git 是否已经安装。
2. 已经满足要求的软件包不会重复安装。
3. 缺少的软件包会从 Ubuntu 软件源下载并安装。
4. 所有依赖满足后再安装“工作空间版本管理”。

不需要提前逐个安装依赖，直接执行：

```bash
sudo apt update
sudo apt install ./code-version-manager_0.1.3_all.deb
```

自动安装缺失依赖需要电脑能够访问 Ubuntu 软件源。如果电脑离线，必须先准备好缺少的依赖安装包。

不要使用下面的命令进行首次安装：

```bash
sudo dpkg -i code-version-manager_0.1.3_all.deb
```

`dpkg -i` 只检查依赖，不会联网下载缺少的软件包；`apt install` 才会自动补齐依赖。

### 可选安装

`Beyond Compare 4` 用于软件中的完整目录对比功能。只进行版本发布、查看、切换或下载时，可以不安装；需要点击“对比”时必须安装。

安装从 Beyond Compare 官方网站下载的 Ubuntu `.deb` 文件，例如：

```bash
sudo apt install ./bcompare-4.*_amd64.deb
```

文件名可能随版本变化，请以实际下载的文件名为准。安装完成后可执行以下命令检查：

```bash
bcompare --version
```

## 2. 检查安装环境

执行以下命令：

```bash
python3 --version
git --version
python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; print('GTK 3 可用')"
```

正常情况下：

* Python 显示 `3.8` 或更高版本。
* Git 显示版本号。
* 最后一条命令显示 `GTK 3 可用`。

## 3. 安装软件

进入 `.deb` 文件所在目录后执行：

```bash
sudo apt install ./code-version-manager_0.1.3_all.deb
```

使用 `apt install` 可以自动检查并安装系统依赖。通过 Ubuntu 图形软件安装器打开 `.deb` 时也会检查依赖，但终端命令能够清楚显示下载和安装过程。

安装完成后，在 Ubuntu 应用菜单中搜索并打开“Code Version Manager”。也可以在终端执行：

```bash
code-version-manager
```

## 4. 常见安装问题

### 提示缺少依赖

确认电脑可以访问 Ubuntu 软件源，然后执行：

```bash
sudo apt update
sudo apt --fix-broken install
sudo apt install ./code-version-manager_0.1.3_all.deb
```

### 点击对比没有反应

先确认 Beyond Compare 已安装，并且以下命令能够启动它：

```bash
bcompare
```

Beyond Compare 不是 Ubuntu 默认自带软件，需要单独安装。

### 无法发布版本

确认当前用户对所选工作空间具有读写权限。软件发布版本时会在该工作空间中创建本地 Git 版本记录，不需要互联网连接。

## 5. 卸载

```bash
sudo apt remove code-version-manager
```

卸载软件不会删除各工作空间中已经保存的 Git 历史和文件。
