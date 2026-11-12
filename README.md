# Code Version Manager 开发与维护

Python 3、GTK 3 桌面 Git 管理器，用于在任意独立工作空间中离线发布版本、固定基线开发分支、手动目录合并、比较和 ZIP 导出，并可选择通过 SSH 同步当前分支或执行高级整仓覆盖。应用逻辑集中在 `src/code_version_manager.py`，不依赖额外服务或 Python 包管理框架。

## 文档职责

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| [`HELP.md`](HELP.md) | 软件使用者 | 应用界面、发布、分支、同步、SSH、恢复与常见操作问题 |
| [`INSTALL.md`](INSTALL.md) | 安装、升级和卸载软件的用户 | 系统要求、预构建 deb 安装与升级、启动和安装故障排查 |
| [`AGENTS.md`](AGENTS.md) | 项目维护者和编程代理 | 当前业务规则、源码入口、数据边界、测试和实现约束 |

已安装软件可从标题栏“帮助”菜单打开内置帮助窗口、手动检查更新或查看关于信息；F1 打开帮助，新版本提示显示在主窗口底部。应用从 `/usr/share/doc/code-version-manager/HELP.md` 读取帮助，源码运行时回退到项目根目录。deb 将三份用户文档安装到 `/usr/share/doc/code-version-manager/`。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `src/code_version_manager.py` | 标签解析、数据类型、GitRepository 后端、GTK 界面和应用入口 |
| `tests/` | 后端、同步流程、生成文件、合并、删除、恢复、备注与 GTK 回归测试 |
| `tests/verify_workspace_copy.py` | 仅在隔离 Git 副本和临时远程中验证真实工作空间流程 |
| `data/` | 启动器、图标、默认忽略模板和 Beyond Compare 配置 |
| `debian/`、`build_deb.sh` | Debian 元数据、安装维护脚本和 deb 打包 |
| `build_config.json` | 构建配置，统一指定本软件的更新仓库 |
| `VERSION` | Debian 软件包版本，不是被管理工作空间的版本标签 |

仓库业务细节和存储边界以 [`AGENTS.md`](AGENTS.md) 为准。用户操作说明集中在 `HELP.md`，安装维护说明集中在 `INSTALL.md`；更新行为时同时检查对应文档，避免把开发步骤写入用户帮助。

## 开发环境

运行和测试依赖来自系统 APT：Python 3.8 以上、python3-gi、gir1.2-gtk-3.0、Git 和 openssh-client。Beyond Compare、Meld、KDiff3 是可选外部程序。不使用 pip 或虚拟环境作为应用运行要求。

从源码启动：

```bash
python3 src/code_version_manager.py
```

## 测试与隔离验证

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m py_compile src/code_version_manager.py
git diff --check
```

`tests/verify_workspace_copy.py` 会复制来源仓库和应用元数据，在副本及临时 bare origin 中验证覆盖、撤回和恢复流程，然后核对来源未变。使用前确认副本大小和 Git 元数据；脚本禁止对来源目录或真实 origin 执行覆盖。

```bash
python3 tests/verify_workspace_copy.py /path/to/workspace
```

没有显示服务时，依赖真实 GTK 控件的用例会跳过；运行 UI 回归时需要可用显示服务或已安装的 Xvfb。测试使用 `importlib` 独立加载源码模块；配置测试应将设置路径重定向到临时目录。

`tests/test_general_robustness.py` 覆盖配置原子保存、Git 忽略反选、历史归档与 ZIP 失败保护、附件 API 编码、后台回调失效、引用完成后的刷新失败及 linked worktree 存储统计。Release 测试只模拟 gh 与 HTTP 响应，同步测试只访问临时 bare 仓库；完整验证应记录实际执行和跳过的 GTK 用例。历史物化使用磁盘归档逐项读取，软件 Release 上传使用 gh 授权的二进制附件 API，不增加 Python 依赖。

## 构建 Debian 软件包

项目发行号取自根目录 `VERSION`。构建前可调整 `data/workspace.gitignore`，它作为新建工作空间的忽略模板。脚本把程序安装至 `/usr/lib/code-version-manager/`，将 `README.md`、`INSTALL.md` 和 `HELP.md` 安装至 `/usr/share/doc/code-version-manager/`，并在 `dist/` 生成 `Architecture: all` 软件包。

本软件更新仓库的唯一配置来源是项目根目录 [`build_config.json`](build_config.json) 的 `update_repository` 字段，默认内容为：

```json
{
  "update_repository": "lqxmb/code-version-manager"
}
```

发布前将该字段改为用于分发升级包的公开 GitHub 仓库 `owner/repo`，再运行下面的构建命令。脚本先检查 JSON 对象及仓库格式，配置缺失或无效时终止；每次构建将文件以 `0644` 权限复制到包内 `/usr/lib/code-version-manager/build_config.json`。安装并重启后，应用从程序旁读取这一副本；源码运行从项目根目录读取，均不依赖启动时的工作目录。Python 源码不另设默认更新仓库，配置异常只使更新检查报错，不影响本地版本管理。

```bash
sh build_deb.sh
```

脚本同时生成 `dist/code-version-manager_<VERSION>_all.deb.sha256`。从“高级操作 → 发布软件到 GitHub”将本软件 deb 及校验文件发布到构建配置指定的更新仓库，确认发行信息后执行。若希望发行标签关联实际源码提交，应先推送 `v<VERSION>` 标签；缺少标签时，发布器在已确认的目标默认分支提交上创建标签。发布器也支持其他软件、独立公开或私有发行仓库及同版多架构 deb，确认正式发布成功后，将目标写入工作空间本地 Git 配置 `codeversionmanager.releaseRepositoryHistory`，供下次回填和下拉选择；无成功历史时兼容旧 `codeversionmanager.releaseRepository`。这一发布目标不会改变本软件的构建配置。空发行仓库在确认后仅初始化 README，不读取或上传源码工作空间。应用使用 GitHub CLI 登录状态，固定本地附件副本；新标签使用草稿，已有同标签 Release 按帮助中的整版替换流程更新。操作细节见 HELP；CLI 安装见 INSTALL。仍可到 GitHub 网页手动创建正式 Release 并上传附件；不要勾选预发布。客户端按 Debian 版本规则比较最新正式发行版，只推送代码或标签不会提供升级包。根目录 `VERSION` 保持本地忽略；首次提供联网更新的发行号为 0.4.9。

发布窗口打开时仅查找本机的 `gh` 和桌面终端，缺少时直接显示可复制的官方安装说明地址；CLI 能力和授权仍在操作时检查。依赖提示和终端启动共用终端检测逻辑，缺少终端不阻止已有授权的登录检查及发布。

发行仓库创建与发布预检查分开：创建预检查固定账号、仓库和可见性，确认后复核并通过 `gh repo create` 创建空仓库；公开／私有默认选公开。创建能力只在创建操作时检查，子进程沿用取消和超时管理。创建请求结束后独立查询目标，无法核实时报告结果待确认，不自动重试；已确认的目标保存到窗口绑定工作空间的本地配置。README、标签和 Release 继续由后续发布确认流程处理。

软件更新后端与 GitRepository 独立，使用 Python 标准库 HTTPS 和系统 dpkg/APT/PolicyKit；不引入 Python 包或额外常驻服务。更新回归使用模拟网络、临时缓存和模拟安装进程，禁止在测试中实际请求管理员授权或安装系统软件。

Release 发布保留所选 deb 的原始文件名，校验附件为同名 `.sha256`，从包内读取的包名、版本和架构只用于校验与展示；发布器不按某个软件的附件模板改名。新标签发布先建草稿；同标签已有正式版、预发布版或草稿时，确认后整版删除旧附件并上传本次选择的附件，最终发布为正式版。旧版本替换保留当前 latest；新版本仍须高于最新正式版。替换中断会报告最后确认的附件和 Release 链接，不自动回滚 GitHub 上已完成的操作。发布回归使用临时 deb、模拟 gh 和模拟匿名 HTTPS，不访问真实发布接口。覆盖原名与 UTF-8 校验内容、同名及额外旧附件替换、多架构附件、目标配置隔离、空仓库初始化、标签创建、私有目标授权核实、草稿续传、确认后状态变化、删除和上传中断、取消与发布超时核实；不访问真实发布接口。本软件自身更新源和安装包校验独立于发布目标，其他软件的升级功能由各软件接入。

构建前运行目标测试和语法检查；修改 SSH、同步、恢复、合并或删除流程时优先运行相应专项测试。包的依赖、安装及卸载指引见 [`INSTALL.md`](INSTALL.md)。不要将真实工作空间或 origin 用于覆盖验证。
