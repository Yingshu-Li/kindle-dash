"""数据获取层：课程表、待办、天气。

这里只负责把外部数据整理成干净的 Python 结构，不碰任何绘图逻辑。
render.py 只消费本模块的返回值，两者可以独立测试。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests
import yaml

# ---------------------------------------------------------------- 数据结构


@dataclass
class Course:
    """课表里的一格。periods 是它占的节次，连排会合并成一个格子。"""

    weekday: int          # 1=周一 .. 7=周日
    periods: list[int]    # 占用的节次号，例如 [1, 2]
    name: str
    room: str = ""
    note: str = ""
    # 被临时调整过的格子加个汉字角标：停 / 加 / 换。
    # 用汉字而不是抽象图形 —— 自解释，不需要图例，墨水屏上也更清晰。
    mark: str = ""
    # 停课不把格子清空，而是保留原班级并划掉 ——
    # 「今天不用去」和「本来是哪个班」都是有用信息，空格子会把后者丢掉。
    cancelled: bool = False

    @property
    def first(self) -> int:
        return min(self.periods)

    @property
    def span(self) -> int:
        return len(self.periods)


@dataclass
class Todo:
    # 待办的唯一标识。用字符串而非整数，因为不同数据源形态不同：
    # GitHub 是 issue 编号，Lark 是 record_id。设备端点击完成时原样回传。
    uid: str
    title: str
    labels: list[str] = field(default_factory=list)
    due: date | None = None
    priority: bool = False


@dataclass
class Weather:
    ok: bool
    location: str = ""
    temp_now: float | None = None
    temp_max: float | None = None
    temp_min: float | None = None
    description: str = ""
    precip_prob: int | None = None
    error: str = ""


# ---------------------------------------------------------------- 工具


def _hhmm_to_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------- 课程表


def _norm_periods(entry: dict) -> list[int]:
    """periods 支持写成 [1,2]，也兼容单节的 period: 3。"""
    if "periods" in entry and entry["periods"]:
        return sorted(int(p) for p in entry["periods"])
    if "period" in entry and entry["period"]:
        return [int(entry["period"])]
    return []


def _matches(course: Course, match: dict) -> bool:
    """override 的 match 块支持按 name / period 定位，都写则须同时满足。"""
    if not match:
        return False
    if "name" in match and course.name != match["name"]:
        return False
    if "period" in match and int(match["period"]) not in course.periods:
        return False
    return True


def _to_course(entry: dict, weekday: int) -> Course | None:
    periods = _norm_periods(entry)
    # 教师视角：格子里最需要看到的是「去哪个班」，科目往往是固定的。
    # 所以 class 优先于 name；两者都支持，便于学生视角复用同一套代码。
    name = (entry.get("class") or entry.get("name") or "").strip()
    if not periods or not name:
        return None      # 填一半的行直接跳过，而不是让整次渲染失败
    return Course(
        weekday=weekday,
        periods=periods,
        name=name,
        room=(entry.get("room") or "").strip(),
        note=(entry.get("note") or entry.get("subject") or "").strip(),
    )


def week_courses(
    schedule_cfg: dict, overrides_cfg: dict, cfg: dict, today: date
) -> dict[int, list[Course]]:
    """算出本周每一天实际要上的课，已套用落在本周内的调课。

    返回 {weekday: [Course]}。周一到周五各一个键，即使当天没课也有空列表。
    """
    weekdays = cfg["schedule"]["weekdays"]
    monday = today - timedelta(days=today.isoweekday() - 1)

    week: dict[int, list[Course]] = {wd: [] for wd in weekdays}
    for entry in schedule_cfg.get("courses") or []:
        wd = int(entry.get("weekday", 0))
        if wd in week:
            c = _to_course(entry, wd)
            if c:
                week[wd].append(c)

    # 套用调课：只处理日期落在本周显示范围内的
    for ov in overrides_cfg.get("overrides") or []:
        ov_date = ov.get("date")
        if isinstance(ov_date, str):
            try:
                ov_date = date.fromisoformat(ov_date[:10])
            except ValueError:
                continue
        if not isinstance(ov_date, date):
            continue

        wd = ov_date.isoweekday()
        if wd not in week:
            continue
        # 必须是本周的那一天，不能是别的周的同一星期几
        if monday + timedelta(days=wd - 1) != ov_date:
            continue

        action = ov.get("action")
        match = ov.get("match") or {}

        if action == "cancel":
            # 不删除，而是标记为停课并保留原班级 —— 屏幕上会划掉显示，
            # 这样「今天不用去」和「本来是哪个班」两条信息都在
            for c in week[wd]:
                if _matches(c, match):
                    c.cancelled = True
                    c.mark = "停"

        elif action == "add":
            c = _to_course(ov.get("course") or {}, wd)
            if c:
                c.mark = "加"
                week[wd].append(c)

        elif action == "move":
            to = ov.get("to") or {}
            for c in week[wd]:
                if _matches(c, match):
                    new_periods = _norm_periods(to)
                    if new_periods:
                        c.periods = new_periods
                    # class 是 name 的别名，与课表里的写法保持一致。
                    # 只认 name 的话，yaml 里写 class 会静默不生效 ——
                    # 角标出现了但班级没改，很难发现。
                    new_name = (to.get("class") or to.get("name") or "").strip()
                    if new_name:
                        c.name = new_name
                    for key in ("room", "note"):
                        if key in to and to[key]:
                            setattr(c, key, to[key])
                    c.mark = "换"

    for wd in week:
        week[wd].sort(key=lambda c: c.first)
    return week


def period_bounds(cfg: dict) -> list[dict]:
    return cfg["schedule"]["periods"]


def current_period(cfg: dict, now: datetime) -> int | None:
    """当前时刻正处于第几节；不在任何节次内返回 None。"""
    now_min = now.hour * 60 + now.minute
    for p in period_bounds(cfg):
        if _hhmm_to_minutes(p["start"]) <= now_min < _hhmm_to_minutes(p["end"]):
            return int(p["idx"])
    return None


def next_class(cfg: dict, today_courses: list[Course], now: datetime):
    """返回 (period_dict, Course)：正在上的优先，否则是接下来最近的一节。

    超出 next_lookahead_minutes 则返回 (None, None)，避免大早上就提示晚上的课。
    """
    now_min = now.hour * 60 + now.minute
    lookahead = cfg["schedule"].get("next_lookahead_minutes", 180)
    by_no = {int(p["idx"]): p for p in period_bounds(cfg)}

    best = None
    for c in today_courses:
        if c.cancelled:
            continue                         # 停掉的课不该提示「当前/下一节」
        p = by_no.get(c.first)
        p_end = by_no.get(max(c.periods))
        # 两端都要查在 —— 数据里若写了「1,9」而第 9 节根本不存在，
        # 直接下标访问会 KeyError 让整次渲染崩掉，屏幕就永远停在旧图上。
        # 宁可跳过这一节，也不能让一行脏数据打掉整张图。
        if not p or not p_end:
            continue
        start = _hhmm_to_minutes(p["start"])
        end = _hhmm_to_minutes(p_end["end"])
        if start <= now_min < end:
            return p, c                      # 正在上课，直接返回
        if start >= now_min and start - now_min <= lookahead:
            if best is None or start < best[0]:
                best = (start, p, c)

    return (best[1], best[2]) if best else (None, None)


# ---------------------------------------------------------------- 待办


_DUE_RE = re.compile(r"^\s*due:\s*(\d{4}-\d{2}-\d{2})\s*(.*)$", re.IGNORECASE)


def load_todos(cfg: dict, repo: str | None = None, token: str | None = None) -> list[Todo]:
    """从 GitHub Issues 拉取 open 的待办。

    CI 里 repo/token 由 GITHUB_REPOSITORY / GITHUB_TOKEN 提供。
    本地预览若没有 token，返回空列表并不报错 —— 用 --stub 可以喂假数据看版面。
    """
    todo_cfg = cfg.get("todo") or {}
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    token = token or os.environ.get("GITHUB_TOKEN")

    if not repo or not token:
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/issues",
        headers=headers,
        params={"state": "open", "per_page": 100, "sort": "created", "direction": "asc"},
        timeout=30,
    )
    resp.raise_for_status()

    priority_labels = set(todo_cfg.get("priority_labels") or [])
    todos: list[Todo] = []

    for item in resp.json():
        # Issues API 也会返回 PR，必须过滤掉
        if "pull_request" in item:
            continue

        title = item["title"]
        due = None
        m = _DUE_RE.match(title)
        if m:
            try:
                due = date.fromisoformat(m.group(1))
                title = m.group(2).strip() or title
            except ValueError:
                pass

        labels = [lb["name"] for lb in item.get("labels", [])]
        todos.append(
            Todo(
                uid=str(item["number"]),
                title=title,
                labels=labels,
                due=due,
                priority=bool(priority_labels & set(labels)),
            )
        )

    return sort_todos(todos)


def sort_todos(todos: list[Todo]) -> list[Todo]:
    """优先级在前，其次按截止日升序，无截止日的排最后。"""
    far_future = date.max
    return sorted(todos, key=lambda t: (not t.priority, t.due or far_future, t.title))


def stub_todos(today: date | None = None) -> list[Todo]:
    """本地调版面用的假数据，不参与 CI。

    条数刻意给多，用来检验待办区放满时的表现，以及超出容量后的省略提示。
    """
    today = today or date.today()
    return [
        Todo("1", "交电费", [], today, True),
        Todo("2", "回复家长群消息", [], today, True),
        Todo("3", "取快递", [], today + timedelta(days=1)),
        Todo("4", "体检报告还没去拿", [], today - timedelta(days=2)),
        Todo("5", "买水粉颜料", [], today + timedelta(days=1)),
        Todo("6", "周末爸妈家吃饭", [], today + timedelta(days=2)),
        Todo("7", "订下月机票", ["重要"], today + timedelta(days=3)),
        Todo("8", "健身卡续费", [], None),
        Todo("9", "整理照片备份", [], None),
        Todo("10", "换季衣物收纳", [], None),
    ]


# ---------------------------------------------------------------- Lark 数据源


def _lark_client(cfg):
    import lark  # 延迟导入：用 YAML 数据源时不该强制依赖它
    lk = cfg.get("lark") or {}
    return lark, lark.Lark(lk.get("base_url", "https://open.larksuite.com"))


def lark_app_token(cfg) -> str:
    """app_token 优先取环境变量。

    代码仓库是公开的，虽然单靠 app_token 读不到数据（还需要 app_secret，
    且应用必须被授权到那个 Base），但没必要把它摆出去。
    本地开发时配置里留着也能用，CI 里则由 Secret 注入。
    """
    return os.environ.get("LARK_APP_TOKEN") or (cfg.get("lark") or {}).get("app_token", "")


def load_from_lark(cfg, tz):
    """从多维表格读课表/调课/待办，产出与 YAML 路径完全相同的结构。

    这样 courses_for_date() 等下游逻辑不需要知道数据来自哪里。
    """
    lark, client = _lark_client(cfg)
    app_token = lark_app_token(cfg)
    lk = cfg["lark"]
    tables = lk.get("tables") or {}
    fields = lk.get("fields") or {}

    # ---- 课程表：网格形态 ----
    # 表格做成和屏幕一样的样子：一行一个节次，一列一个星期，格子里填班级。
    # 比「一条记录一节课」直观得多 —— 改课就是找到那一格改内容，
    # 不用在十几条散记录里翻找。
    fs = fields.get("schedule") or {}
    period_col = fs.get("period", "节次")
    wd_cols = fs.get("weekdays") or {
        "周一": 1, "周二": 2, "周三": 3, "周四": 4, "周五": 5
    }

    time_col = fs.get("time", "时间")
    courses = []
    period_times = {}
    for row in client.records(app_token, tables.get("schedule", "")):
        periods = lark.as_periods(row.get(period_col))
        if not periods:
            continue                      # 没填节次的行直接跳过

        # 作息时间也由表格管：手机上就能改，不必动代码仓库。
        # 解析不出来就不覆盖，渲染端会回落到 config.yaml 里的默认值。
        rng = lark.as_time_range(row.get(time_col))
        if rng:
            period_times[periods[0]] = {"start": rng[0], "end": rng[1]}

        for col, wd in wd_cols.items():
            cell = lark.plain(row.get(col))
            if not cell:
                continue                  # 空格子 = 这节没课
            courses.append({
                "weekday": int(wd),
                "periods": periods,
                "name": cell,
            })

    # ---- 临时调整 ----
    # 只需四个字段：日期 + 节次 + 操作 + 班级。星期由日期推出来，
    # 原班级由网格查出来，都不必人工重复填写。
    fo = fields.get("overrides") or {}
    action_map = {
        "停课": "cancel", "取消": "cancel", "cancel": "cancel",
        "加课": "add", "新增": "add", "add": "add",
        "换班": "move", "调课": "move", "调整": "move", "move": "move",
    }
    overrides = []
    expired_ids = []
    cleanup_days = int(lk.get("overrides_cleanup_days", 3))
    cutoff = date.today() - timedelta(days=cleanup_days)

    for row in client.records(app_token, tables.get("overrides", "")):
        d = lark.as_date(row.get(fo.get("date", "日期")), tz)

        # 过期行登记待删。一次性的调整不该要求人事后手动打扫 ——
        # 留几天缓冲是为了避开时区边界，也方便你回头查最近改过什么。
        if d and d < cutoff:
            rid = row.get("_record_id")
            if rid:
                expired_ids.append(rid)
            continue

        raw_action = lark.plain(row.get(fo.get("action", "操作")))
        action = action_map.get(raw_action.strip().lower())
        periods = lark.as_periods(row.get(fo.get("period", "节次")))
        klass = lark.plain(row.get(fo.get("class", "班级")))
        if not d or not action or not periods:
            continue

        entry = {"date": d.isoformat(), "action": action}
        if action == "cancel":
            # 按节次定位要停的那一节；填了班级则进一步限定
            entry["match"] = {"period": periods[0]}
            if klass:
                entry["match"]["name"] = klass
        elif action == "add":
            if not klass:
                continue                  # 加课必须写清是哪个班
            entry["course"] = {"periods": periods, "name": klass}
        elif action == "move":
            if not klass:
                continue                  # 换班必须写清换成哪个班
            entry["match"] = {"period": periods[0]}
            entry["to"] = {"name": klass}

        overrides.append(entry)

    if expired_ids:
        try:
            n = client.delete_records(app_token, tables.get("overrides", ""),
                                      expired_ids)
            print(f"已清理 {n} 条过期的临时调整（早于 {cutoff}）")
        except Exception as e:
            # 清理失败不该影响出图 —— 过期行本来就不参与渲染
            print(f"清理过期调整失败（不影响渲染）: {e}")

    # ---- 待办 ----
    ft = fields.get("todo") or {}
    todo_cfg = cfg.get("todo") or {}
    priority_labels = set(todo_cfg.get("priority_labels") or [])
    todos = []
    for row in client.records(app_token, tables.get("todo", "")):
        if lark.as_bool(row.get(ft.get("done", "完成"))):
            continue  # 已完成的不上屏
        title = lark.plain(row.get(ft.get("title", "事项")))
        if not title:
            continue
        tags = lark.plain(row.get(ft.get("tags", "标签")))
        todos.append(Todo(
            uid=row.get("_record_id", ""),
            title=title,
            labels=[t for t in tags.split() if t],
            due=lark.as_date(row.get(ft.get("due", "截止")), tz),
            priority=(lark.as_bool(row.get(ft.get("priority", "优先")))
                      or bool(priority_labels & set(tags.split()))),
        ))

    return ({"courses": courses, "period_times": period_times},
            {"overrides": overrides},
            sort_todos(todos))


# ---------------------------------------------------------------- 天气


# WMO weather code -> 中文短描述。只覆盖常见码，其余归入「未知」。
_WMO = {
    0: "晴", 1: "多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "暴雨",
    85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷暴冰雹", 99: "雷暴冰雹",
}


def fetch_weather(cfg: dict) -> Weather:
    """Open-Meteo，免 API key。失败时返回 ok=False，由渲染端决定怎么显示。"""
    w = cfg.get("weather") or {}
    if not w.get("enabled", True):
        return Weather(ok=False, error="disabled")

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": w["latitude"],
                "longitude": w["longitude"],
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "timezone": (cfg.get("locale") or {}).get("timezone", "Asia/Shanghai"),
                "forecast_days": 1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        d = resp.json()

        cur = d.get("current") or {}
        daily = d.get("daily") or {}

        def first(key):
            v = daily.get(key)
            return v[0] if isinstance(v, list) and v else None

        code = cur.get("weather_code")
        if code is None:
            code = first("weather_code")

        return Weather(
            ok=True,
            location=w.get("location_name", ""),
            temp_now=cur.get("temperature_2m"),
            temp_max=first("temperature_2m_max"),
            temp_min=first("temperature_2m_min"),
            precip_prob=first("precipitation_probability_max"),
            description=_WMO.get(code, "未知"),
        )
    except Exception as e:  # 网络/解析任何环节出错都不该让整张图渲染失败
        return Weather(ok=False, location=w.get("location_name", ""), error=str(e))
