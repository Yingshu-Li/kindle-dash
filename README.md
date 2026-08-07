# Kindle Dashboard

把一台越狱的 Kindle Basic 2014（KT2，600×800 墨水屏）变成桌面 dashboard：
今日课程、待办、天气、时间。

## 日常怎么用

| 我想做什么 | 怎么做 |
|---|---|
| 加一条待办 | 在本仓库新建一个 Issue（手机 GitHub App 即可） |
| 完成一条待办 | 关闭对应 Issue |
| 待办排在最前面 | 给 Issue 打 `urgent` 或 `重要` 标签 |
| 给待办加截止日 | 标题写成 `due:2026-08-20 交作业` |
| 改常规课表 | 编辑 `data/schedule.yaml` |
| 临时调课/停课/加课 | 编辑 `data/overrides.yaml`，只影响指定日期 |
| 换城市天气 | 编辑 `data/config.yaml` 的经纬度 |

改完提交即可。关闭 Issue 会立刻触发重新渲染；其余改动最迟半小时内生效。

## 它是怎么跑的

```
data/*.yaml + Issues + Open-Meteo
        ↓  GitHub Actions（每 30 分钟 / Issue 变动 / push）
   render.py → dash.png (600×800, 8bit 灰度) + hitmap.json
        ↓  强推到 render-output 孤儿分支（永远单个 commit）
   Kindle 通过 GitHub Contents API 拉取并用 fbink 画到屏幕上
```

`hitmap.json` 描述了时钟区域、状态区域和每条待办的屏幕坐标，设备端所有
坐标都从它读取 —— 调整版面不需要同步修改设备上的脚本。

## 本地预览

```bash
pip install -r render/requirements.txt

python render/render.py --stub --no-weather --preview-clock
python render/render.py --at "2026-08-03 09:15" --stub --preview-clock
```

- `--stub` 用假待办，不需要 token
- `--no-weather` 跳过天气请求，迭代更快
- `--preview-clock` 在时钟区画占位时间看构图；**CI 绝不能开**，否则会和
  设备端每分钟重绘的时间叠在一起
- `--at` 模拟某个时刻，用来验证「当前/下一节课」的高亮逻辑

产物在 `out/`，该目录不进版本库。

## 注意事项

- **Actions 额度**：私有仓库 Free 计划每月 2000 分钟，且每次任务按整分钟
  向上取整。当前 30 分钟一次约用 1140 分钟/月。若改成 15 分钟一次会直接
  超额。想要更高频率，可以考虑把仓库改为公开（公开仓库 Actions 免费无限），
  但那样待办和课表内容就公开了。
- **定时任务休眠**：仓库连续 60 天无活动后 GitHub 会自动停用 schedule
  触发器。日常增删 Issue 就算活动，正常使用不会遇到；真被停用了，在
  Actions 页面手动 `workflow_dispatch` 一次即可恢复。
- **字体随仓库分发**（`assets/fonts/`），不在 CI 里临时安装 —— 这样本地
  预览和线上产出才是像素级一致的，调版面时不会两边对不上。
