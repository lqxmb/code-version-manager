# Code Version Manager - 安装说明

本文档适用于 Ubuntu 20.04。软件支持完全本地、离线管理版本，不要求安装 ROS、Catkin、VS Code。远程同步为可选功能；启用时需要可访问的 Git 仓库，以及具有写入权限的 HTTPS 访问令牌、已有 Git 凭据或 SSH 密钥。

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
sudo apt install ./code-version-manager_0.1.8_all.deb
```

自动安装缺失依赖需要电脑能够访问 Ubuntu 软件源。如果电脑离线，必须先准备好缺少的依赖安装包。

不要使用下面的命令进行首次安装：

```bash
sudo dpkg -i code-version-manager_0.1.8_all.deb
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
python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; print('GTK 3 可用')"
```

正常情况下：

* Python 显示 `3.8` 或更高版本。
* Git 显示版本号。
* 最后一条命令显示 `GTK 3 可用`。

## 3. 安装软件

进入 `.deb` 文件所在目录后执行：

```bash
sudo apt install ./code-version-manager_0.1.8_all.deb
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
sudo apt install ./code-version-manager_0.1.8_all.deb
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

本地版本已经保留，不需要再次发布同一个版本。检查网络、远程仓库地址和写入权限，在“Git 账号”中重新输入访问令牌后点击“同步远程”。本地连续累积多个版本时，会补齐当前分支的全部提交和版本标签。

长期在固定电脑使用时推荐 SSH，而不是反复输入访问令牌。GitHub 不接受网站登录密码进行 Git 推送。在“Git 账号”中点击“SSH 公钥设置”，复制 `.pub` 公钥并在 GitHub 以 `Authentication Key` 类型添加；不能上传私钥。然后在终端运行 `ssh -T git@github.com`，核对官方主机指纹并完成首次连接。密钥加密时需先用 `ssh-add` 解锁。最后点击“改用 SSH（推荐）”，保存并验证，再同步即可。私钥或自定义 SSH 主机别名仍由系统 SSH 配置管理，软件不会自动生成或覆盖它们。

如果目标仓库尚未创建，点击“创建远程仓库（网页）”，在 GitHub 官方网页登录并确认创建。该入口建议私有仓库，不会自动创建或上传文件。请勿初始化 README、`.gitignore` 或许可证；创建完成后把 SSH 地址填回软件。已有的仓库不需要重复创建。

访问令牌不会持久化，软件重启后需重新输入，也可以使用已配置的系统 Git 凭据助手或 SSH 密钥。SSH 需要已确认的主机公钥和已解锁的密钥。使用 SSH 但系统未安装客户端时，可执行 `sudo apt install openssh-client`。

如果出现 `unable to read askpass response from '/bin/false'` 或 `terminal prompts disabled`，表示 HTTPS 凭据没有提供给 Git，不是本地版本损坏。修复后的 0.1.8 会在同步时弹出用户名和 PAT/访问令牌输入框；也可以提前在“Git 账号”中填写。公开仓库能读取不代表已经登录或具有推送权限。

如果出现 `Permission to ... denied to ...`、`Write access to repository not granted` 或 GitHub HTTP `403`，表示当前凭据没有获准写入。账号名称正确仍然可能缺少令牌权限。到 GitHub 的 `Settings → Developer settings → Personal access tokens → Fine-grained tokens` 检查当前令牌：`Resource owner` 必须匹配仓库所有者，`Repository access` 选择 `Only select repositories` 后勾选目标仓库，`Repository permissions → Contents` 必须是 `Read and write`，不能仅有公共仓库只读访问权限。令牌所属账号本身也必须具有仓库写入权限；组织仓库还可能要求审批或 SSO 授权。软件里的 GitHub 令牌创建链接只预填拥有者和 Contents 写权限，仍需手动勾选目标仓库。

保存令牌权限后回到软件，在“Git 账号”中替换旧令牌，再点击“验证连接与推送”进行不上传版本的 dry-run 检查，或直接点击“同步远程”。不要再次发布相同版本，也不要删除本地提交或标签；所有本地版本仍然保留。软件无法绕过 GitHub 的授权限制。dry-run 预检查通过不代表分支保护规则或服务端钩子一定允许正式同步。

如果出现“远程操作超时”，先看提示中的阶段和 Git 最后输出。旧版 0.1.8 对所有远程操作统一限制为 120 秒；更新后的 0.1.8 将推送默认时限调整为 15 分钟，可在“Git 账号”中修改为 1–60 分钟，连接验证仍为 120 秒。超时会清理本次传输子进程并保留本地版本；远程是否已经更新需要重试同步确认。仓库可以读取但上传超时时，仍需检查网络、代理或 VPN，不应仅靠不断延长时限处理。

本修复保持版本号 0.1.8；如果已经安装旧的 0.1.8 安装包，请先退出软件，重新构建后执行 `sudo apt install --reinstall ./dist/code-version-manager_0.1.8_all.deb`，再重新打开软件。

遇到分支历史分歧或标签冲突时需先手动处理，软件不会自动合并或强制覆盖远程。远程服务器不支持原子推送时也会拒绝同步，但本地版本不受影响。完整配置和同步范围请查看 [README.md](README.md)。

## 5. 卸载

```bash
sudo apt remove code-version-manager
```

卸载软件不会删除各工作空间中已经保存的 Git 历史和文件。
