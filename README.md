# ZJU Course Digest

浙江大学课程自动整理工具（AI Agent Skill）——抓取**智云课堂**最新课次的字幕 / PPT 与**学在浙大**的作业 / DDL / 课件，按 `data/<课程名>/<YYYY-MM-DD>/` 落盘，并生成带证据引用的 `ai_input.md` 与 `class_report.md`。

面向"没去线下上课，需要知道老师布置了啥、有什么考核 / 考勤 / 考试通知 / 考试重点"的场景。不追求课堂内容精读，重点是**要求与考核信息的证据化提取**——每条结论都标注来源（字幕时间戳 / PPT 页码 / DDL 原文）。

## 功能

- **登录**：浙大统一认证（RSA + CAS），自动检测校内直连 / WebVPN，校外无需配置
- **抓取课次**：智云 ASR 字幕 + PPT 截图（去重后合成 PDF + 时间轴）+ 学在浙大 todos / 原始课件
- **证据汇编**：`ai_input.md` 全文嵌入字幕、PPT 时间轴、DDL 原始字段，作为后续分析的唯一事实来源
- **报告生成**：`class_report.md` 聚焦作业 DDL、Quiz / 考试安排、考勤要求、"会考重点"，结论分**【明确要求】/【建议事项】/【AI 推断】**三类标注

## 快速开始

```bash
git clone https://github.com/FangZhangDev/zju-course-digest.git
cd zju-course-digest
pip install -r scripts/requirements.txt

# $PY 指向你的 Python 3.11+ 解释器（建议 conda 独立环境）
PY=python

# 首次：学号密码登录（自动检测校内直连 / WebVPN）
$PY scripts/zju_login.py -u 学号 -p 密码

# 列出课程
$PY scripts/zju_collect.py list-courses

# 抓取某门课最新一节课
$PY scripts/zju_collect.py collect --course 数据科学
```

详细的抓取参数、产出目录结构与报告生成流程见 [SKILL.md](SKILL.md)。

## 产出目录结构

```
data/courses/<课程名>/<YYYY-MM-DD>/
  metadata.json          课程/讲次元信息（course_id, sub_id, 讲师, 起止时间, 统计）
  transcript_raw.json    原始 ASR 分段 [{start_sec, end_sec, text}]
  transcript_clean.txt   清洗后纯文本（带 [HH:MM:SS - HH:MM:SS] 时间戳）
  slides.pdf             PPT 截图去重后合成 PDF
  slide_timeline.json    每帧 PPT: created_sec / created_hms / 对应字幕窗 / pdf_page
  course_todos.json      学在浙大 todos（本课过滤）+ 作业类活动 + 下载记录
  coursewares/           学在浙大原始课件（老师上传的原始 PPT/PDF）
  ai_input.md            证据汇编（给 AI 的唯一事实来源）
  class_report.md        要求与考核提取报告
```

## 用作 AI Agent Skill

遵循 `SKILL.md`（YAML frontmatter + 说明文档）这一通用 skill 约定，可被多种 AI Agent CLI 直接使用：

- **Claude Code**：放到 `~/.claude/skills/zju-course-digest/`
- **Codex / Qwen Code / 其他 agent CLI**：放到各自的 skills 目录，或软链接过去
- 也可以不作为 skill 安装，直接把 `SKILL.md` 作为提示词交给任意 agent 按步骤执行

## 安全

- 学号密码只进本地 `data/credentials.json`，session/JWT 同理；`data/`、`cache/` 已在 `.gitignore`，**严禁提交或分享**
- 只处理当前账号有权访问的课程，不做权限绕过、不做批量爬取
- 请遵守学校相关规定，合理使用；抓取频率以个人学习用途为限

## 致谢

本项目的接口实现大量复用与翻译自以下优秀开源项目，感谢原作者：

- [zju-scholar](https://github.com/Lucent-Snow/zju-scholar)（GPL-3.0）— 统一认证 / 教务 / 学在浙大 / 智云课堂 API 实现，本仓库的 `zju_auth.py`、`zju_api.py`、`zju_zhiyun.py`、`zju_webvpn.py`、`zju_session.py` 等模块直接复用其接口层
- [Celechron](https://github.com/Celechron/Celechron)（GPL-3.0）— 浙大统一认证 RSA + CAS 登录与各服务登录流程的 Dart 原始实现，`zju_auth.py` 翻译自其 `zjuam.dart` / `zdbk.dart` / `courses.dart`

## License

[GPL-3.0](LICENSE)。本项目为 [zju-scholar](https://github.com/Lucent-Snow/zju-scholar) 与 [Celechron](https://github.com/Celechron/Celechron) 的派生作品，故沿用 GPL-3.0 协议开源。
