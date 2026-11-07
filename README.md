# Code Version Manager 开发与维护

Python 3、GTK 3 桌面 Git 管理器，用于在任意独立工作空间中离线发布版本、固定基线开发分支、手动目录合并、比较和 ZIP 导出，并可选择通过 SSH 同步当前分支或执行高级整仓覆盖。应用逻辑集中在 `src/code_version_manager.py`，不依赖额外服务或 Python 包管理框架。

## 文档职责

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| [`HELP.md`](HELP.md) | 软件使用者 | 应用界面、发布、分支、同步、SSH、恢复与常见操作问题 |
| [`INSTALL.md`](INSTALL.md) | 安装、升级和卸载软件的用户 | 系统要求、预构建 deb 安装与升级、启动和安装故障排查 |
| [`AGENTS.md`](AGENTS.md) | 项目维护者和编程代理 | 当前业务规则、源码入口、数据边界、测试和实现约束 |

已安装软件可从标题栏“关于”旁的“帮助”打开 `HELP.md`。deb 将三份用户文档安装到 `/usr/share/doc/code-version-manager/`。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `src/code_version_manager.py` | 标签解析、数据类型、GitRepository 后端、GTK 界面和应用入口 |
| `tests/` | 后端、同步流程、生成文件、合并、删除、恢复、备注与 GTK 回归测试 |
| `tests/verify_workspace_copy.py` | 仅在隔离 Git 副本和临时远程中验证真实工作空间流程 |
| `data/` | 启动器、图标、默认忽略模板和 Beyond Compare 配置 |
| `debian/`、`build_deb.sh` | Debian 元数据、安装维护脚本和 deb 打包 |
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

## 构建 Debian 软件包

项目发行号取自根目录 `VERSION`。构建前可调整 `data/workspace.gitignore`，它作为新建工作空间的忽略模板。脚本把程序安装至 `/usr/lib/code-version-manager/`，将 `README.md`、`INSTALL.md` 和 `HELP.md` 安装至 `/usr/share/doc/code-version-manager/`，并在 `dist/` 生成 `Architecture: all` 软件包。

```bash
sh build_deb.sh
```

构建前运行目标测试和语法检查；修改 SSH、同步、恢复、合并或删除流程时优先运行相应专项测试。包的依赖、安装及卸载指引见 [`INSTALL.md`](INSTALL.md)。不要将真实工作空间或 origin 用于覆盖验证。
