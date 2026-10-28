# Code Version Manager - 安装说明

本文档适用于 Ubuntu 20.04。软件支持完全本地、离线管理版本，不要求安装 ROS、Catkin、VS Code。远程同步为可选功能，统一使用 SSH；启用时需要可访问的 Git 仓库、已授权的公钥和本机可用的 SSH 密钥。

## 1. 安装前需要的软件

### 必须安装

| 软件包 | 最低要求 | 用途 |
| --- | --- | --- |
| `python3` | 3.8 | 运行软件 |
| `python3-gi` | Ubuntu 20.04 软件源版本 | Python 的 GTK 接口 |
| `gir1.2-gtk-3.0` | Ubuntu 20.04 软件源版本 | GTK 3 图形界面 |
| `git` | Ubuntu 20.04 软件源版本 | 保存、切换和导出版本 |
| `openssh-client` | Ubuntu 20.04 软件源版本 | SSH 远程同步 |

这些依赖已经写入 `.deb` 安装包的 `Depends` 字段。使用 APT 安装本软件时，系统会自动执行以下流程：

1. 检查 Python 版本以及 `python3-gi`、GTK 3、Git 和 OpenSSH 客户端是否已经安装。
2. 已经满足要求的软件包不会重复安装。
3. 缺少的软件包会从 Ubuntu 软件源下载并安装。
4. 所有依赖满足后再安装“工作空间版本管理”。

不需要提前逐个安装依赖，直接执行：

```bash
sudo apt update
sudo apt install ./code-version-manager_0.1.9_all.deb
```

自动安装缺失依赖需要电脑能够访问 Ubuntu 软件源。如果电脑离线，必须先准备好缺少的依赖安装包。

不要使用下面的命令进行首次安装：

```bash
sudo dpkg -i code-version-manager_0.1.9_all.deb
```

`dpkg -i` 只检查依赖，不会联网下载缺少的软件包；`apt install` 才会自动补齐依赖。

### 可选安装

`Beyond Compare 4` 用于软件中的完整目录对比功能。它是可选依赖：只进行版本发布、查看、切换或下载时，可以不安装；需要点击“对比”时必须安装。软件自带 `BCSessions.xml` 默认会话配置，只有检测到 `bcompare` 命令后才会加载该配置并启动 Beyond Compare。

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
ssh -V
python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; print('GTK 3 可用')"
```

正常情况下：

* Python 显示 `3.8` 或更高版本。
* Git 和 OpenSSH 显示版本号。
* 最后一条命令显示 `GTK 3 可用`。

## 3. 安装软件

进入 `.deb` 文件所在目录后执行：

```bash
sudo apt install ./code-version-manager_0.1.9_all.deb
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
sudo apt install ./code-version-manager_0.1.9_all.deb
```

### 点击对比没有反应

先确认 Beyond Compare 已安装，并且以下命令能够启动它：

```bash
bcompare
```

Beyond Compare 不是 Ubuntu 默认自带软件，需要单独安装。

### 无法发布版本

确认当前用户对所选工作空间具有读写权限。软件发布版本时会在该工作空间中创建本地 Git 版本记录，不需要互联网连接。

### 本地发布成功，但远程同步失败

本地版本已经保留，不需要再次发布同一个版本。检查网络、SSH 仓库地址、公钥授权和仓库写权限，再点击“同步远程”。本地累积多个版本时，会一次补齐当前分支的全部提交和可达版本标签。

“Git 账号”优先填入当前工作空间的 `origin` 地址，GitHub HTTPS 地址会转换为 SSH，例如 `git@github.com:lqxmb/code-version-manager.git`。没有远程配置时会按工作空间名提示地址示例，需填写实际账号或组织及仓库名。点击“SSH 公钥设置”，复制 `.pub` 公钥并在 GitHub 中以 `Authentication Key` 类型添加。没有公钥时，可在窗口内填写邮箱和可选口令，点击“生成新的 SSH 密钥”。软件调用 `ssh-keygen` 在本机生成密钥，已有同名文件时使用新文件名，界面只显示公钥。首次连接请在终端运行 `ssh -T git@github.com` 并核对官方主机指纹。加密或使用新文件名的密钥需按窗口提示用 `ssh-add` 加载；自定义 SSH 主机别名由系统配置管理。

- 出现 `Permission denied (publickey)`：检查所选公钥是否已添加到正确的账号，以及 SSH agent 是否已解锁对应私钥。
- 出现 `Host key verification failed`：在终端连接并核对官方主机指纹后确认，不要禁用主机密钥检查。
- 出现仓库写权限拒绝：检查密钥所属账号的仓库写权限、分支保护规则和组织授权要求。
- 如果仍配置为网页克隆地址，请从仓库页面复制 SSH 地址，保存到“Git 账号”后重新验证。

目标仓库尚未创建时，点击“创建远程仓库（网页）”，在 GitHub 官方网页登录并确认创建。创建时不要初始化 README、`.gitignore` 或许可证，完成后把 SSH 地址填回软件；已有仓库不需要重复创建。

“保存配置”只保存当前工作空间的设置，不连接远程。“保存并验证连接”先保存配置，再执行读取连接检查和不上传版本的推送 dry-run，成功后弹窗显示“连接及推送预检查通过”；没有本地提交或正在查看历史提交时显示“连接成功，推送尚未验证”，失败则弹窗说明原因。实际上传需点击主窗口“同步远程”或启用发布后自动同步。预检查通过后，正式同步仍需满足分支规则和服务端检查。遇到历史分歧或标签冲突需先手动处理；软件不会自动合并或强制覆盖远程。不支持原子推送的服务器会拒绝同步，本地版本不受影响。

远程连接和预检查的超时为 120 秒；实际推送默认允许 15 分钟，可在“Git 账号 → 高级设置：推送超时”中修改为 1–60 分钟。此设置仍用于限制实际上传的等待时间，一般使用默认值即可。超时会清理本次传输子进程并保留本地版本；远程可能已经更新，恢复网络后重试同步即可检查并补齐。读取成功不代表上传链路正常，不要只靠反复延长时限处理网络问题。

升级到 0.1.9 时先退出软件，在项目目录执行 `sudo apt install ./dist/code-version-manager_0.1.9_all.deb`，再重新打开。完整配置和同步范围请查看 [README.md](README.md)。

## 5. 卸载

```bash
sudo apt remove code-version-manager
```

卸载软件不会删除各工作空间中已经保存的 Git 历史和文件。
