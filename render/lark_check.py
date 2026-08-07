"""Lark 配置自检。

存在的意义：直接跑 render.py 出错时，报错往往很含糊（权限？表 ID？列名？），
而 Lark 这条链路上有五个独立的失败点。本工具逐个验证并给出明确结论，
把「不知道哪里错了」变成「就是这一处错了」。

    set LARK_APP_ID=cli_xxxxx
    set LARK_APP_SECRET=xxxxx
    python render/lark_check.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import lark      # noqa: E402
import sources   # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OK = "  [OK] "
NG = "  [!!] "


def main() -> None:
    cfg = sources.load_yaml(str(REPO_ROOT / "data" / "config.yaml"))
    lk = cfg.get("lark") or {}
    base_url = lk.get("base_url", "")
    app_token = lk.get("app_token", "")
    tables = lk.get("tables") or {}
    fields = lk.get("fields") or {}

    print("=" * 60)
    print("1. 检查凭据")
    print("=" * 60)
    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        print(NG + "环境变量 LARK_APP_ID / LARK_APP_SECRET 未设置")
        print("      PowerShell: $env:LARK_APP_ID='cli_xxx'")
        return
    print(OK + f"LARK_APP_ID = {app_id[:10]}…（Secret 已设置，不回显）")
    print(OK + f"base_url    = {base_url}")

    print()
    print("=" * 60)
    print("2. 换取 tenant_access_token")
    print("=" * 60)
    try:
        client = lark.Lark(base_url)
        token = client.token()
        print(OK + f"成功，token 前缀 {token[:12]}…")
    except Exception as e:
        print(NG + f"失败：{e}")
        print("      常见原因：")
        print("        · app_id / app_secret 抄错")
        print("        · base_url 与账号所在体系不符")
        print("          （Lark 用 open.larksuite.com，飞书用 open.feishu.cn，两者不通用）")
        return

    print()
    print("=" * 60)
    print("3. 检查 app_token（多维表格标识）")
    print("=" * 60)
    if not app_token:
        print(NG + "config.yaml 里 lark.app_token 还是空的")
        print("      从表格链接里取：https://xxx/base/<app_token>?table=<table_id>")
        return
    print(OK + f"app_token = {app_token}")

    print()
    print("=" * 60)
    print("4. 逐表读取并核对列名")
    print("=" * 60)
    print("   列名对不上是最常见的故障，而且报错很隐蔽 ——")
    print("   渲染器只会安静地跳过读不懂的行，屏幕上表现为「数据莫名其妙少了」。")
    print()

    any_fail = False
    for key, label in (("schedule", "课程表"), ("overrides", "调课"), ("todo", "待办")):
        tid = tables.get(key, "")
        print(f"--- {label}（tables.{key}）---")
        if not tid:
            print(NG + "table_id 为空，跳过")
            any_fail = True
            print()
            continue

        try:
            rows = client.records(app_token, tid)
        except Exception as e:
            print(NG + f"读取失败：{e}")
            msg = str(e)
            if "99991672" in msg or "permission" in msg.lower() or "forbidden" in msg.lower():
                print("      多半是【没把应用加进这个多维表格】——")
                print("      打开表格 → 右上角 ⋯ 更多 → 添加文档应用 → 选你的应用 → 可编辑")
                print("      光在权限页面勾选 bitable 是不够的，这一步最容易漏。")
            any_fail = True
            print()
            continue

        print(OK + f"读到 {len(rows)} 行")

        # 列名必须查字段定义，不能从记录反推 ——
        # 读记录接口不返回空字段，建好但没填过值的列（未勾选的复选框、
        # 空日期）会完全不出现，反推的话会误判成「列不存在」。
        try:
            defs = client.fields(app_token, tid)
            actual = {f.get("field_name", "") for f in defs}
            print(f"      表格实际列名: {sorted(actual)}")
        except Exception as e:
            print(NG + f"读取字段定义失败：{e}")
            any_fail = True
            print()
            continue

        # 把配置里的列名摊平。课程表的星期列是网格形态：
        # weekdays: {周一: 1, ...} —— 列名是【键】，值是星期编号。
        want = fields.get(key) or {}
        expect = {}
        for k, v in want.items():
            if isinstance(v, dict):
                for col in v:
                    expect[f"{k}.{col}"] = col
            else:
                expect[k] = v

        missing = [f"{k} -> 「{v}」" for k, v in expect.items() if v not in actual]
        if missing:
            print(NG + "以下配置的列名在表格里找不到：")
            for m in missing:
                print(f"        {m}")
            print("      改表格列名、或改 config.yaml 里的映射，二选一")
            any_fail = True
        else:
            print(OK + "列名映射全部匹配")

        print(f"      首行样例: { {k: v for k, v in list(rows[0].items())[:6]} }")
        print()

    print("=" * 60)
    if any_fail:
        print("结论：存在问题，按上面的提示逐条修")
    else:
        print("结论：Lark 侧全部就绪，可以把 config.yaml 的 provider 改成 lark 了")
    print("=" * 60)


if __name__ == "__main__":
    main()
