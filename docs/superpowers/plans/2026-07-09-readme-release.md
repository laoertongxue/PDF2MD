# README and Release 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 重写中英双语 README，并让 GitHub Release 可下载 macOS Apple Silicon 桌面客户端。

**架构：** README 只描述当前可交付能力，Release 用一个 GitHub Actions workflow 在 `v*` tag 上构建 Tauri DMG。发布前加一个小脚本检查 sidecar launcher，避免把本机绝对路径打进可下载客户端；Tauri 包内放入 `src/` 作为 sidecar 可引用的源码资源。

**技术栈：** Markdown、GitHub Actions、Tauri v2、npm、bash。

---

## 文件结构

- 修改：`README.md`  
  职责：项目公开首页，包含中文和英文版本、下载入口、开发/构建说明。
- 创建：`.github/workflows/release.yml`  
  职责：tag 发布时构建 macOS Apple Silicon DMG 并上传到 GitHub Release。
- 修改：`parsing-core-app/src-tauri/binaries/python3`  
  职责：桌面 App sidecar launcher。必须移除 `/Users/laoer/...` 绝对路径。
- 修改：`parsing-core-app/src-tauri/tauri.conf.json`  
  职责：把 Python 源码目录作为桌面包资源放进 App。
- 创建：`scripts/check-release-sidecar.sh`  
  职责：检查 sidecar launcher 不包含开发机绝对路径，并可执行。
- 修改：`.gitignore`  
  职责：忽略本地 release 检查产生的临时文件（如有必要）。

## 任务 1：给 sidecar launcher 加发布安全检查

**文件：**
- 创建：`scripts/check-release-sidecar.sh`
- 修改：`.gitignore`

- [ ] **步骤 1：创建失败的检查脚本**

创建 `scripts/check-release-sidecar.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

launcher="parsing-core-app/src-tauri/binaries/python3"

test -x "$launcher"

if rg -n '/Users/laoer|/Users/[^[:space:]]+/Documents/PDF2MD|/\.venv/bin/python3' "$launcher"; then
  echo "release sidecar contains a development-machine path" >&2
  exit 1
fi
```

- [ ] **步骤 2：运行检查验证失败**

运行：

```bash
chmod +x scripts/check-release-sidecar.sh
./scripts/check-release-sidecar.sh
```

预期：FAIL，输出包含 `release sidecar contains a development-machine path`。

- [ ] **步骤 3：Commit**

```bash
git add scripts/check-release-sidecar.sh
git commit -m "test: check release sidecar paths"
```

## 任务 2：移除 sidecar launcher 的本机绝对路径并引用包内源码

**文件：**
- 修改：`parsing-core-app/src-tauri/binaries/python3`
- 修改：`parsing-core-app/src-tauri/tauri.conf.json`

- [ ] **步骤 1：改成相对 App 路径优先**

替换 `parsing-core-app/src-tauri/binaries/python3` 为：

```bash
#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "$script_dir/../../.." && pwd)"
resource_src="$script_dir/../Resources/src"
repo_src="$app_root/src"

if [[ -d "$resource_src" ]]; then
  default_pythonpath="$resource_src"
else
  default_pythonpath="$repo_src"
fi

export PYTHONPATH="${PDF2MD_PYTHONPATH:-$default_pythonpath}"
python_bin="${PDF2MD_PYTHON:-python3}"

exec "$python_bin" -m parsing_core.serving.serve "$@"
```

- [ ] **步骤 2：把 Python 源码加入 Tauri 资源**

在 `parsing-core-app/src-tauri/tauri.conf.json` 的 `bundle` 对象中加入 `resources`：

```json
{
  "bundle": {
    "active": true,
    "icon": [
      "icons/icon.png"
    ],
    "externalBin": [
      "binaries/python3"
    ],
    "resources": [
      "../../src"
    ]
  }
}
```

- [ ] **步骤 3：运行发布路径检查**

运行：

```bash
./scripts/check-release-sidecar.sh
```

预期：PASS，无输出。

- [ ] **步骤 4：本机手动验证 launcher 能启动后端**

运行：

```bash
PDF2MD_PYTHON=/Users/laoer/Documents/PDF2MD/.venv/bin/python3 \
PDF2MD_PYTHONPATH=/Users/laoer/Documents/PDF2MD/src \
parsing-core-app/src-tauri/binaries/python3 --port 8000 --host 127.0.0.1 --parent-pid $$
```

另开命令验证：

```bash
curl -s http://127.0.0.1:8000/health
```

预期：输出 `{"status":"ok"}`。验证后停止后端进程。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src-tauri/binaries/python3 parsing-core-app/src-tauri/tauri.conf.json
git commit -m "fix(release): bundle source for sidecar launcher"
```

## 任务 3：重写 README 为中英双语公开首页

**文件：**
- 修改：`README.md`

- [ ] **步骤 1：替换 README 内容**

README 使用以下结构：

```markdown
# PDF2MD

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![Release](https://img.shields.io/github/v/release/laoertongxue/PDF2MD?include_prereleases)](https://github.com/laoertongxue/PDF2MD/releases)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-blue)](https://github.com/laoertongxue/PDF2MD/releases)

PDF2MD 是一个面向 MBA / 课程教材精读的桌面应用：把 PDF 等课程资料整理成 Markdown，并辅助生成结构化精读笔记、Mermaid 图和写作卡片。

中文 | [English](#english)

## 中文

### 这是什么

PDF2MD 不是普通的“PDF 转 Markdown”工具。它的目标是帮助你重读 MBA 或专业课程教材：按章节整理资料，生成稳定结构的精读笔记，并把概念解释、案例解读、应用方法和写作素材沉淀下来。

### 适合谁

- 正在系统重读 MBA、管理学、经济学、市场营销、战略等课程的人
- 想把教材精读输出为公众号长文、系列贴文或知识卡片的人
- 想用本地桌面应用管理课程资料、章节和精读结果的人

### 核心特性

- 课程工作台：按课程组织教材、章节、精读结果和卡片
- Markdown 产出：把资料整理为便于继续写作的 Markdown
- Mermaid 预览：精读笔记中的知识图和应用流程图可以直接预览
- 大模型辅助：支持围绕章节生成概念解释、案例解读、实际应用和写作卡片
- 桌面应用：Tauri 客户端自动拉起本地解析服务

### 下载桌面客户端

当前 Release 先支持 macOS Apple Silicon。

下载地址：[GitHub Releases](https://github.com/laoertongxue/PDF2MD/releases)

下载 `.dmg` 后安装 `PDF2MD.app`。如果 macOS 提示未签名应用，请在系统设置中允许打开。当前版本是 Apple Silicon 预览版；代码签名、公证和完整内置 Python runtime 不在当前版本范围内。

### 典型流程

1. 创建课程，例如“战略管理”。
2. 导入教材 PDF 或课程资料。
3. 识别并确认章节。
4. 运行章节精读。
5. 查看 Markdown 笔记、Mermaid 图和写作卡片。
6. 基于卡片输出贴文或公众号长文。

### 本地开发

```bash
git clone https://github.com/laoertongxue/PDF2MD.git
cd PDF2MD
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,serve,llm]"

cd parsing-core-app
npm install
npm run tauri dev
```

### 本地构建桌面应用

```bash
cd parsing-core-app
npm install
npm run tauri build
```

构建产物位于：

```text
parsing-core-app/src-tauri/target/release/bundle/dmg/
```

### 技术栈

- Tauri v2
- React + TypeScript + Vite
- Tailwind CSS
- Python + FastAPI
- SQLite
- Mermaid

### 路线图

- macOS Apple Silicon Release 下载
- 完整内置 Python runtime
- 更稳健的 PDF 章节识别
- Word / PPT / Excel / 图片等多格式资料工作流
- 更完整的精读模板和写作卡片模板
- macOS 签名与公证
- Windows / Linux 客户端

### License

MIT

## English

PDF2MD is a desktop app for intensive course reading. It helps turn course materials into Markdown notes, Mermaid diagrams, and reusable writing cards.

### Who It Is For

- MBA or professional-course learners
- Writers turning textbook reading into posts or long-form essays
- Users who want a local desktop workspace for course materials and chapter notes

### Features

- Course workspace for sources, chapters, notes, and cards
- Markdown-oriented output
- Mermaid diagram preview
- LLM-assisted chapter reading notes
- Tauri desktop client with a local parsing service

### Download

The first downloadable client is a macOS Apple Silicon preview.

Download from [GitHub Releases](https://github.com/laoertongxue/PDF2MD/releases).

Code signing, notarization, and a fully embedded Python runtime are outside the current release scope.

### Development

```bash
git clone https://github.com/laoertongxue/PDF2MD.git
cd PDF2MD
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,serve,llm]"

cd parsing-core-app
npm install
npm run tauri dev
```

### Build

```bash
cd parsing-core-app
npm install
npm run tauri build
```

### License

MIT
```

- [ ] **步骤 2：检查 README 不过度承诺**

运行：

```bash
rg -n "Windows|Linux|Intel|公证|notarization|signed" README.md
```

预期：只出现在路线图或明确“不在当前版本范围内”的句子里。

- [ ] **步骤 3：Commit**

```bash
git add README.md
git commit -m "docs: rewrite bilingual readme"
```

## 任务 4：新增 GitHub Release workflow

**文件：**
- 创建：`.github/workflows/release.yml`

- [ ] **步骤 1：创建 workflow**

创建 `.github/workflows/release.yml`：

```yaml
name: Release Desktop App

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write

jobs:
  macos-apple-silicon:
    name: Build macOS Apple Silicon DMG
    runs-on: macos-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Node
        uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: parsing-core-app/package-lock.json

      - name: Check release sidecar
        run: ./scripts/check-release-sidecar.sh

      - name: Install frontend dependencies
        working-directory: parsing-core-app
        run: npm ci

      - name: Build desktop app
        working-directory: parsing-core-app
        run: npm run tauri build

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: parsing-core-app/src-tauri/target/release/bundle/dmg/*.dmg
          fail_on_unmatched_files: true
```

- [ ] **步骤 2：本地 YAML 基础检查**

运行：

```bash
python - <<'PY'
from pathlib import Path
p = Path(".github/workflows/release.yml")
s = p.read_text()
assert 'tags:' in s
assert '"v*"' in s
assert 'softprops/action-gh-release@v2' in s
assert 'parsing-core-app/src-tauri/target/release/bundle/dmg/*.dmg' in s
PY
```

预期：PASS，无输出。

- [ ] **步骤 3：Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: publish macos dmg on tag"
```

## 任务 5：本地验证和最终提交状态

**文件：**
- 修改：无新增业务文件

- [ ] **步骤 1：运行 release sidecar 检查**

运行：

```bash
./scripts/check-release-sidecar.sh
```

预期：PASS，无输出。

- [ ] **步骤 2：运行前端构建**

运行：

```bash
cd parsing-core-app && npm run build
```

预期：PASS。

- [ ] **步骤 3：运行 Tauri 构建**

运行：

```bash
cd parsing-core-app && npm run tauri build
```

预期：PASS，并生成：

```text
parsing-core-app/src-tauri/target/release/bundle/dmg/PDF2MD_0.1.0_aarch64.dmg
```

- [ ] **步骤 4：检查构建产物 sidecar 不含本机路径**

运行：

```bash
rg -n '/Users/laoer|/Users/[^[:space:]]+/Documents/PDF2MD|/\.venv/bin/python3' \
  parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app/Contents/MacOS/python3
```

预期：FAIL exit code 1，且无匹配输出。

- [ ] **步骤 5：检查构建产物包含源码资源**

运行：

```bash
test -d parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app/Contents/Resources/src/parsing_core
```

预期：PASS，无输出。

- [ ] **步骤 6：检查 Git 状态**

运行：

```bash
git status --short --branch
```

预期：没有未提交文件。允许显示 `ahead`，因为当前网络到 GitHub 可能不可用。
