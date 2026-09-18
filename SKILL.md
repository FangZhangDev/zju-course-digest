---
name: zju-course-digest
description: 浙江大学课程自动整理工具——抓取智云课堂最新课次的字幕/PPT 和学在浙大的作业/DDL/课件，按 data/课程名/日期/ 落盘，并生成带证据的 ai_input.md 与 class_report.md（重点：作业、考核、考勤、考试通知、考试重点）。当用户说"帮我整理今天的课/某门课最新一节课/老师布置了什么作业/这节课有什么考试通知/智云+学在浙大整理"，或提到 zju-course-digest、智云课堂、学在浙大课程整理时使用。
---

# ZJU Course Digest — 浙大课程自动整理

面向"没去线下上课，需要知道老师布置了啥、有什么考核/考勤/作业/考试通知/考试重点"的场景。
不追求课堂内容精读，重点是**要求与考核信息的证据化提取**。

接口实现全部复用 [zju-scholar](https://github.com/Lucent-Snow/zju-scholar)（统一认证/智云/学在浙大），
本 skill 只新增 `zju_collect.py` 编排层和报告生成提示词。

## 目录约定

- 脚本：`<SKILL>/scripts/`（`<SKILL>` 即本 skill 的安装目录）
- 常见安装位置：
  - WorkBuddy / CodeBuddy：`~/.workbuddy/skills/zju-course-digest`
  - Claude Code：`~/.claude/skills/zju-course-digest`
  - 其他 agent CLI：各自的 skills 目录，或软链接过去
  - 均可用 `$(dirname "$(find ~ -name zju_collect.py -path '*zju-course-digest*' 2>/dev/null | head -1)")/..` 定位
- Python：建议用专用虚拟环境（Python 3.11+，依赖见 `scripts/requirements.txt`），下文以 `$PY` 指代
- 数据默认落在 `<SKILL>/data/courses/<课程名>/<YYYY-MM-DD>/`，也可用 `--data-dir` 指到课程项目目录

## 第 0 步：登录（首次或 session 过期时）

```bash
cd <SKILL>
# $PY 指向专用虚拟环境的 python（Python 3.11+）
PY=python

# 首次：学号密码登录（自动检测校内直连/WebVPN）
$PY scripts/zju_login.py -u 学号 -p 密码

# 已保存凭证时重新登录
$PY scripts/zju_login.py

# 查看状态
$PY scripts/zju_login.py --status

# 网络诊断（连不上时先跑这个）
$PY scripts/zju_login.py --network
```

- 凭证与 session 保存在 `<SKILL>/data/credentials.json`、`data/session.json`，已在 `.gitignore`，严禁提交 Git
- Session 会过期；查询报 401/未登录时重跑 `zju_login.py` 即可
- 校外网络自动走 WebVPN，无需配置

## 环境适配说明

本 skill 在 IDE / Agent 运行时（如 WorkBuddy）中运行时，宿主可能注入
`HTTP_PROXY` / `HTTPS_PROXY` 环境变量。浙大服务在校园网内是直连的，
被强制走代理会表现为 `httpx.ConnectError`。本 skill 已自动处理：

- 所有 CLI 入口启动时调用 `ensure_direct_network()` 清理代理变量
- 各 API 客户端创建时设 `trust_env=False`，不读环境代理
- PPT 图片下载用 `ProxyHandler({})` 的 opener

因此**不需要**手动 `unset HTTP_PROXY`。如仍连不上，先跑
`zju_login.py --network` 看具体是哪一项失败。

> 若你确实需要走外部代理访问（极少见），可设 `ZJU_USE_ENV_PROXY=1` 跳过自动清理。

## 第 1 步：列课程

```bash
$PY scripts/zju_collect.py list-courses
```

输出智云(近期学习+我的课程)与学在浙大(进行中)的课程 ID/名称/教师，用于确定 `--course` 关键字。

## 第 2 步：抓取一节课

```bash
# 最新一节课（默认今天日期作目录）
$PY scripts/zju_collect.py collect --course 数据科学

# 指定日期目录 / 指定讲次
$PY scripts/zju_collect.py collect --course 数据科学 --date 2026-09-15
$PY scripts/zju_collect.py collect --course 数据科学 --sub-id 1912570

# 跳过较慢的部分
$PY scripts/zju_collect.py collect --course 数据科学 --skip-coursewares   # 不下载学在浙大课件
$PY scripts/zju_collect.py collect --course 数据科学 --no-slides          # 不要 PPT

# 课件去重强度（默认 12；课堂 PPT 翻页慢可用 8 更保守，快翻页可用 15）
$PY scripts/zju_collect.py collect --course 数据科学 --dedup-threshold 15
```

产出目录结构：

```
data/courses/<课程名>/<YYYY-MM-DD>/
  metadata.json          课程/讲次元信息（course_id, sub_id, 讲师, 起止时间, 统计）
  transcript_raw.json    原始 ASR 分段 [{start_sec, end_sec, text}]
  transcript_clean.txt   清洗后纯文本（带 [HH:MM:SS - HH:MM:SS] 时间戳）
  slides.pdf             PPT 截图去重后合成 PDF
  slide_timeline.json    每帧 PPT: created_sec / created_hms / 对应字幕窗 / pdf_page
  course_todos.json      学在浙大 todos（本课过滤）+ 作业类活动 + 下载记录
  coursewares/           学在浙大原始课件（老师上传的原始 PPT/PDF，优先保留）
```

- `slide_timeline.json` 中 `pdf_page` 是 slides.pdf 中的实际页码，字幕/PPT 证据引用用它
- 学在浙大课件下载失败不影响其他产物；失败项记录在 `course_todos.json` 的 `downloaded[].error`

## 第 3 步：生成 ai_input.md（证据汇编）

抓取完成后，把课次目录里的证据汇编成一份给 AI 用的输入文件。写入
`<课次目录>/ai_input.md`，要求：

1. **结构**：
   - `## metadata`：metadata.json 的关键字段
   - `## 字幕全文`：transcript_clean.txt 原样嵌入（带时间戳）
   - `## PPT 时间轴`：slide_timeline.json 表格化（pdf_page | created_hms | 页面 speech_text 摘要）
   - `## 学在浙大 todos / 作业`：course_todos.json 中 todos、assignments 的原始字段（含 end_time）
   - `## 课件清单`：coursewares 文件名列表（标注原始文件 vs 智云截图 PDF）
2. **保留证据**：时间戳、PPT 页码、学在浙大 DDL 原文一律保留，不概括
3. 若 coursewares 中有原始 PPT/PDF，提取其中文本（pdfplumber/pdftotext 可用则提取）附在 PPT 时间轴之后，作为字幕纠错的上下文
4. 字数上限：不做删减，全文嵌入；该文件是后续报告的唯一事实来源

生成方式：直接由 Claude 读取上述 JSON/txt 拼装写入 ai_input.md（无需脚本）。

## 第 4 步：生成 class_report.md（要求与考核提取）

基于 ai_input.md 写报告，**重点不是课堂内容摘要**：

1. 本节课明确布置的作业：每项的截止时间、提交位置/方式
2. Quiz / 小测 / 考试信息（时间、范围、形式）
3. 考勤要求、课堂要求、考试安排
4. 老师强调"会考/重点/必须掌握"的内容
5. 老师提到但不确定是否强制的事项

输出格式：

- 每条结论分三类标注：**【明确要求】**（老师/平台原文明确）、**【建议事项】**（老师建议但非强制）、**【AI 推断】**（AI 推测，禁止写成老师要求）
- 每条结论必须带证据引用：`[字幕 01:23:18]`、`[PPT 第 31 页]`、`[学在浙大 DDL]`（用 slide_timeline 的 pdf_page 和 transcript_clean 的时间戳、course_todos 的 end_time）
- ASR 疑似识别错误时：结合 PPT 上下文给出修正，同时保留原文（如 "XX（ASR 原文：YY）"）
- 无证据支撑的猜测一律不放；某类信息没有就写"本节课未提及"

报告写入 `<课次目录>/class_report.md`。

## 完整对话 → 操作映射

- "帮我整理今天的 XX 课" → list-courses 确认关键字 → collect → 生成 ai_input.md → 生成 class_report.md
- "老师这节课布置了什么" → collect → class_report.md 第 1 节
- "这学期有什么 DDL" → `$PY scripts/zju_courses.py todos`（全局 todos，不必抓课次）
- "把这门课的课件都给我" → collect 后看 `coursewares/` 目录

## 分层职责

- `zju_login.py`：统一认证 + 三个服务登录（复用 zju-scholar）
- `zju_collect.py`：本 skill 核心编排（智云字幕/PPT + 学在浙大 todos/课件 → 课次目录）
- `zju_courses.py`：学在浙大单功能查询（todos/coursewares/resources 等，复用 zju-scholar）
- `zju_zhiyun.py`：智云单功能查询（lecture/ppt/transcript 等，复用 zju-scholar）

## 安全

- 密码只进 `data/credentials.json`（gitignore），不进环境变量、不进 Git、不进报告
- session/JWT 同上；`.gitignore` 已包含 `data/`、`cache/`、`output/`
- 只处理当前账号有权访问的课程，不做权限绕过、不做批量爬取
- CC98 无关功能本 skill 不涉及；如需论坛信息用 zju-scholar 原版

## 依赖

Python 3.11+：
httpx、pycryptodome、pillow、imagehash。见 `scripts/requirements.txt`。
PPT 合成 PDF 与去重为纯 Python 实现（imagehash 可选，缺失时跳过去重）。
