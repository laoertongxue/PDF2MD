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
