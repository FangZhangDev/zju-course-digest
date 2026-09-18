"""zju_collect.py — 按课次聚合智云 + 学在浙大数据到 data/课程名/YYYY-MM-DD/

复用 zju-scholar 的接口实现（zju_session/zju_zhiyun/zju_api），
不重新逆向任何接口。核心产出：

  data/<课程名>/<YYYY-MM-DD>/
    metadata.json          课程与讲次元信息
    transcript_raw.json    原始 ASR 分段（start_sec/end_sec/text）
    transcript_clean.txt   清洗后带时间戳的纯文本
    slides.pdf             PPT 截图去重后合成 PDF
    slide_timeline.json    PPT 页 -> created_sec -> 对应字幕区间
    course_todos.json      学在浙大 todos + 课程作业活动
    coursewares/           学在浙大原始课件（优先原始 PPT/PDF）

用法：
  python zju_collect.py list-courses                     # 列出智云+学在浙大课程
  python zju_collect.py collect --course 数据科学         # 抓取该课最新一节
  python zju_collect.py collect --course 数据科学 --sub-id 1912570
  python zju_collect.py collect --course 数据科学 --date 2026-09-15
  python zju_collect.py collect --course 数据科学 --skip-coursewares
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import struct
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zju_console import ensure_direct_network, ensure_utf8_io
from zju_session import get_courses_api, get_zhiyun_api, load_session
from zju_zhiyun import ZhiyunApi

# 学在浙大下载也需要 legacy DH SSL 兼容（与 zju_api 内部一致）
from zju_api import CoursesApi

TZ8 = timezone(timedelta(hours=8))

URL_PPT = "https://classroom.zju.edu.cn/pptnote/v1/schedule/search-ppt"


async def get_ppt_timeline_safe(api: ZhiyunApi, course_id: str, sub_id: str) -> list[dict]:
    """PPT timeline 安全版。

    上游 get_ppt_timeline 假设接口支持分页（page/per_page），但实测
    search-ppt 接口忽略分页参数、一次性返回全部帧（265 帧 per_page=50/100/全量
    均相同）。当帧数 >= per_page 时上游的 while True 循环永不终止。
    这里改为单次请求直接取全量。
    """
    headers = api._headers.copy()
    headers["Referer"] = f"https://classroom.zju.edu.cn/livingroom?sub_id={sub_id}"

    async with api._make_client() as client:
        resp = await client.get(
            api._url(URL_PPT),
            headers=headers,
            params={"course_id": str(course_id), "sub_id": str(sub_id), "page": 1, "per_page": 10000},
        )
        data = resp.json()

    timeline = []
    for item in data.get("list", []) or []:
        if not isinstance(item, dict):
            continue
        content = api._parse_embedded_json(item.get("content"))
        timeline.append({
            "course_id": str(course_id),
            "sub_id": str(sub_id),
            # 接口无唯一 slide id（id/old_id 均为空），用 created_sec 兼作标识
            "slide_id": item.get("id") or item.get("old_id") or item.get("created_sec"),
            "created_sec": int(item.get("created_sec", 0) or 0),
            "image_url": content.get("pptimgurl", ""),
            "title": content.get("title", ""),
        })

    # 按 created_sec 升序去重（同秒多帧保留一张）
    timeline.sort(key=lambda x: x["created_sec"])
    deduped = []
    for t in timeline:
        if deduped and deduped[-1]["created_sec"] == t["created_sec"]:
            continue
        deduped.append(t)
    return deduped


def log(level: str, message: str, **kv):
    """标准日志：timestamp level: message key=value"""
    parts = [f"{datetime.now(TZ8).isoformat(timespec='seconds')} {level}: {message}"]
    parts += [f"{k}={v}" for k, v in kv.items()]
    print(" ".join(parts), file=sys.stderr)


def safe_dirname(name: str) -> str:
    """课程名转安全目录名（保留中文，去掉路径非法字符）。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return name[:80] or "unknown_course"


def jpeg_size(data: bytes) -> tuple[int, int, int]:
    """解析 JPEG SOF，返回 (width, height, n_components)；失败返回 (0,0,0)。"""
    from io import BytesIO
    buf = BytesIO(data)
    buf.read(2)  # SOI
    while True:
        marker = buf.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return 0, 0, 0
        if marker[1] in (0xC0, 0xC1, 0xC2):
            buf.read(3)  # length + precision
            h = struct.unpack(">H", buf.read(2))[0]
            w = struct.unpack(">H", buf.read(2))[0]
            ncomp = buf.read(1)[0]
            return w, h, ncomp
        length = struct.unpack(">H", buf.read(2))[0]
        buf.read(length - 2)


def images_to_pdf(img_paths: list[str], output_path: Path):
    """JPEG 合成 PDF（无依赖，规范结构：每页 Page 字典 + Image XObject + 内容流）。"""
    PAGE_W, PAGE_H = 612, 792
    n = len(img_paths)
    pdf = b"%PDF-1.4\n"
    offsets = []

    # obj 1: Catalog
    offsets.append(len(pdf))
    pdf += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"

    # obj 2: Pages（每页 3 个对象：page=3+i*3, img=4+i*3, content=5+i*3）
    kids = " ".join(f"{3 + i * 3} 0 R" for i in range(n))
    offsets.append(len(pdf))
    pdf += f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {n} >>\nendobj\n".encode()

    for i, img_path in enumerate(img_paths):
        img_data = Path(img_path).read_bytes()
        w, h, ncomp = jpeg_size(img_data)
        if w == 0 or h == 0:
            w, h, ncomp = 1280, 720, 3
        colorspace = "DeviceGray" if ncomp == 1 else "DeviceRGB"
        scale = min(PAGE_W / w, PAGE_H / h)
        dw, dh = w * scale, h * scale
        x, y = (PAGE_W - dw) / 2, (PAGE_H - dh) / 2

        page_obj = 3 + i * 3
        img_obj = page_obj + 1
        content_obj = page_obj + 2

        # Page 字典（上游 zju-scholar 缺失这一段，导致 PDF 无法按页渲染）
        offsets.append(len(pdf))
        pdf += (
            f"{page_obj} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /XObject << /Img{i} {img_obj} 0 R >> >> "
            f"/Contents {content_obj} 0 R >>\nendobj\n"
        ).encode()

        # 图片 XObject
        offsets.append(len(pdf))
        pdf += (
            f"{img_obj} 0 obj\n<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
            f"/ColorSpace /{colorspace} /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {len(img_data)} >>\nstream\n"
        ).encode() + img_data + b"\nendstream\nendobj\n"

        # 页面内容流
        content = f"q {dw:.2f} 0 0 {dh:.2f} {x:.2f} {y:.2f} cm /Img{i} Do Q".encode()
        offsets.append(len(pdf))
        pdf += (
            f"{content_obj} 0 obj\n<< /Length {len(content)} >>\nstream\n"
        ).encode() + content + b"\nendstream\nendobj\n"

    xref_pos = len(pdf)
    total_objs = 2 + n * 3 + 1
    pdf += f"xref\n0 {total_objs}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()

    output_path.write_bytes(pdf)


def dedup_adjacent_indices(img_paths: list[str], threshold: int = 12) -> tuple[list[int], int]:
    """相邻感知哈希去重，返回 (保留的索引列表, 去掉数量)。不删除文件。"""
    try:
        from PIL import Image
        import imagehash
    except ImportError:
        log("WARN", "imagehash 未安装，跳过去重")
        return list(range(len(img_paths))), 0

    if len(img_paths) <= 1:
        return list(range(len(img_paths))), 0

    kept_idx = [0]
    last_h = imagehash.phash(Image.open(img_paths[0]), hash_size=16)
    for i, p in enumerate(img_paths[1:], start=1):
        h = imagehash.phash(Image.open(p), hash_size=16)
        if h - last_h > threshold:
            kept_idx.append(i)
            last_h = h
    removed = len(img_paths) - len(kept_idx)
    return kept_idx, removed


def fmt_ts(total_sec: int) -> str:
    """秒 -> HH:MM:SS"""
    s = max(0, int(total_sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """返回一个不读环境代理的 opener。

    urllib 默认会通过 getproxies() 读取 HTTP_PROXY / HTTPS_PROXY，
    在注入了代理变量的 Host（IDE / Agent 运行时）下会把校内直连的
    图片下载请求送到代理，导致下载失败。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def download_image(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _no_proxy_opener().open(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        log("WARN", f"PPT 图片下载失败 url={url} err={e}")
        return False


# ──────────────────────────── 智云部分 ────────────────────────────

async def list_zhiyun_courses(api: ZhiyunApi) -> list[dict]:
    """列出智云课程：合并「近期学习」与「我的课程」，去重后返回。

    不能只取「近期学习」：那是有过观看记录才会出现的列表，刚开课、或从未在线上
    打开过的新课不会在里面，导致 list-courses 看不到、collect 也匹配不到该课程。
    这里以近期学习优先、再补上我的课程，按 course_id 去重。
    """
    merged: list[dict] = []
    seen: set[str] = set()

    def _add(items):
        for c in items or []:
            cid = str(c.get("course_id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            merged.append(c)

    try:
        _add(await api.get_recent_learning(per_page=20))
    except Exception as exc:  # noqa: BLE001
        log("WARN", "获取智云「近期学习」失败", err=str(exc))
    try:
        _add(await api.get_my_courses(per_page=100))
    except Exception as exc:  # noqa: BLE001
        log("WARN", "获取智云「我的课程」失败", err=str(exc))
    return merged


async def resolve_zhiyun_course(api: ZhiyunApi, keyword: str) -> dict | None:
    keyword = keyword.strip()
    courses = await list_zhiyun_courses(api)
    if not courses:
        return None
    if not keyword:
        # 未给关键字：唯一课程才敢直接返回；多门课时拒绝猜测
        return courses[0] if len(courses) == 1 else None

    # 复用归一化匹配（兼容「XX（研）」vs「XX」写法差异）
    matched = match_course_by_keyword(courses, keyword, key="title")
    if matched is not None:
        return matched

    # 兜底：归一化后前两字命中（仅在无其他候选时）
    kw_norm = normalize_course_name(keyword)
    if len(kw_norm) >= 2:
        cands = [c for c in courses
                 if kw_norm[:2] in normalize_course_name(c.get("title", ""))]
        if len(cands) == 1:
            return cands[0]
    return None


def build_transcript_clean(segments: list[dict]) -> str:
    """带时间戳的清洗纯文本：去口头语、合并重复，保留原始分段顺序。"""
    filler_re = ZhiyunApi._strip_leading_fillers
    lines = []
    last = None
    for seg in segments:
        text = seg["text"]
        text = filler_re(text)
        if not text or ZhiyunApi._is_low_information_text(text):
            continue
        if text == last:
            continue
        last = text
        lines.append(f"[{fmt_ts(seg['start_sec'])} - {fmt_ts(seg['end_sec'])}] {text}")
    return "\n".join(lines)


def build_slide_timeline(
    timeline: list[dict], segments: list[dict]
) -> list[dict]:
    """PPT 页(created_sec) <-> 字幕区间(start/end) 对应。

    每页 PPT 附：覆盖的字幕时间窗、页内出现的字幕文本（截断保存）。
    """
    result = []
    for i, slide in enumerate(timeline):
        created = slide["created_sec"]
        next_created = timeline[i + 1]["created_sec"] if i + 1 < len(timeline) else None
        # 本页显示期间的字幕：start_sec >= created 且 (下一页之前)
        cover = [
            seg for seg in segments
            if seg["start_sec"] >= created and (next_created is None or seg["start_sec"] < next_created)
        ]
        result.append({
            "page": i + 1,
            "slide_id": slide.get("slide_id"),
            "created_sec": created,
            "created_hms": fmt_ts(created),
            "image_url": slide.get("image_url", ""),
            "title": slide.get("title", ""),
            "speech_start_sec": cover[0]["start_sec"] if cover else None,
            "speech_end_sec": cover[-1]["end_sec"] if cover else None,
            "speech_text": "".join(s["text"] for s in cover)[:2000],
        })
    return result


async def collect_lecture(
    zhiyun: ZhiyunApi,
    course: dict,
    sub_id: str | None,
    out_dir: Path,
    *,
    dedup_threshold: int = 12,
    skip_slides: bool = False,
    keep_slides_tmp: bool = False,
) -> dict:
    """抓一节课：字幕 + PPT + 时间轴对应。"""
    course_id = str(course["course_id"])
    course_title = course.get("title", "unknown")

    if sub_id:
        videos = await zhiyun.get_course_videos(course_id)
        target = next((v for v in videos if str(v["sub_id"]) == str(sub_id)), None)
        if target is None:
            # catalogue 接口找不到就直接构造
            target = {"sub_id": sub_id, "title": "", "start_at": "", "end_at": ""}
    else:
        videos = await zhiyun.get_course_videos(course_id)
        if not videos:
            raise RuntimeError(f"智云课程 {course_title} 没有可用视频")
        # 优先选"有字幕的最新一节"（status=6）：
        # 最新一节常尚未转码出字幕（status=2），抓了也没有内容
        with_subs = [v for v in videos if str(v.get("status")) == "6"]
        target = with_subs[0] if with_subs else videos[0]
        sub_id = str(target["sub_id"])
        if not with_subs:
            log("WARN", "该课程所有讲次均无字幕（可能尚未转码完成）", sub_id=sub_id)

    log("INFO", "开始抓取课次", course=course_title, sub_id=sub_id)
    meta = {
        "course_id": course_id,
        "course_name": course_title,
        "sub_id": str(sub_id),
        "lecture_title": target.get("title") or target.get("sub_title") or "",
        "lecturer": target.get("lecturer_name", "") or course.get("teacher", ""),
        "start_at": target.get("start_at", ""),
        "end_at": target.get("end_at", ""),
        "duration_sec": target.get("duration", 0),
        "collected_at": datetime.now(TZ8).isoformat(),
    }

    # ---- 字幕 ----
    transcript_raw = await zhiyun.get_transcript(sub_id)
    segments = ZhiyunApi._normalize_transcript_segments(transcript_raw)
    meta["has_transcript"] = bool(segments)
    meta["transcript_segments"] = len(segments)

    (out_dir / "transcript_raw.json").write_text(
        json.dumps({"sub_id": sub_id, "segments": segments}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    clean = build_transcript_clean(segments)
    (out_dir / "transcript_clean.txt").write_text(clean, encoding="utf-8")
    log("INFO", "字幕完成", segments=len(segments), clean_chars=len(clean))

    # ---- PPT ----
    timeline = [] if skip_slides else await get_ppt_timeline_safe(zhiyun, course_id, sub_id)
    meta["ppt_frames"] = len(timeline)

    if timeline:
        tmpdir = out_dir / "_slides_tmp"
        tmpdir.mkdir(exist_ok=True)
        img_files = []
        kept_timeline = []
        for idx, slide in enumerate(timeline):
            p = tmpdir / f"s{idx:04d}.jpg"
            if download_image(slide["image_url"], p):
                img_files.append(str(p))
                kept_timeline.append(slide)

        removed = 0
        kept_indices = list(range(len(img_files)))
        if img_files:
            kept_indices, removed = dedup_adjacent_indices(img_files, dedup_threshold)
            pdf_path = out_dir / "slides.pdf"
            images_to_pdf([img_files[i] for i in kept_indices], pdf_path)
            meta["slides_in_pdf"] = len(kept_indices)
            meta["slides_dedup_removed"] = removed
        else:
            meta["slides_in_pdf"] = 0

        # PDF 第 N 页对应 kept_indices[N-1] 帧的 created_sec
        pdf_page_times = [kept_timeline[i]["created_sec"] for i in kept_indices if i < len(kept_timeline)]

        slide_timeline = build_slide_timeline(timeline, segments)
        # 每个 timeline 帧映射到它之前最近的保留帧（即 PDF 页码）
        for entry in slide_timeline:
            entry["pdf_page"] = None
            best = None
            for pdf_idx, t in enumerate(pdf_page_times):
                if t <= entry["created_sec"] and (best is None or t > pdf_page_times[best]):
                    best = pdf_idx
            entry["pdf_page"] = (best + 1) if best is not None else None

        (out_dir / "slide_timeline.json").write_text(
            json.dumps(slide_timeline, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 清理临时图片。
        # 注意：这一步只是收尾，失败不应影响已产出的 slides.pdf / slide_timeline.json。
        # 某些 IDE / Agent 运行时会对批量删除做二次确认（或沙箱拦截），
        # 直接抛出会让整次采集前功尽弃，因此这里吞掉异常并提示可手动删除。
        keep_tmp = os.environ.get("ZJU_KEEP_SLIDES_TMP") == "1" or keep_slides_tmp
        if keep_tmp:
            log("INFO", "保留临时图片目录", path=str(tmpdir))
        else:
            try:
                for p in tmpdir.glob("*.jpg"):
                    p.unlink()
                tmpdir.rmdir()
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响产物
                log("WARN", "临时图片清理失败，可手动删除该目录", path=str(tmpdir), error=str(exc))
        log("INFO", "PPT 完成", frames=len(timeline), in_pdf=len(kept_indices), removed=removed)
    else:
        (out_dir / "slide_timeline.json").write_text("[]", encoding="utf-8")
        meta["slides_in_pdf"] = 0
        log("INFO", "该课次无 PPT timeline")

    (out_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


# ──────────────────────── 课程名匹配（跨平台共用） ────────────────────────

def normalize_course_name(name: str) -> str:
    """归一化课程名用于跨平台匹配。

    智云与学在浙大对同一门课的命名常有差异，例如：
      - 智云「医学统计软件的应用（研）」 vs 学在浙大「医学统计软件的应用」
      - 学在浙大偶有尾部制表符/全角空格
    这里统一去掉学制后缀、括号注释与空白，只留主标题。
    """
    if not name:
        return ""
    s = str(name).strip()
    # 去掉各类空白（含制表符、全角空格）
    s = re.sub(r"[\s\u3000]+", "", s)
    # 去掉括号及其中内容（含全角/半角），如「（研）」「(研究生)」
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s


def match_course_by_keyword(courses: list[dict], keyword: str, key: str = "name") -> dict | None:
    """在课程列表中按关键字匹配课程，归一化后比较。

    匹配优先级：
      1. 归一化后完全相等
      2. 归一化后关键字包含
      3. 原始字段包含关键字
      4. 归一化后前两字命中
    """
    kw = str(keyword or "").strip()
    if not kw:
        return None
    kw_norm = normalize_course_name(kw)

    def names(c):
        return str(c.get(key, "") or "")

    # 1. 归一化完全相等
    for c in courses:
        if kw_norm and normalize_course_name(names(c)) == kw_norm:
            return c
    # 2. 归一化后包含
    for c in courses:
        if kw_norm and kw_norm in normalize_course_name(names(c)):
            return c
    # 3. 原字段包含
    for c in courses:
        if kw in names(c):
            return c
    # 4. 归一化后前两字命中
    if len(kw_norm) >= 2:
        for c in courses:
            if kw_norm[:2] in normalize_course_name(names(c)):
                return c
    return None


# ──────────────────────────── 学在浙大部分 ────────────────────────────

async def _collect_xuezai_async(
    courses_api: CoursesApi, keyword: str, out_dir: Path, skip_coursewares: bool,
    also_keywords: list[str] | None = None,
) -> dict:
    result = {"course_id": None, "course_name": "", "todos": [], "assignments": [], "coursewares": [], "downloaded": []}

    # 1. 课程列表 -> 匹配
    courses_data = await courses_api.get_my_courses(statuses=["ongoing", "notStarted"])
    all_courses = courses_data.get("courses", []) if isinstance(courses_data, dict) else courses_data

    # 依次用主关键字与候选关键字尝试（智云名与学在浙大名可能不同）
    candidates = [keyword] + [k for k in (also_keywords or []) if k and k != keyword]
    matched = None
    for kw_try in candidates:
        matched = match_course_by_keyword(all_courses, kw_try)
        if matched is not None:
            break

    if matched is None:
        log("WARN", "学在浙大未匹配到课程", keyword=keyword)
        (out_dir / "course_todos.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    course_id = matched["id"]
    result["course_id"] = str(course_id)
    result["course_name"] = matched.get("name", "")
    log("INFO", "学在浙大课程匹配", course=result["course_name"], course_id=course_id)

    # 2. 全局 todos 过滤出该课程
    try:
        todos = await courses_api.get_todos()
        # 用归一化名称比较，兼容「XX（研）」与「XX」的差异
        norm_targets = {
            normalize_course_name(k)
            for k in ([keyword] + (also_keywords or []))
            if normalize_course_name(k)
        }
        norm_matched = normalize_course_name(result["course_name"])
        if norm_matched:
            norm_targets.add(norm_matched)
        result["todos"] = [
            t for t in todos
            if normalize_course_name(t.get("course_name")) in norm_targets
        ]
        result["all_todos_count"] = len(todos)
    except Exception as e:
        log("WARN", "获取 todos 失败", err=str(e))

    # 3. 课程活动里的作业类（assignment/homework 类型）
    try:
        activities = await courses_api.get_course_activities(course_id)
        for act in activities:
            atype = str(act.get("type", "")).lower()
            if any(k in atype for k in ("assignment", "homework", "exam", "quiz")):
                result["assignments"].append({
                    "activity_id": act.get("id"),
                    "title": act.get("title", ""),
                    "type": act.get("type", ""),
                    "end_time": act.get("end_time") or act.get("due_at") or "",
                    "start_time": act.get("start_time") or "",
                    "status": act.get("status", ""),
                })
    except Exception as e:
        log("WARN", "获取课程活动失败", err=str(e))

    (out_dir / "course_todos.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("INFO", "todos 完成", todos=len(result["todos"]), assignments=len(result["assignments"]))

    # 4. 课件下载
    if skip_coursewares:
        return result

    cw_dir = out_dir / "coursewares"
    cw_dir.mkdir(exist_ok=True)
    try:
        cws = await courses_api.get_coursewares(course_id, page_size=100)
        items = cws.get("coursewares", []) if isinstance(cws, dict) else []
        result["coursewares"] = [
            {k: v for k, v in it.items() if k != "raw"} for it in items
        ]
        for it in items:
            upload_id = it.get("upload_id")
            name = it.get("name") or f"upload_{upload_id}"
            if not upload_id:
                continue
            dest = cw_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                result["downloaded"].append({"name": name, "skipped": "exists"})
                continue
            try:
                dl = await courses_api.download_resource(upload_id, cw_dir)
                result["downloaded"].append(dl)
                log("INFO", "课件下载", name=name, size=dl.get("size", 0))
            except Exception as e:
                result["downloaded"].append({"name": name, "error": str(e)})
                log("WARN", "课件下载失败", name=name, err=str(e))
    except Exception as e:
        log("WARN", "获取课件列表失败", err=str(e))

    # 下载完成后把 course_todos.json 重写（补 downloaded）
    (out_dir / "course_todos.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


# ──────────────────────────── CLI ────────────────────────────

async def _async_main(args):
    session = load_session()
    if not session:
        print(json.dumps({"ok": False, "error": "未登录，先运行 zju_login.py"}, ensure_ascii=False))
        sys.exit(2)

    base = Path(args.data_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)

    if args.command == "list-courses":
        zhiyun = get_zhiyun_api()
        zy = await list_zhiyun_courses(zhiyun)
        try:
            courses_api = get_courses_api()
            xz_data = await courses_api.get_my_courses(statuses=["ongoing", "notStarted"])
            xz = xz_data.get("courses", []) if isinstance(xz_data, dict) else xz_data
        except Exception as e:
            log("WARN", "学在浙大课程列表失败", err=str(e))
            xz = []
        print(json.dumps({
            "ok": True,
            "zhiyun": [{"course_id": c.get("course_id"), "title": c.get("title"), "teacher": c.get("teacher") or c.get("lecturer_name", "")} for c in zy],
            "xuezai": [{"course_id": c.get("id"), "name": c.get("name"), "teachers": c.get("instructors", [])} for c in xz],
        }, ensure_ascii=False, indent=2))
        return

    # collect
    keyword = args.course
    zhiyun = get_zhiyun_api()

    course = await resolve_zhiyun_course(zhiyun, keyword)
    if course is None:
        print(json.dumps({"ok": False, "error": f"智云未找到课程: {keyword}"}, ensure_ascii=False))
        sys.exit(1)

    course_dirname = safe_dirname(course.get("title") or keyword)
    date_str = args.date or datetime.now(TZ8).strftime("%Y-%m-%d")
    out_dir = base / course_dirname / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    log("INFO", "输出目录", path=str(out_dir))

    meta = await collect_lecture(
        zhiyun, course, args.sub_id, out_dir,
        dedup_threshold=args.dedup_threshold,
        skip_slides=args.no_slides,
        keep_slides_tmp=args.keep_slides_tmp,
    )

    # 学在浙大部分（todos + 课件）
    try:
        courses_api = get_courses_api()
        # 智云与学在浙大课程名可能不同（如「XX（研）」vs「XX」），
        # 把智云的实际课程名一并作为候选关键字。
        zhiyun_title = course.get("title") or ""
        await _collect_xuezai_async(
            courses_api, keyword, out_dir, args.skip_coursewares,
            also_keywords=[zhiyun_title],
        )
    except Exception as e:
        log("WARN", "学在浙大数据获取失败（智云部分已完成）", err=str(e))
        (out_dir / "course_todos.json").write_text(
            json.dumps({"error": str(e)}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(json.dumps({"ok": True, "course_dir": str(out_dir), "meta": meta}, ensure_ascii=False, indent=2))


def main():
    ensure_utf8_io()
    ensure_direct_network()
    parser = argparse.ArgumentParser(description="按课次聚合智云 + 学在浙大数据")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list-courses", help="列出智云 + 学在浙大当前课程")
    p_list.add_argument("--data-dir", default=None)

    p_col = sub.add_parser("collect", help="抓取一节课")
    p_col.add_argument("--course", required=True, help="课程名关键字")
    p_col.add_argument("--sub-id", default=None, help="指定智云 sub_id（默认最新一节）")
    p_col.add_argument("--date", default=None, help="输出目录日期 YYYY-MM-DD（默认今天）")
    p_col.add_argument("--data-dir", default=None, help="数据根目录（默认 <skill>/data/courses）")
    p_col.add_argument("--skip-coursewares", action="store_true", help="跳过学在浙大课件下载")
    p_col.add_argument("--no-slides", action="store_true", help="跳过 PPT 下载")
    p_col.add_argument("--dedup-threshold", type=int, default=12, help="相邻帧哈希距离阈值（默认12，越大去重越狠）")
    p_col.add_argument("--keep-slides-tmp", action="store_true", help="保留 PPT 中间图目录 _slides_tmp（调试用）")
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.data_dir is None:
        args.data_dir = Path(__file__).resolve().parent.parent / "data" / "courses"

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
