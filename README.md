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

推荐下载 `PDF2MD_<版本>_aarch64.dmg`，打开后把 `PDF2MD.app` 拖入“应用程序”。
若 DMG 无法使用，可下载 `PDF2MD_<版本>_aarch64.app.zip`，解压后把应用移入“应用程序”。
两种资产内容相同，均为 Apple Silicon 版本并内置完整 Python runtime。

当前公开包使用 ad-hoc 签名，尚未配置 Developer ID 签名与 Apple 公证。首次启动若被
Gatekeeper 拦截，请在 Finder 中按住 Control 点按 `PDF2MD.app`，选择“打开”并再次确认；
若系统仍阻止启动，请前往“系统设置 > 隐私与安全性”，在对应提示旁选择“仍要打开”。
只对从本仓库 Releases 下载且校验值匹配的应用执行此操作。

每个 CI Release 同时提供 DMG、ZIP 及各自的 `.sha256`。Actions artifact 名称包含 workflow
run ID，发布资产由同一次 run 的 artifact 下载后创建，并带有 GitHub artifact attestation。
CI 在公开仓库的标准 `macos-14` M1 runner 上原生构建，并明确断言 `arm64` 架构。发布门禁
验证 arm64 Mach-O、包结构、版本和 ad-hoc 签名，还会在受限 `PATH=/usr/bin:/bin` 的干净
环境中冷启动打包后的 sidecar、检查 `/health`，并在运行前后分别验证签名。手工 Release
记录单独描述 Finder 安装、Gatekeeper 和用户环境验收，不与 CI 结果混写。

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

### Task 12 验收

多教材主题融合流程已完成 1440x900 与 1024x768 双视口验收，覆盖多教材导入队列、
同名章节区分、主题映射、融合来源跳转、卡片筛选、后端错误恢复和同页双 Mermaid 预览。
截图与机器可读结果见 [Task 12 验收证据](docs/acceptance/task-12/README.md)。

CI 门禁摘要：版本一致性与真实网络 E2E 已加入发布门禁；Mermaid 11 预览已通过真实解析、
安全渲染、错误节点清理和响应式无溢出验证。此处是 CI 结果，不等同于手工 Apple Silicon
实机安装验收。

### 路线图

- macOS Apple Silicon Release 下载
- Developer ID 签名与 Apple 公证
- 更稳健的 PDF 章节识别
- Word / PPT / Excel / 图片等多格式资料工作流
- 更完整的精读模板和写作卡片模板
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

Prefer `PDF2MD_<version>_aarch64.dmg`, then drag `PDF2MD.app` into Applications.
If the DMG is unavailable, use `PDF2MD_<version>_aarch64.app.zip`, unzip it, and move
the app into Applications. Both assets contain the same Apple Silicon app with a fully
embedded Python runtime.

The public build is currently ad-hoc signed; Developer ID signing and Apple notarization
are not configured. If Gatekeeper blocks the first launch, Control-click `PDF2MD.app` in
Finder, choose **Open**, and confirm. If macOS still blocks it, go to **System Settings >
Privacy & Security** and choose **Open Anyway** for the matching prompt. Do this only for
an app downloaded from this repository's Releases whose checksum matches.

Each CI release includes the DMG, ZIP, and a `.sha256` file for each. The Actions artifact
name contains the workflow run ID; release assets are downloaded from that same run before
the GitHub Release is created, and GitHub artifact attestations record their provenance.
CI builds natively on the standard M1 `macos-14` runner for public repositories and explicitly
requires `arm64`. The release gate verifies the arm64 Mach-O files, bundle structure, version,
checksums, and ad-hoc signature. It also cold-starts the packaged sidecar in a clean environment
with `PATH=/usr/bin:/bin`, checks `/health`, and verifies the signature both before and after the
run. Manual release notes separately report Finder installation, Gatekeeper, and user-environment
acceptance instead of presenting those checks as CI results.

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
