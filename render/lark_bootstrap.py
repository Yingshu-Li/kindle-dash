"""在已有的多维表格里自动建好三张表并填入初始数据。

为什么不连 Base 一起建：通过 API 创建的表格归应用所有，未必出现在你自己的
云文档里，手机上就编辑不了。所以 Base 由你手动新建并授权给应用，
建列和填数据这些繁琐活交给脚本。

凭据只从环境变量读取，不写进任何文件，也不需要告诉别人：

    $env:LARK_APP_ID='cli_xxxxx'
    $env:LARK_APP_SECRET='xxxxx'
    python render/lark_bootstrap.py <app_token>

幂等：同名的表已存在就跳过，不会重复创建或重复插入数据。
跑完会打印三个 table_id，直接填进 config.yaml 即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import lark  # noqa: E402

# Bitable 字段类型代码
T_TEXT = 1
T_SELECT = 3
T_DATE = 5
T_CHECKBOX = 7

# 课表原件上的 14 节课。行=节次，列=星期。
SCHEDULE_GRID = {
    1: {},
    2: {"周二": "一3班", "周三": "二6班", "周五": "二2班"},
    3: {"周五": "二8班"},
    4: {"周一": "四6班", "周二": "二2班", "周三": "一2班"},
    5: {"周一": "二8班", "周二": "一1班", "周三": "一3班",
        "周四": "一1班", "周五": "一2班"},
    6: {"周四": "四6班", "周五": "二6班"},
    7: {},
    8: {},
}

WEEKDAY_COLS = ["周一", "周二", "周三", "周四", "周五"]

SAMPLE_TODOS = [
    {"事项": "交电费", "优先": True},
    {"事项": "回复家长群消息"},
    {"事项": "买水粉颜料"},
]


class Bootstrap:
    def __init__(self, app_token: str):
        self.app_token = app_token
        self.client = lark.Lark("")   # base_url 稍后从配置覆盖
        self.base = ""

    # ------------------------------------------------------------ HTTP

    def _url(self, path: str) -> str:
        return f"{self.base}/open-apis/bitable/v1/apps/{self.app_token}{path}"

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(self._url(path), json=body, timeout=30,
                          headers={"Authorization": f"Bearer {self.client.token()}"})
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise lark.LarkError(f"{path} 失败: code={d.get('code')} msg={d.get('msg')}")
        return d.get("data") or {}

    def list_tables(self) -> dict[str, str]:
        r = requests.get(self._url("/tables"), timeout=30,
                         params={"page_size": 100},
                         headers={"Authorization": f"Bearer {self.client.token()}"})
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise lark.LarkError(f"列出数据表失败: code={d.get('code')} msg={d.get('msg')}")
        return {t["name"]: t["table_id"] for t in (d.get("data") or {}).get("items", [])}

    # ------------------------------------------------------------ 建表

    def ensure_table(self, name: str, fields: list[dict]) -> tuple[str, bool]:
        """返回 (table_id, 是否新建)。同名表已存在则直接复用。"""
        existing = self.list_tables()
        if name in existing:
            print(f"  表「{name}」已存在，跳过创建")
            return existing[name], False

        data = self._post("/tables", {
            "table": {"name": name, "default_view_name": "表格", "fields": fields}
        })
        tid = data.get("table_id", "")
        print(f"  已创建表「{name}」  table_id={tid}")
        return tid, True

    def ensure_fields(self, table_id: str, wanted: list[dict]) -> list[str]:
        """给已有的表补齐缺少的列。返回新加的列名。

        存在的意义：表结构会随需求演进（比如后来才想到把作息时间也放进表里），
        总不能每次都让人重建表、重填数据。
        """
        have = {f.get("field_name") for f in self.client.fields(self.app_token, table_id)}
        added = []
        for spec in wanted:
            name = spec["field_name"]
            if name in have:
                continue
            self.client.add_field(self.app_token, table_id, name,
                                  spec.get("type", T_TEXT))
            added.append(name)
        return added

    def add_records(self, table_id: str, records: list[dict]) -> int:
        if not records:
            return 0
        data = self._post(f"/tables/{table_id}/records/batch_create",
                          {"records": [{"fields": f} for f in records]})
        return len(data.get("records") or [])

    def count_records(self, table_id: str) -> int:
        return len(self.client.records(self.app_token, table_id))


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    app_token = sys.argv[1].strip()

    # 从 config.yaml 取 base_url，保证与渲染器用的是同一个体系（Lark / 飞书）
    import sources
    cfg = sources.load_yaml(str(Path(__file__).resolve().parent.parent
                                / "data" / "config.yaml"))
    base_url = (cfg.get("lark") or {}).get("base_url", "https://open.larksuite.com")

    bs = Bootstrap(app_token)
    bs.base = base_url.rstrip("/")
    bs.client.base = bs.base

    print(f"base_url  = {bs.base}")
    print(f"app_token = {app_token}")
    print()

    try:
        bs.client.token()
    except Exception as e:
        raise SystemExit(f"取 token 失败：{e}\n  检查 LARK_APP_ID / LARK_APP_SECRET，"
                         f"以及应用是否已【发布】。")

    print("=== 1. 课程表（网格）===")
    # 「时间」列让作息也归表格管 —— 原本写死在 config.yaml 里，
    # 改一次要动代码仓库，手机上根本改不了。
    sched_fields = [
        {"field_name": "节次", "type": T_TEXT},
        {"field_name": "时间", "type": T_TEXT},
    ]
    sched_fields += [{"field_name": c, "type": T_TEXT} for c in WEEKDAY_COLS]
    sched_id, created = bs.ensure_table("课程表", sched_fields)

    added = bs.ensure_fields(sched_id, sched_fields)
    if added:
        print(f"  补齐缺少的列: {added}")

    # 时间的初值取自 config.yaml 里现有的作息，之后以表格为准
    period_times = {int(p["idx"]): f'{p["start"]}-{p["end"]}'
                    for p in (cfg.get("schedule") or {}).get("periods", [])}

    if created or bs.count_records(sched_id) == 0:
        rows = []
        for p in sorted(SCHEDULE_GRID):
            row = {"节次": str(p), "时间": period_times.get(p, "")}
            for col in WEEKDAY_COLS:
                v = SCHEDULE_GRID[p].get(col)
                if v:
                    row[col] = v          # 空格子干脆不写，保持单元格为空
            rows.append(row)
        print(f"  写入 {bs.add_records(sched_id, rows)} 行")
    else:
        # 表已存在：只给「时间」还空着的行补上初值，不覆盖你改过的内容
        updates = []
        for row in bs.client.records(bs.app_token, sched_id):
            if lark.plain(row.get("时间")):
                continue
            p = lark.as_periods(row.get("节次"))
            if p and period_times.get(p[0]):
                updates.append({"record_id": row["_record_id"],
                                "fields": {"时间": period_times[p[0]]}})
        if updates:
            n = bs.client.update_records(bs.app_token, sched_id, updates)
            print(f"  为 {n} 行补上了默认时间（已填写的不动）")
        else:
            print("  已有数据，不覆盖")

    print()
    print("=== 2. 临时调整 ===")
    ov_fields = [
        {"field_name": "日期", "type": T_DATE},
        {"field_name": "节次", "type": T_TEXT},
        {"field_name": "操作", "type": T_SELECT,
         "property": {"options": [{"name": "停课"}, {"name": "加课"}, {"name": "换班"}]}},
        {"field_name": "班级", "type": T_TEXT},
        {"field_name": "备注", "type": T_TEXT},
    ]
    ov_id, _ = bs.ensure_table("临时调整", ov_fields)
    print("  留空 —— 需要调课时再加行，过期会自动清理")

    print()
    print("=== 3. 待办 ===")
    todo_fields = [
        {"field_name": "事项", "type": T_TEXT},
        {"field_name": "完成", "type": T_CHECKBOX},
        {"field_name": "截止", "type": T_DATE},
        {"field_name": "优先", "type": T_CHECKBOX},
        {"field_name": "标签", "type": T_TEXT},
    ]
    todo_id, created = bs.ensure_table("待办", todo_fields)
    if created or bs.count_records(todo_id) == 0:
        print(f"  写入 {bs.add_records(todo_id, SAMPLE_TODOS)} 条示例")
    else:
        print("  已有数据，不覆盖")

    print()
    print("=" * 60)
    print("把下面这段填进 data/config.yaml 的 lark 节：")
    print("=" * 60)
    print(f'  app_token: "{app_token}"')
    print("  tables:")
    print(f'    schedule: "{sched_id}"')
    print(f'    overrides: "{ov_id}"')
    print(f'    todo: "{todo_id}"')


if __name__ == "__main__":
    main()
