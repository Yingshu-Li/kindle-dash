"""把课程表 / 待办 / 天气渲染成 600x800 的 8 位灰度 PNG。

同时产出 hitmap.json，内含：
  - 时钟区域坐标（设备端每分钟在此局部重绘时间）
  - 状态区域坐标（设备端在此显示离线提示）
  - 每条待办的屏幕矩形与 issue 编号（供点击完成使用）
设备端一切坐标都从 hitmap 读取，改版面不需要同步改设备脚本。

用法：
    python render.py                     # CI：真实数据
    python render.py --stub              # 本地：假待办，用于调版面
    python render.py --at "2026-08-06 09:15"   # 模拟某个时刻，验证高亮逻辑
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
import sources  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# 灰度取值。设备是 8bit Y8，0=黑 255=白。
BLACK = 0
GRAY_DARK = 90
GRAY_MID = 150
GRAY_LIGHT = 210
GRAY_BG = 226
WHITE = 255


# ---------------------------------------------------------------- 字体


def _font_candidates(cfg_path: str | None, bold: bool) -> list[tuple[str, int]]:
    """按优先级列出候选字体：仓库自带 > 开发机系统字体。

    仓库自带的排第一，保证 CI 和本地预览产出一致；系统字体只是本地兜底，
    避免字体还没就位时完全跑不起来。粗细两套要分别回落，否则本地预览里
    加粗完全看不出来。
    """
    out: list[tuple[str, int]] = []
    if cfg_path:
        out.append((str(REPO_ROOT / cfg_path), 0))
    if bold:
        out += [
            ("C:/Windows/Fonts/msyhbd.ttc", 0),   # 微软雅黑 Bold
            ("C:/Windows/Fonts/simhei.ttf", 0),   # 黑体，本身就偏粗
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
        ]
    out += [
        ("C:/Windows/Fonts/msyh.ttc", 0),         # 微软雅黑 Regular
        ("C:/Windows/Fonts/simhei.ttf", 0),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
        ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ]
    return out


class Fonts:
    """按需加载并缓存字号，避免每次绘制都重新打开字体文件。

    优先使用仓库自带的 Noto Sans SC 可变字体：一个文件覆盖全部字重。
    注意它的字重轴默认值是 100(Thin)，**必须显式设定**，否则字形极细，
    在 167dpi 墨水屏上几乎无法阅读。
    """

    def __init__(self, cfg: dict):
        f = cfg.get("fonts") or {}
        self._w_regular = int(f.get("weight_regular", 400))
        self._w_bold = int(f.get("weight_bold", 700))
        self._cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}

        # 可变字体路径（首选）
        vf = f.get("file")
        self._vf = str(REPO_ROOT / vf) if vf and os.path.exists(REPO_ROOT / vf) else None

        # 回落：系统静态字体，粗细分别解析
        self._regular = None if self._vf else self._resolve(f.get("regular"), bold=False)
        self._bold = None if self._vf else (
            self._resolve(f.get("bold"), bold=True) or self._regular
        )

        if not self._vf and not self._regular:
            raise SystemExit(
                "找不到任何可用字体。确认 assets/fonts/ 下有 Noto 可变字体，"
                "或开发机上存在微软雅黑/黑体。"
            )

    @staticmethod
    def _resolve(cfg_path: str | None, bold: bool) -> tuple[str, int] | None:
        for path, index in _font_candidates(cfg_path, bold):
            if os.path.exists(path):
                return (path, index)
        return None

    def get(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (bold, size)
        if key in self._cache:
            return self._cache[key]

        if self._vf:
            font = ImageFont.truetype(self._vf, size)
            # 每个字号一个独立对象，所以可以各自设定字重互不干扰
            font.set_variation_by_axes([self._w_bold if bold else self._w_regular])
        else:
            path, index = self._bold if bold else self._regular
            font = ImageFont.truetype(path, size, index=index)

        self._cache[key] = font
        return font

    @property
    def source(self) -> str:
        if self._vf:
            return f"{os.path.basename(self._vf)} (wght {self._w_regular}/{self._w_bold})"
        return f"regular={self._regular[0]}  bold={self._bold[0]}"


# ---------------------------------------------------------------- 绘制辅助


def text_width(draw: ImageDraw.ImageDraw, s: str, font) -> int:
    return int(draw.textlength(s, font=font))


def ellipsize(draw: ImageDraw.ImageDraw, s: str, font, max_w: int) -> str:
    """超宽则截断加省略号。中文没有词边界，按字符逐个退。"""
    if text_width(draw, s, font) <= max_w:
        return s
    ell = "…"
    ell_w = text_width(draw, ell, font)
    out = ""
    for ch in s:
        if text_width(draw, out + ch, font) + ell_w > max_w:
            break
        out += ch
    return out + ell


# ---------------------------------------------------------------- 各区域


def draw_header(draw, fonts, cfg, now, weather, hitmap,
                next_period=None, next_course=None, preview_clock=False):
    lay = cfg["layout"]
    mx = lay["margin_x"]
    clock = lay["clock_rect"]  # x1, y1, x2, y2

    # 时钟区域留空，由设备端每分钟局部重绘。这里把设备需要的几何量全部算好。
    #
    # fbink 有两套坐标语义，混用会出错，所以两种都预先算出来：
    #   -t  的 top/left/right/bottom 是「距屏幕各边的边距」
    #   -k  的 top/left/width/height 是「坐标 + 尺寸」
    top_off = int(cfg["screen"].get("top_offset", 0))
    W = cfg["screen"]["width"]
    PH = cfg["screen"]["height"] + top_off          # 屏幕物理高度
    cx1, cy1, cx2, cy2 = clock
    sy1, sy2 = cy1 + top_off, cy2 + top_off         # 转成屏幕坐标
    ch = cy2 - cy1

    hitmap["clock"] = {
        # 给 -t 用的边距
        "top": sy1,
        "left": cx1,
        "right": W - cx2,
        "bottom": PH - sy2,
        # 像素字号。取区域高度的七成 —— fbink 的行高比字号本身大
        #（含上下伸部），给满会被判定放不下而整个截断。
        "px": int(ch * 0.70),
        # 给 -k 用的坐标+尺寸
        "cls": {"top": sy1, "left": cx1, "width": cx2 - cx1, "height": ch},
    }

    if preview_clock:
        # 仅本地预览：画个占位时钟，方便判断整体构图。CI 产出绝不能带它，
        # 否则会和设备端每分钟重绘的时间叠在一起。
        draw.text((cx1, cy1), f"{now:%H:%M}",
                  font=fonts.get(hitmap["clock"]["px"], bold=True), fill=BLACK)
        draw.rectangle([cx1, cy1, cx2, cy2], outline=GRAY_LIGHT, width=1)

    right_x = cx2 + 14
    names = cfg["locale"]["weekday_names"]
    weekday = names[now.isoweekday() - 1]

    f_date = fonts.get(24, bold=True)
    f_sub = fonts.get(15)
    f_next = fonts.get(15, bold=True)

    y = cy1
    draw.text((right_x, y), f"{now.month}月{now.day}日 {weekday}",
              font=f_date, fill=BLACK)
    y += 30

    if weather.ok:
        parts = [f"{weather.location} {weather.description}"]
        if weather.temp_max is not None and weather.temp_min is not None:
            parts.append(f"{round(weather.temp_max)}°/{round(weather.temp_min)}°")
        if weather.precip_prob:
            parts.append(f"降水{weather.precip_prob}%")
        draw.text((right_x, y), "  ".join(parts), font=f_sub, fill=GRAY_DARK)
    else:
        draw.text((right_x, y), "天气获取失败", font=f_sub, fill=GRAY_MID)
    y += 24

    # 「当前/下一节课」区域留白，由设备每分钟重算重绘 ——
    # 这行依赖当前时刻，凝固在图里的话大半节课都是错的。
    # 这里只登记几何量，绘制逻辑在设备端。
    nx1, ny1, nx2, ny2 = lay["next_rect"]
    hitmap["next"] = {
        "top": ny1 + top_off,
        "left": nx1,
        "right": W - nx2,
        "bottom": PH - (ny2 + top_off),
        "px": 17,
        "cls": {"top": ny1 + top_off, "left": nx1,
                "width": nx2 - nx1, "height": ny2 - ny1},
    }

    draw.line([(mx, lay["header_height"]), (600 - mx, lay["header_height"])],
              fill=BLACK, width=2)


def wrap_text(draw, s: str, font, max_w: int, max_lines: int) -> list[str]:
    """按像素宽度折行。中文没有词边界，逐字符累加即可。"""
    lines: list[str] = []
    cur = ""
    for ch in s:
        if text_width(draw, cur + ch, font) <= max_w:
            cur += ch
            continue
        if cur:
            lines.append(cur)
        cur = ch
        if len(lines) == max_lines - 1:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # 内容超出可容纳行数时，最后一行加省略号
    if len(lines) == max_lines:
        consumed = sum(len(x) for x in lines)
        if consumed < len(s):
            lines[-1] = ellipsize(draw, lines[-1] + s[consumed:], font, max_w)
    return lines


def draw_week_grid(draw, fonts, cfg, week, today_wd, cur_period) -> int:
    """画周一到周五 × 8 节的课表网格，返回本区域结束的 y。

    连排的课合并成一个格子；晚上那两节（extra 组）用浅灰底与白天区分；
    今天那一列的表头反白；正在上的那一格加粗边框。
    """
    lay = cfg["layout"]
    sch = cfg["schedule"]
    mx = lay["margin_x"]
    top = lay["grid_top"]
    label_w = lay["grid_label_w"]
    head_h = lay["grid_header_h"]
    row_h = lay["grid_row_h"]

    weekdays = sch["weekdays"]
    periods = sch["periods"]
    names = cfg["locale"]["weekday_names"]

    grid_x = mx + label_w
    grid_w = 600 - mx - grid_x
    col_w = grid_w // len(weekdays)
    grid_right = grid_x + col_w * len(weekdays)
    body_top = top + head_h
    bottom = body_top + row_h * len(periods)

    f_head = fonts.get(15, bold=True)
    # 节次列要在 40px 行高里塞下「号码 + 起止两行」，字号必须收紧，
    # 否则最后一行的结束时间会被网格底线切掉
    f_no = fonts.get(13, bold=True)
    f_time = fonts.get(9)
    # 格子里显示班级（教师视角最需要的信息）。班级名都很短（如「四6班」），
    # 所以字号可以给得很大 —— 一眼就能看清该去哪个班。
    f_name_big = fonts.get(22, bold=True)
    f_name_sm = fonts.get(18, bold=True)
    f_room = fonts.get(11)
    f_mark = fonts.get(11, bold=True)      # 停/加/换 角标

    # ---- 先铺 extra 组的底色，其余元素都画在它上面 ----
    for i, p in enumerate(periods):
        if p.get("group") == "extra":
            y = body_top + i * row_h
            draw.rectangle([mx, y, grid_right, y + row_h], fill=GRAY_BG)

    # ---- 星期表头 ----
    for j, wd in enumerate(weekdays):
        x = grid_x + j * col_w
        is_today = wd == today_wd
        if is_today:
            draw.rectangle([x, top, x + col_w, top + head_h], fill=BLACK)
        label = names[wd - 1]
        tw = text_width(draw, label, f_head)
        draw.text((x + (col_w - tw) // 2, top + 4), label, font=f_head,
                  fill=WHITE if is_today else BLACK)

    # ---- 节次标签列：节次号 + 起止时间 ----
    # 教师需要知道这节课几点下课，所以起止都显示。
    # 列宽为此从 38 加到 50 —— 38px 下第二行会被网格底线切掉。
    for i, p in enumerate(periods):
        y = body_top + i * row_h
        no = str(p["idx"])
        tw = text_width(draw, no, f_no)
        draw.text((mx + (label_w - tw) // 2 - 2, y + 1), no, font=f_no, fill=BLACK)
        for k, t in enumerate((p["start"], p["end"])):
            tw = text_width(draw, t, f_time)
            draw.text((mx + (label_w - tw) // 2 - 2, y + 16 + k * 11), t,
                      font=f_time, fill=GRAY_DARK)

    # ---- 网格线 ----
    # 横线：白天与晚上之间那条画粗，作为分组界线
    prev_group = None
    for i, p in enumerate(periods):
        y = body_top + i * row_h
        g = p.get("group")
        w = 2 if (prev_group and g != prev_group) else 1
        draw.line([(mx, y), (grid_right, y)],
                  fill=GRAY_MID if w == 1 else BLACK, width=w)
        prev_group = g
    draw.line([(mx, top), (grid_right, top)], fill=BLACK, width=1)
    draw.line([(mx, body_top), (grid_right, body_top)], fill=BLACK, width=1)
    draw.line([(mx, bottom), (grid_right, bottom)], fill=BLACK, width=1)

    # 竖线
    for j in range(len(weekdays) + 1):
        x = grid_x + j * col_w
        draw.line([(x, top), (x, bottom)], fill=GRAY_MID, width=1)
    draw.line([(mx, top), (mx, bottom)], fill=GRAY_MID, width=1)

    # ---- 课程格子 ----
    by_no = {int(p["idx"]): idx for idx, p in enumerate(periods)}
    for j, wd in enumerate(weekdays):
        x = grid_x + j * col_w
        for c in week.get(wd, []):
            if c.first not in by_no:
                continue
            i = by_no[c.first]
            y = body_top + i * row_h
            h = row_h * c.span
            active = (wd == today_wd and cur_period is not None
                      and cur_period in c.periods)

            pad = 2
            box = [x + pad, y + pad, x + col_w - pad, y + h - pad]
            if c.cancelled:
                draw.rectangle(box, outline=GRAY_MID, width=1)
            elif active:
                draw.rectangle(box, fill=GRAY_BG, outline=BLACK, width=2)
            else:
                draw.rectangle(box, outline=GRAY_DARK, width=1)

            inner_w = col_w - pad * 2 - 4
            show_room = bool(c.room) and sch.get("show_room", False)
            f_name = f_name_sm if show_room else f_name_big
            line_h = 18 if show_room else 21

            max_name_lines = 2 if h >= row_h * 2 else 1
            lines = wrap_text(draw, c.name, f_name, inner_w, max_name_lines)

            block_h = len(lines) * line_h + (12 if show_room else 0)
            ty = y + (h - block_h) // 2
            name_fill = GRAY_MID if c.cancelled else BLACK
            for ln in lines:
                tw = text_width(draw, ln, f_name)
                lx = x + (col_w - tw) // 2
                draw.text((lx, ty), ln, font=f_name, fill=name_fill)
                if c.cancelled:
                    # 划掉而不是清空：既表明今天不用去，又保留了本来是哪个班
                    my = ty + line_h // 2 + 1
                    draw.line([(lx - 3, my), (lx + tw + 3, my)],
                              fill=BLACK, width=2)
                ty += line_h

            # 被临时调整过的格子加汉字角标：停 / 加 / 换
            if c.mark:
                draw.text((x + pad + 3, y + pad + 1), c.mark,
                          font=f_mark, fill=BLACK)

            if show_room:
                room = ellipsize(draw, c.room, f_room, inner_w)
                tw = text_width(draw, room, f_room)
                draw.text((x + (col_w - tw) // 2, ty), room,
                          font=f_room, fill=GRAY_DARK)

    return bottom


def draw_schedule_unused(draw, fonts, cfg, courses, highlight_idx) -> int:
    """画今日课程，返回本区域实际结束的 y。

    高度随课程数收缩，不占死固定区域 —— 课少的日子把空间让给待办，
    避免屏幕中间出现一大块空白。
    """
    lay = cfg["layout"]
    mx = lay["margin_x"]
    top = lay["schedule_top"]
    bottom = lay["schedule_max_bottom"]

    draw.text((mx, top), "今日课程", font=fonts.get(16, bold=True), fill=GRAY_DARK)
    items_top = top + 28

    if not courses:
        draw.text((mx, items_top + 4), "今天没有课", font=fonts.get(20), fill=GRAY_MID)
        end_y = items_top + 34
        draw.line([(mx, end_y), (600 - mx, end_y)], fill=GRAY_MID, width=2)
        return end_y

    max_items = cfg["schedule"]["max_items_today"]
    shown = courses[:max_items]
    row_h = min(46, max((bottom - items_top) // max(len(shown), 1), 40))

    f_time = fonts.get(17)
    f_time_end = fonts.get(14)
    f_name = fonts.get(21, bold=True)
    f_room = fonts.get(14)

    for i, c in enumerate(shown):
        y = items_top + i * row_h
        is_hl = i == highlight_idx

        if is_hl:
            # 浅灰底 + 左侧黑色粗条：比整行反白轻，但在墨水屏上依然一眼可辨
            draw.rectangle([mx, y, 600 - mx, y + row_h - 4], fill=GRAY_BG)
            draw.rectangle([mx, y, mx + 5, y + row_h - 4], fill=BLACK)

        tx = mx + 14
        draw.text((tx, y + 4), c.start, font=f_time, fill=BLACK)
        draw.text((tx, y + 24), c.end, font=f_time_end, fill=GRAY_DARK)

        nx = mx + 90
        name_w = 600 - mx - nx - 8
        draw.text((nx, y + 2), ellipsize(draw, c.name, f_name, name_w),
                  font=f_name, fill=BLACK)

        sub = " · ".join(p for p in (c.room, c.note) if p)
        if sub:
            draw.text((nx, y + 26), ellipsize(draw, sub, f_room, name_w),
                      font=f_room, fill=GRAY_DARK)

    end_y = items_top + len(shown) * row_h
    if len(courses) > max_items:
        draw.text((mx + 90, end_y), f"还有 {len(courses) - max_items} 节",
                  font=f_room, fill=GRAY_MID)
        end_y += 22

    end_y += 8
    draw.line([(mx, end_y), (600 - mx, end_y)], fill=GRAY_MID, width=2)
    return end_y


def draw_todos(draw, fonts, cfg, todos, today, hitmap, top):
    """top 由课程区的实际结束位置决定，不是固定值。"""
    lay = cfg["layout"]
    mx = lay["margin_x"]
    bottom = lay["todo_bottom"]

    draw.text((mx, top), "待办", font=fonts.get(16, bold=True), fill=GRAY_DARK)
    items_top = top + 28

    hitmap["todos"] = []

    if not todos:
        draw.text((mx, items_top + 16), "没有待办事项", font=fonts.get(20), fill=GRAY_MID)
        return

    row_h = 27
    capacity = max((bottom - items_top) // row_h, 1)
    max_items = min(cfg["todo"]["max_items"], capacity)
    # 放不下时「还有 N 条」要占一行，不预留的话它会压到页脚上
    if len(todos) > max_items and max_items > 1:
        max_items -= 1
    shown = todos[:max_items]

    f_item = fonts.get(18)
    f_item_b = fonts.get(18, bold=True)
    f_due = fonts.get(13)

    for i, t in enumerate(todos[:max_items]):
        y = items_top + i * row_h

        # 复选框
        box = [mx, y + 4, mx + 15, y + 19]
        draw.rectangle(box, outline=BLACK, width=2)

        # 截止日徽标靠右
        due_w = 0
        if t.due:
            days = (t.due - today).days
            if days < 0:
                label = "已逾期"
            elif days == 0:
                label = "今天"
            elif days == 1:
                label = "明天"
            else:
                label = f"{days}天"
            due_w = text_width(draw, label, f_due) + 10
            overdue = days <= 0
            bx1 = 600 - mx - due_w
            draw.rectangle([bx1, y + 4, 600 - mx, y + 20],
                           fill=BLACK if overdue else None,
                           outline=BLACK, width=1)
            draw.text((bx1 + 5, y + 5), label, font=f_due,
                      fill=WHITE if overdue else BLACK)

        tx = mx + 24
        avail = 600 - mx - tx - (due_w + 8 if due_w else 0)
        font = f_item_b if t.priority else f_item
        draw.text((tx, y + 2), ellipsize(draw, t.title, font, avail),
                  font=font, fill=BLACK)

        # 整行都是热区，方便手指点，不只限于复选框
        hitmap["todos"].append({
            "uid": t.uid,
            "rect": [mx, y, 600 - mx, y + row_h - 2],
            "title": t.title,
        })

    if len(todos) > max_items:
        y = items_top + len(shown) * row_h
        draw.text((mx + 24, y + 2), f"还有 {len(todos) - max_items} 条",
                  font=f_due, fill=GRAY_MID)


def draw_footer(draw, fonts, cfg, now, hitmap):
    lay = cfg["layout"]
    mx = lay["margin_x"]
    top = lay["footer_top"]

    # 科目和教师名在页脚标一次即可 —— 教师视角下科目基本固定，
    # 在每个格子里重复它是浪费版面，班级才是要看的信息。
    #
    # 这里【刻意不画渲染时间戳】。画上去的话图片每次渲染都不一样，
    # 「内容没变就不重绘」的判断会永远为真：设备每 15 分钟做一次
    # 整屏刷新，输出仓库每 15 分钟多一个 commit —— 防抖动机制被
    # 一个时间戳完全架空。
    # 数据是否新鲜由设备端判断并显示（断了会标「离线 N 分钟」），
    # 那也比「云端何时渲染」更贴近你真正关心的问题。
    sch = cfg["schedule"]
    tags = [t for t in (sch.get("subject_label"), sch.get("teacher_label")) if t]
    if tags:
        draw.text((mx, top + 12), "  ·  ".join(tags),
                  font=fonts.get(14, bold=True), fill=BLACK)

    # 底部按钮。界面被冻结后触摸全屏无反应，只有点中这些矩形才有动作，
    # 所以它们必须画得足够显眼、够大、边界清楚。
    top_off = int(cfg["screen"].get("top_offset", 0))
    f_btn = fonts.get(15, bold=True)

    def button(key, label):
        x1, y1, x2, y2 = lay[key]
        draw.rectangle([x1, y1, x2, y2], outline=BLACK, width=2)
        tw = text_width(draw, label, f_btn)
        draw.text((x1 + (x2 - x1 - tw) // 2, y1 + (y2 - y1 - 18) // 2),
                  label, font=f_btn, fill=BLACK)
        return {"left": x1, "top": y1 + top_off, "right": x2, "bottom": y2 + top_off}

    # 图标符号要挑字体里确实有的 —— ↻ 在 Noto Sans SC 里缺失，会画成方框。
    # 「→」和「←」是基本箭头，覆盖率高得多。
    hitmap["update"] = button("update_rect", "↓ 更新")
    hitmap["exit"] = button("exit_rect", "← 返回")

    # 电量/离线提示区，由设备每分钟重绘 —— 冻结 cvm 后原生状态栏的电量
    # 也跟着凝固了，所以必须我们自己读 lipc 画。
    # 右边界要避开返回主页按钮，否则两者会叠在一起。
    top_off = int(cfg["screen"].get("top_offset", 0))
    W = cfg["screen"]["width"]
    PH = cfg["screen"]["height"] + top_off
    # 右边界要避开「更新」按钮，否则电量文字会和按钮叠在一起
    ux1 = lay["update_rect"][0]
    sx1, sy1 = 262, top + 6
    sx2, sy2 = ux1 - 10, top + 30
    hitmap["status"] = {
        "top": sy1 + top_off,
        "left": sx1,
        "right": W - sx2,
        "bottom": PH - (sy2 + top_off),
        "px": 16,
        "cls": {"top": sy1 + top_off, "left": sx1,
                "width": sx2 - sx1, "height": sy2 - sy1},
    }


# ---------------------------------------------------------------- 下发给设备


def _rect_vars(prefix: str, r: dict) -> list[str]:
    """一个区域导出两套坐标：-t 用边距，-k 用坐标+尺寸。"""
    return [
        f"{prefix}_TOP={r['top']}",
        f"{prefix}_LEFT={r['left']}",
        f"{prefix}_RIGHT={r['right']}",
        f"{prefix}_BOTTOM={r['bottom']}",
        f"{prefix}_PX={r['px']}",
        f"{prefix}_CLS_TOP={r['cls']['top']}",
        f"{prefix}_CLS_LEFT={r['cls']['left']}",
        f"{prefix}_CLS_W={r['cls']['width']}",
        f"{prefix}_CLS_H={r['cls']['height']}",
    ]


def write_device_conf(out_dir: Path, hitmap: dict, cfg: dict, week: dict) -> None:
    """产出扁平的 shell 变量文件，设备端 `. device.conf` 直接引入。

    设备端不解析 JSON —— busybox 没有 jq，用 sed 抠嵌套结构既脆弱又难维护。
    课表也一并下发成「分钟数」形式的时间轴，设备只需做整数比较，
    不必在 shell 里解析 HH:MM。
    """
    # 字体指纹：设备靠它判断字体要不要重新下载。字体 60 KB 而且极少变化，
    # 每次同步都传一遍是浪费。
    font_sha = ""
    font_file = out_dir / "device-font.ttf"
    if font_file.exists():
        import hashlib
        font_sha = hashlib.md5(font_file.read_bytes()).hexdigest()

    lines = [
        "# 由 render.py 自动生成，勿手工编辑",
        f"TOP_OFFSET={hitmap['top_offset']}",
        f"TZ_POSIX={hitmap['tz_posix']}",
        f"GENERATED_AT='{hitmap['generated_at']}'",
        f"FONT_SHA={font_sha}",
        "",
        "# 时钟区",
        *_rect_vars("CLOCK", hitmap["clock"]),
        "",
        "# 当前/下一节课（设备每分钟重算）",
        *_rect_vars("NEXT", hitmap["next"]),
        "",
        "# 状态区（离线提示 / 电量）",
        *_rect_vars("STATUS", hitmap["status"]),
        "",
        "# 按钮命中矩形（屏幕坐标）。冻结界面后只有点中它们才有动作。",
        f"EXIT_X1={hitmap['exit']['left']}",
        f"EXIT_Y1={hitmap['exit']['top']}",
        f"EXIT_X2={hitmap['exit']['right']}",
        f"EXIT_Y2={hitmap['exit']['bottom']}",
        f"UPD_X1={hitmap['update']['left']}",
        f"UPD_Y1={hitmap['update']['top']}",
        f"UPD_X2={hitmap['update']['right']}",
        f"UPD_Y2={hitmap['update']['bottom']}",
        "",
        "# 设备端要绘制的文案。与字体子集同源生成 —— 设备只有子集字体，",
        "# 画不了子集之外的字，所以文案绝不能写死在 shell 里。",
        *[f"{k}='{v}'" for k, v in DEVICE_MESSAGES.items()],
        "",
        "# 每天的课表时间轴：起始分钟,结束分钟,科目 用分号分隔。",
        "# 设备端取 SCHED_$(date +%u)，与当前分钟数比较即可定位当前/下一节。",
    ]

    by_idx = {int(p["idx"]): p for p in cfg["schedule"]["periods"]}
    for wd in sorted(week.keys()):
        items = []
        for c in sorted(week[wd], key=lambda x: x.first):
            if c.cancelled:
                continue          # 停掉的课不进设备端的时间轴
            p_start = by_idx.get(c.first)
            p_end = by_idx.get(max(c.periods))
            if not p_start or not p_end:
                continue
            s = sources._hhmm_to_minutes(p_start["start"])
            e = sources._hhmm_to_minutes(p_end["end"])
            # 科目名里不能出现分隔符，替换掉以免破坏格式
            name = c.name.replace(";", "").replace(",", "")
            items.append(f"{s},{e},{name}")
        lines.append(f"SCHED_{wd}='" + ";".join(items) + "'")

    lines.append("")
    (out_dir / "device.conf").write_text(
        "\n".join(lines), encoding="utf-8", newline="\n"
    )


# 设备端会绘制的所有文案，统一在这里定义并随 device.conf 下发。
#
# 为什么要集中管理：设备只有字体子集，画不了子集之外的字。如果文案写死在
# shell 脚本里、字符集写在这里，两处迟早分叉 —— 已经踩过一次（漏了拉丁
# 字母，"rebooting ..." 整行变成方框加叉）。
# 现在文案和字符集同源，设备脚本只用 $MSG_* 变量，不可能再对不上。
DEVICE_MESSAGES = {
    "MSG_CURRENT": "当前",
    "MSG_NEXT": "下一节",
    "MSG_NO_CLASS": "今天没有课",
    "MSG_AFTER_SCHOOL": "今天已放学",
    "MSG_REBOOT": "正在重启，请稍候",
    "MSG_OFFLINE": "离线",
    "MSG_MINUTES": "分钟",
    "MSG_BATTERY": "电量",
    "MSG_WAIT_USB": "已连接电脑",
    # 手动更新的各阶段提示。整个过程约一分钟，不给反馈的话
    # 用户会以为按钮没反应而反复点击。
    "MSG_UPD_START": "正在请求云端渲染",
    "MSG_UPD_WAIT": "等待新数据",
    "MSG_UPD_OK": "已更新",
    "MSG_UPD_NOCHANGE": "已是最新",
    "MSG_UPD_FAIL": "更新失败",
    "MSG_UPD_TIMEOUT": "更新超时，稍后自动重试",
}

# 除文案外还需要的字符：数字、标点、以及完整的拉丁字母
#（日志、调试信息、以及任何英文提示都可能用到，Latin 字形很小，全带上不亏）
DEVICE_FIXED_CHARS = (
    "0123456789"
    ":.,-/%()[]<>+= "
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


def build_device_font(out_dir: Path, cfg: dict, week: dict) -> None:
    """按实际课表动态生成设备端字体子集。

    设备要在本地绘制科目名，就必须有对应的汉字字形。完整 CJK 字体 17 MB，
    而实际用到的字符不过几十个 —— 按需子集化后只有几十 KB，
    拷贝和加载都快得多。字符集没变时跳过，避免每次渲染都重做。
    """
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    src = REPO_ROOT / (cfg.get("fonts") or {}).get("file", "")
    dst = out_dir / "device-font.ttf"
    stamp = out_dir / "device-font.charset"

    chars = set(DEVICE_FIXED_CHARS)
    for msg in DEVICE_MESSAGES.values():
        chars.update(msg)
    for courses in week.values():
        for c in courses:
            chars.update(c.name)
    charset = "".join(sorted(chars))

    if dst.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == charset:
        return      # 字符集未变，沿用上次的产物

    if not src.exists():
        print(f"警告: 找不到可变字体 {src}，跳过设备字体生成")
        return

    font = TTFont(str(src))
    # 必须先固化字重 —— fbink 不会去设置可变字体的字重轴，
    # 直接用会落到默认的 100(Thin)，大字号下细得看不清
    weight = int((cfg.get("fonts") or {}).get("weight_bold", 700))
    instancer.instantiateVariableFont(font, {"wght": weight}, inplace=True)

    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.notdef_outline = True
    opts.name_IDs = [1, 2, 4, 6]
    sub = subset.Subsetter(options=opts)
    sub.populate(text=charset)
    sub.subset(font)
    font.save(str(dst))

    stamp.write_text(charset, encoding="utf-8")
    print(f"设备字体  : {dst.name}  {dst.stat().st_size / 1024:.1f} KB  "
          f"({len(chars)} 字符)")


# ---------------------------------------------------------------- 主流程


def build(cfg, schedule_cfg, overrides_cfg, now, todos, weather, preview_clock=False):
    W = cfg["screen"]["width"]
    H = cfg["screen"]["height"]

    img = Image.new("L", (W, H), WHITE)
    draw = ImageDraw.Draw(img)
    fonts = Fonts(cfg)

    # POSIX TZ 字符串给设备端用。设备系统时区可能还停在别的地方（比如从悉尼
    # 带到国内），所以不依赖它，而是由渲染端下发时区，设备用 TZ 变量临时覆盖。
    # 注意 POSIX 的符号是反的：东八区要写成 UTC-8。
    off = now.utcoffset()
    total_min = int(off.total_seconds() // 60) if off else 0
    sign = "-" if total_min >= 0 else "+"
    oh, om = divmod(abs(total_min), 60)
    tz_posix = f"UTC{sign}{oh}" + (f":{om:02d}" if om else "")

    hitmap: dict = {
        "screen": {"width": W, "height": H},
        # 设备把整张图画在 y=top_offset 处，顶部让给原生状态栏。
        # 下面所有坐标都是内容区内部坐标，设备端需自行加上这个偏移。
        "top_offset": int(cfg["screen"].get("top_offset", 0)),
        "tz_posix": tz_posix,
        "generated_at": now.isoformat(),
    }

    today = now.date()
    today_wd = today.isoweekday()
    week = sources.week_courses(schedule_cfg, overrides_cfg, cfg, today)
    cur_period = sources.current_period(cfg, now)
    next_period, next_course = sources.next_class(cfg, week.get(today_wd, []), now)

    draw_header(draw, fonts, cfg, now, weather, hitmap,
                next_period, next_course, preview_clock)
    grid_end = draw_week_grid(draw, fonts, cfg, week, today_wd, cur_period)
    draw_todos(draw, fonts, cfg, todos, today, hitmap,
               grid_end + cfg["layout"]["todo_gap"])
    draw_footer(draw, fonts, cfg, now, hitmap)

    return img, hitmap, fonts, week


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO_ROOT / "out"))
    ap.add_argument("--stub", action="store_true", help="用假待办数据，本地调版面用")
    ap.add_argument("--no-weather", action="store_true", help="跳过天气请求，加快本地迭代")
    ap.add_argument("--at", help='模拟时刻，格式 "YYYY-MM-DD HH:MM"')
    ap.add_argument("--preview-clock", action="store_true",
                    help="在时钟区域画占位时间，仅本地看构图用，CI 不要开")
    args = ap.parse_args()

    cfg = sources.load_yaml(str(REPO_ROOT / "data" / "config.yaml"))

    tz = ZoneInfo(cfg["locale"]["timezone"])
    now = (
        datetime.strptime(args.at, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if args.at
        else datetime.now(tz)
    )

    provider = (cfg.get("data_source") or {}).get("provider", "yaml")
    if args.stub:
        provider = "yaml"  # --stub 走本地文件 + 假待办，不碰任何网络凭据

    if provider == "lark":
        schedule_cfg, overrides_cfg, todos = sources.load_from_lark(cfg, tz)
        # 表格里填了时间的节次，覆盖 config.yaml 里的默认作息。
        # 没填的保持默认 —— 表格是权威，配置是兜底。
        pt = schedule_cfg.get("period_times") or {}
        if pt:
            for p in cfg["schedule"]["periods"]:
                t = pt.get(int(p["idx"]))
                if t:
                    p["start"], p["end"] = t["start"], t["end"]
            print(f"作息      : {len(pt)} 节的时间来自表格")
    else:
        schedule_cfg = sources.load_yaml(str(REPO_ROOT / "data" / "schedule.yaml"))
        overrides_cfg = sources.load_yaml(str(REPO_ROOT / "data" / "overrides.yaml"))
        todos = sources.stub_todos(now.date()) if args.stub else sources.load_todos(cfg)
    weather = (
        sources.Weather(ok=False, error="skipped")
        if args.no_weather
        else sources.fetch_weather(cfg)
    )

    img, hitmap, fonts, week = build(
        cfg, schedule_cfg, overrides_cfg, now, todos, weather, args.preview_clock
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "dash.png"
    # mode 已经是 L，保存即为 8 位灰度 PNG，与设备 Y8 帧缓冲一致
    img.save(png, optimize=True)
    # ensure_ascii=True：纯 ASCII 能彻底避开设备端的编码问题
    (out_dir / "hitmap.json").write_text(
        json.dumps(hitmap, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    # 字体要先于 device.conf 生成 —— 后者里要写字体的指纹
    build_device_font(out_dir, cfg, week)
    write_device_conf(out_dir, hitmap, cfg, week)

    print(f"数据源    : {provider}")
    print(f"字体      : {fonts.source}")
    print(f"渲染时刻  : {now:%Y-%m-%d %H:%M %Z}")
    print(f"本周课程  : " + " ".join(
        f"{cfg['locale']['weekday_names'][wd-1]}={len(cs)}" for wd, cs in sorted(week.items())))
    print(f"待办      : {len(todos)} 条")
    print(f"天气      : {'ok' if weather.ok else weather.error}")
    print(f"输出      : {png}  ({png.stat().st_size} B, mode={img.mode})")


if __name__ == "__main__":
    main()
