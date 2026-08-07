"""从可变字体生成设备端专用的静态粗体子集。

为什么需要这一步：
  1. fbink 的 OpenType 渲染不会去设置可变字体的字重轴，会落到默认值
     100(Thin)，70px 的大时钟会细得几乎看不见。必须先固化成静态实例。
  2. 设备只需要画时钟和几行状态文字，用不到两万多个汉字。子集化之后
     从 17 MB 降到几十 KB，拷贝和加载都快得多。

产物 assets/fonts/device-clock.ttf 需要提交进仓库，部署时随扩展拷到设备。
改动字符集后重新跑一次即可。

    python render/build_device_font.py
"""

from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "assets" / "fonts" / "NotoSansSC-VF.ttf"
DST = REPO_ROOT / "assets" / "fonts" / "device-clock.ttf"

WEIGHT = 700

# 设备端会用到的字符：时钟数字、ASCII、以及少量状态提示汉字
CHARS = (
    "0123456789:./-+%"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    " "
    "离线分钟秒更新电量已连接失败等待中无网络"
)


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"找不到源字体: {SRC}")

    font = TTFont(str(SRC))
    instancer.instantiateVariableFont(font, {"wght": WEIGHT}, inplace=True)

    options = subset.Options()
    options.layout_features = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = True
    # 保留 name 表里的字体名，方便在设备上排查是不是加载对了文件
    options.name_IDs = [1, 2, 4, 6]

    subsetter = subset.Subsetter(options=options)
    subsetter.populate(text="".join(sorted(set(CHARS))))
    subsetter.subset(font)

    DST.parent.mkdir(parents=True, exist_ok=True)
    font.save(str(DST))

    print(f"源字体   : {SRC.name}  {SRC.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"字重固化 : wght={WEIGHT}")
    print(f"字符数   : {len(set(CHARS))}")
    print(f"产物     : {DST}  {DST.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
