"""Lark / 飞书多维表格（Bitable）数据源。

Lark 与飞书是两套独立租户，数据不互通，但 API 完全一致 —— 只有域名不同：
    Lark  https://open.larksuite.com
    飞书  https://open.feishu.cn
所以 base_url 做成配置项，切换只需改一行，代码无需改动。

凭据从环境变量读取（CI 里放 GitHub Secrets）：
    LARK_APP_ID
    LARK_APP_SECRET
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import requests

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
RECORDS_PATH = "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

# 单选「星期」列的取值 -> isoweekday
WEEKDAY_MAP = {
    "周一": 1, "周二": 2, "周三": 3, "周四": 4, "周五": 5, "周六": 6, "周日": 7,
    "星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4,
    "星期五": 5, "星期六": 6, "星期日": 7, "星期天": 7,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
}


class LarkError(RuntimeError):
    pass


def plain(value) -> str:
    """把 Bitable 各种字段表示统一成朴素字符串。

    同一个「文本」列，API 可能返回裸字符串，也可能返回富文本片段数组
    [{"type":"text","text":"..."}]；多选返回字符串数组。这里统一拍平，
    免得调用方到处做类型判断。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, (int, float)):
        # 整数别显示成 8.0
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            if key in value:
                return plain(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(p for p in (plain(v) for v in value) if p).strip()
    return str(value).strip()


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return plain(value).lower() in ("true", "1", "yes", "是", "✓")


def as_date(value, tz) -> date | None:
    """日期列返回的是毫秒级 Unix 时间戳；也兼容手填的 YYYY-MM-DD 文本。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(tz).date()
    s = plain(value)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def as_weekday(value) -> int | None:
    """星期列既支持单选（周一…）也支持直接填数字 1-7。"""
    if isinstance(value, (int, float)) and 1 <= int(value) <= 7:
        return int(value)
    s = plain(value).strip().lower()
    if not s:
        return None
    if s.isdigit() and 1 <= int(s) <= 7:
        return int(s)
    for key, wd in WEEKDAY_MAP.items():
        if key in s:
            return wd
    return None


def as_periods(value) -> list[int]:
    """节次列。容忍多种写法：`1,2` / `1-2` / `1 2` / `第1-2节` / 单个数字。"""
    s = plain(value)
    if not s:
        return []
    # 先把常见分隔符统一成逗号，再抽出所有数字
    s = s.replace("，", ",").replace("、", ",").replace(" ", ",")
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        # 处理 1-2 / 1~2 这类范围
        rng = part.replace("~", "-").replace("—", "-")
        if "-" in rng:
            a, _, b = rng.partition("-")
            a = "".join(ch for ch in a if ch.isdigit())
            b = "".join(ch for ch in b if ch.isdigit())
            if a and b:
                out.extend(range(int(a), int(b) + 1))
                continue
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            out.append(int(digits))
    return sorted(set(out))


def as_time_range(value) -> tuple[str, str] | None:
    """解析「08:00-08:40」这类时间段。

    容忍各种手滑写法：全角破折号、波浪线、「至」、以及 as_hhmm 能吃下的
    全部时间格式（8:00 / 08：00 / 0800）。解析不出来就返回 None，
    调用方会回落到配置里的默认作息，而不是让整次渲染失败。
    """
    s = plain(value)
    for sep in ("－", "—", "–", "~", "～", "至", "到"):
        s = s.replace(sep, "-")
    if "-" not in s:
        return None
    a, _, b = s.partition("-")
    a, b = as_hhmm(a), as_hhmm(b)
    if a and b and ":" in a and ":" in b:
        return (a, b)
    return None


def as_hhmm(value) -> str:
    """时间列统一成 HH:MM。容忍 8:00 / 08：00（全角冒号）/ 0800 这类手滑输入。"""
    s = plain(value).replace("：", ":").strip()
    if not s:
        return ""
    if ":" in s:
        h, _, m = s.partition(":")
    elif s.isdigit() and len(s) == 4:
        h, m = s[:2], s[2:]
    else:
        return s
    try:
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        return s


def parse_token(url_or_token: str) -> tuple[str, str]:
    """从链接里解析出 token 及其类型。

    多维表格有两种存放位置，链接形态不同：
        .../base/<app_token>?table=...   放在云空间，token 可直接用
        .../wiki/<node_token>?table=...  放在知识库，这是【节点】token，
                                         必须再换取底层的 app_token
    返回 (token, kind)，kind ∈ {"base", "wiki", "unknown"}。
    """
    s = url_or_token.strip()
    if "/base/" in s:
        return s.split("/base/", 1)[1].split("?", 1)[0].split("/", 1)[0], "base"
    if "/wiki/" in s:
        return s.split("/wiki/", 1)[1].split("?", 1)[0].split("/", 1)[0], "wiki"
    return s.split("?", 1)[0], "unknown"


class Lark:
    def __init__(self, base_url: str, app_id: str | None = None,
                 app_secret: str | None = None, timeout: int = 25):
        self.base = base_url.rstrip("/")
        self.app_id = app_id or os.environ.get("LARK_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("LARK_APP_SECRET", "")
        self.timeout = timeout
        self._token: str | None = None

        if not self.app_id or not self.app_secret:
            raise LarkError(
                "缺少 LARK_APP_ID / LARK_APP_SECRET。"
                "本地调试可以先用 --stub 跳过真实数据。"
            )

    # ------------------------------------------------------------ 鉴权

    def token(self) -> str:
        """tenant_access_token 有效期 2 小时，单次渲染进程内取一次就够。"""
        if self._token:
            return self._token
        r = requests.post(
            self.base + TOKEN_PATH,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise LarkError(f"取 token 失败: code={d.get('code')} msg={d.get('msg')}")
        self._token = d["tenant_access_token"]
        return self._token

    def wiki_obj_token(self, node_token: str) -> str:
        """把知识库节点 token 换成底层多维表格的 app_token。

        知识库里的表格，链接给出的是节点 token，而 Bitable 的 API 需要的是
        它底层文档的 token —— 两者不通用，直接拿节点 token 调 API 会报找不到。
        """
        r = requests.get(
            self.base + "/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {self.token()}"},
            params={"token": node_token, "obj_type": "wiki"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise LarkError(
                f"解析知识库节点失败: code={d.get('code')} msg={d.get('msg')}\n"
                "  多半是应用缺少知识库权限。两种解法：\n"
                "    A. 在权限管理里加 wiki:wiki:readonly 并重新发布\n"
                "    B. 把这个多维表格移到「我的空间」，链接会变成 /base/ 形式，"
                "就不需要这一步了"
            )
        node = (d.get("data") or {}).get("node") or {}
        obj_type = node.get("obj_type")
        if obj_type != "bitable":
            raise LarkError(f"该知识库节点不是多维表格，而是 {obj_type}")
        return node.get("obj_token", "")

    def resolve_app_token(self, url_or_token: str) -> str:
        """接受完整链接或裸 token，统一返回可直接用于 Bitable API 的 app_token。"""
        token, kind = parse_token(url_or_token)
        if kind == "wiki":
            real = self.wiki_obj_token(token)
            print(f"  知识库节点 {token} -> app_token {real}")
            return real
        return token

    # ------------------------------------------------------------ 读表

    def records(self, app_token: str, table_id: str,
                page_size: int = 200) -> list[dict]:
        """列出一张表的全部记录，自动翻页。返回每行的 fields 字典（含 record_id）。"""
        if not app_token or not table_id:
            return []

        url = self.base + RECORDS_PATH.format(app_token=app_token, table_id=table_id)
        headers = {"Authorization": f"Bearer {self.token()}"}
        out: list[dict] = []
        page_token = None

        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            r = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
            if d.get("code") != 0:
                raise LarkError(
                    f"读表失败 table={table_id}: code={d.get('code')} msg={d.get('msg')}"
                )

            data = d.get("data") or {}
            for item in data.get("items") or []:
                row = dict(item.get("fields") or {})
                row["_record_id"] = item.get("record_id")
                out.append(row)

            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break

        return out

    def fields(self, app_token: str, table_id: str) -> list[dict]:
        """列出一张表的字段定义。

        不能靠「从记录反推列名」——  Bitable 的读记录接口不返回空字段，
        所以一个建好但还没填过任何值的列（比如未勾选的复选框、空日期）
        会完全不出现，看起来就像不存在。必须查字段定义才准。
        """
        r = requests.get(
            self.base + f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers={"Authorization": f"Bearer {self.token()}"},
            params={"page_size": 100},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise LarkError(f"读取字段定义失败: code={d.get('code')} msg={d.get('msg')}")
        return (d.get("data") or {}).get("items") or []

    def add_field(self, app_token: str, table_id: str,
                  field_name: str, ftype: int = 1) -> str:
        """给已有的表补一列。用于后续演进时不必重建整张表。"""
        r = requests.post(
            self.base + f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers={"Authorization": f"Bearer {self.token()}"},
            json={"field_name": field_name, "type": ftype},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise LarkError(f"添加字段「{field_name}」失败: "
                            f"code={d.get('code')} msg={d.get('msg')}")
        return ((d.get("data") or {}).get("field") or {}).get("field_id", "")

    def update_records(self, app_token: str, table_id: str,
                       updates: list[dict]) -> int:
        """批量更新记录。updates 每项形如 {"record_id": ..., "fields": {...}}。"""
        if not updates:
            return 0
        url = (self.base
               + RECORDS_PATH.format(app_token=app_token, table_id=table_id)
               + "/batch_update")
        headers = {"Authorization": f"Bearer {self.token()}"}
        done = 0
        for i in range(0, len(updates), 500):
            chunk = updates[i:i + 500]
            r = requests.post(url, headers=headers,
                              json={"records": chunk}, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
            if d.get("code") != 0:
                raise LarkError(f"批量更新失败: code={d.get('code')} msg={d.get('msg')}")
            done += len(chunk)
        return done

    def delete_records(self, app_token: str, table_id: str,
                       record_ids: list[str]) -> int:
        """批量删除记录。用于自动清理过期的临时调课 ——
        一次性的调整不该要求人事后手动打扫。

        返回实际删除的条数。单次上限 500，超出会自动分批。
        """
        if not record_ids:
            return 0

        url = (self.base
               + RECORDS_PATH.format(app_token=app_token, table_id=table_id)
               + "/batch_delete")
        headers = {"Authorization": f"Bearer {self.token()}"}
        done = 0

        for i in range(0, len(record_ids), 500):
            chunk = record_ids[i:i + 500]
            r = requests.post(url, headers=headers,
                              json={"records": chunk}, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
            if d.get("code") != 0:
                raise LarkError(
                    f"删除记录失败: code={d.get('code')} msg={d.get('msg')}"
                )
            done += len(chunk)

        return done

    def set_checkbox(self, app_token: str, table_id: str,
                     record_id: str, field_name: str, value: bool) -> None:
        """勾选/取消某条记录的复选框。供「在 Kindle 上点击完成待办」使用。"""
        url = (self.base
               + RECORDS_PATH.format(app_token=app_token, table_id=table_id)
               + "/" + record_id)
        r = requests.put(
            url,
            headers={"Authorization": f"Bearer {self.token()}"},
            json={"fields": {field_name: value}},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise LarkError(f"更新记录失败: code={d.get('code')} msg={d.get('msg')}")
