# VJVision 发布说明

## v1.1.3-beta (2026-09-08)

### 混音切歌提速
- 识别间隔 `match_interval` 从 6s 缩短到 4s
- **混音（脉动）期间识别频率翻倍**：间隔再减半到 ~2s，更快捕捉新曲置信度爬升
- 混音确认阈值 `MIX_MIN_CONFIDENCE` 从 0.40 降到 0.30（长混音中新曲置信度常被稀释到 0.30–0.38，0.40 太严导致一直卡在 Mix hold）
- 实测：长混音从检测到确认由 ~28s 缩短到 ~15s

### 首歌识别
- 第一首歌确认阈值从 0.30 降到 0.25，减少启动等待时间

### 指纹库错误可见性
- 修复多进程 worker 的异常被静默吞掉的问题：worker 现在把异常字符串返回主进程，主进程以 `ERROR` 级别记录真实失败原因
- 用户之前只看到 "Failed: xxx.flac" 而不知原因（旧版 pydub 不支持 24-bit FLAC）

### 指纹重新分析
- 117 首歌全部用当前调优参数（wratio=0.25, fan=3, amp_min=15）重新生成指纹

---

## v1.1.2-beta (2026-09-08)

### 首歌误识别修复
- **第一首歌跳过 tentative 脉动预览**：在还没有任何确认歌曲时，置信度 0.06–0.30 的暂认匹配不再锁定显示，避免低置信度错误匹配（如 conf=0.07 的错误歌曲）触发脉动并挡住真正的歌曲
- 第一首歌必须等到置信度 ≥ 阈值才确认，确认后 tentative 机制照常工作（用于混音过渡检测）

---

## v1.1.1-beta (2026-09-08)

### 关闭控制台后视觉窗口残留修复
- **visualizer 子进程增加父进程存活检测**：每 ~0.5s 检查父进程是否存活，父进程被强制终止（如关闭控制台窗口）时子进程自动 `pygame.quit()` 退出，不再变成孤儿进程
- **Debug UI 绑定 `WM_DELETE_WINDOW`**：点击窗口 X 按钮走正常 quit 流程，确保 main.py 的 `finally` 块（含 `viz_mgr.stop()`）被执行

---

## v1.1.0-beta (2026-09-08)

### 项目重命名
- `VJ-Visual` → `VJVision`，包目录 `vjvisual` → `vjvision`，所有引用同步更新
- 配置路径：`LOG_FILE=vjvision.log`、`PREFS_DIR=VJVision`

### 混音脉动机制
- 新增 tentative zone（暂认区）：置信度 0.06–0.30 的不同歌曲信号触发"混音"状态，视觉窗口对当前歌曲做脉动效果，提示 DJ 正在切歌
- Mix hold：连续 2 次高置信度命中确认后才正式切换，避免误切

### 指纹管线优化
- dejavu 指纹参数调优（wratio 0.5→0.25, fan 5→3, amp_min 10→15, peak_neighborhood 10→20），哈希量减少 ~85–90%
- 解码改用 soundfile（libsndfile），原生支持 24-bit FLAC，去掉 pydub/ffmpeg 依赖
- 多进程 worker 复用 FingerprintDB 实例，消除每首歌的 dejavu 初始化开销

### Python 3.13+ 兼容
- 注入 numpy 版 `audioop` shim，解决 Python 3.14 移除 stdlib `audioop` 后 pydub 导入崩溃的问题
