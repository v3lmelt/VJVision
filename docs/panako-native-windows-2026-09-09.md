# Panako Windows 原生运行与跨平台适配

已在本机直接通过 Windows Java、JGaborator DLL 和 LMDB 运行 Panako，识别调用不启动 WSL、Docker 或 Linux 子进程。Java 识别核心和 PCM 通信协议保持共用，各平台提供对应 JNI 库。这条路线已经在 Windows x64 验证，macOS、Linux 的本次原生构建脚本尚未在对应机器执行。

本次提供独立构建工具、原生客户端和验证脚本，生产应用仍使用 Dejavu，尚未接入主界面的引擎选择或生成新版安装包。

## 解决的兼容问题

| 问题 | 实际处理 |
| --- | --- |
| 官方旧 JAR 缺少 Windows JGaborator DLL | 使用 Zig 和 Windows JDK JNI 头文件编译 x64 DLL |
| 算法版本不同可能影响对照 | 使用 JGaborator 0.6 / Gaborator 1.2，与此前 JAR 内 Linux 核心版本对应 |
| 默认 1 TB LMDB 映射在本机 Windows 打开失败 | 调整为默认 256 MiB 的可配置映射上限 |
| 原生删除读取了错误队列 | 将 `processDeleteQueue()` 内三个 `storeQueue` 引用改为 `deleteQueue` |
| Windows 中文数据库路径打不开 | 显式设置 JVM `-Dfile.encoding=UTF-8` |
| 外部解码命令的系统差异 | 查询和建库通过内存 PCM 输入，不启动解码子进程 |

映射容量可通过 `NativePanakoWorker(..., map_bytes=...)` 指定，底层属性为 `vjvision.panako.map.bytes`。当前没有自动扩容，较大曲库需要预先设置合适容量。256 MiB 是数据库映射上限，不代表 JVM 内存使用；JVM 最大堆另设为 2 GiB，未测峰值内存。

存储适配类从固定版本上游源码生成，仅修改映射容量和删除队列，查询方法保持不变。编译结果放在 classpath 中原 JAR 之前，原始 JAR 不作修改，外部源码的许可证头保留。构建脚本遇到不同的上游代码结构会拒绝套用补丁。

## 构建版本

- Windows x64，AMD Ryzen 7 5800H。
- 本机 Windows JDK 11.0.12，Python 3.11.15，Zig 0.16.0。
- Panako 官方 2.1 JAR，SHA-256：`767cdd2cd0991658c4a25a0b8e887f9a2a38f69ae17781b02fe1652e1a7173d4`。
- Panako 存储源码提交：`e4b0e1dbb55e340bc66c90bac0ceb82b2cf84211`。
- JGaborator 源码提交：`95e4b64826cc478e1f55cd08ac079d4fea6762df`，内含 Gaborator 1.2。
- Windows DLL SHA-256：`7aa81fffd7971c70d194929f5553876ac8885fdcf3b9eb8a19800cd4d961b5df`，大小 619,008 字节。
- 存储适配 class SHA-256：`57bdc15051d6eeb7902be47cd861f6f4ffcf0e8041245d87c04537d1bc84f735`。

[JGaborator 上游](https://github.com/JorenSix/JGaborator)提供了 Zig 构建 Windows JNI 库的方法。本次先构建了最新的 1.7 核心确认编译通路，随后切回与旧 Panako 对应的 1.2；以下识别数据全部来自 1.2 DLL。

## 本机复现

固定版本源码已在本机缓存。编译和运行均在 PowerShell 中完成：

```powershell
.\.venv\Scripts\python.exe tools/build_panako_native.py --source cache/engine-jgaborator-06 --zig cache/zig-runtime/ziglang/zig.exe --jdk 'C:/Program Files/Java/jdk-11.0.12' --output cache/panako-native-06
.\.venv\Scripts\python.exe tools/build_panako_worker.py --source cache/engine-panako --jar cache/engine-panako/Panako-2.1-all.jar --javac 'C:/Program Files/Java/jdk-11.0.12/bin/javac.exe' --output cache/panako-native-worker
.\.venv\Scripts\python.exe tools/benchmark_panako_native.py --java 'C:/Program Files/Java/jdk-11.0.12/bin/java.exe' --classes cache/panako-native-worker --libraries cache/panako-native-06
```

`build_panako_native.py` 使用 Windows JNI 头文件，并开启 GNU C++11 与数学常量支持。`build_panako_worker.py` 编译通用 PCM 服务和存储补丁。`NativePanakoWorker` 直接启动指定的本机 Java，可为不同机器指定各自的 Java、类文件、原生库和数据目录。

Python 原生客户端复用现有管道收发代码；传入的是本机 Java 命令，WSL 分支不执行。操作系统通过各自的 classpath 分隔符和原生库命名加载依赖。源代码中的解码器入口已被内存实现覆盖，不执行 shell。

验证脚本读取上一轮已完成的 `cache/panako-application.json` 作为场景清单，只复用歌曲哈希、设置和场景定义，不连接 WSL。它重新计算所有识别结果和时间。新结果保存在 `cache/panako-native.json`，日志为 `cache/panako-native.log`。

## 固定片段一致性与计算成本

6 首真实歌曲，每首取第 30、70、110 秒的 2/4/6/12 秒片段，共 72 个窗口。Windows 原生版与此前 WSL 版的歌曲 ID 和接受判定全部一致，为 72/72。这不代表浮点系数、指纹或每个匹配分数逐位相同。

| 输入长度 | Windows 原生完整处理耗时中位 | 接受正确数 |
| --- | ---: | ---: |
| 2 秒 | 26.81 ms | 0/18 |
| 4 秒 | 63.54 ms | 0/18 |
| 6 秒 | 112.93 ms | 18/18 |
| 12 秒 | 253.64 ms | 18/18 |

从 48 kHz 双声道 PCM 开始计时，包含转换、管道通信、Java 查询与结果转换，排除文件解码、建库、JVM 初始化和音频采集。此前 WSL 版 6 秒窗口中位约 86 ms；运行批次、编译器和运行时不同，本轮没有同时重跑 WSL，不能据此作严格的跨系统速度排名。当前结果不支持宣称 Windows 原生版计算更快。

## 连续场景

采用与上一轮相同的 108 个场景、真实 `MatcherThread.run()`、36 秒观察时长以及实际计算耗时推进的模拟时钟。生产源码哈希和配置在运行前校验。接受条件、时间覆盖率含义及六首曲库限制沿用 [Panako 对照报告](panako-application-benchmark-2026-09-09.md)。

| 场景 | 成功数 | 确认中位 / P95 |
| --- | ---: | ---: |
| 原速直接切歌 | 18/18 | 7.46 / 11.38 秒 |
| 8 秒交叉淡化 | 18/18 | 8.16 / 10.83 秒 |
| 首歌开始 | 18/18 | 5.21 / 6.32 秒 |
| 加速 6%，音高随之变化 | 6/6 | 7.75 / 14.67 秒 |
| 保留音高加速 6% | 6/6 | 7.11 / 13.61 秒 |
| EQ 带通 | 6/6 | 6.45 / 10.87 秒 |

直接切歌延迟从切歌动作开始计时；淡化延迟从两首歌的等增益点计算，从淡化开始计时需加 4 秒。首歌从音频开始计算。无音频设备、封面读取和实际画面输出，不代表硬件到显示的总延迟。

最终 108 个场景全部符合预期，没有错误歌曲切换、提前切换或确认后回退。过渡撤回 18/18 保持原歌，静音与噪声 6/6 无误认，未入库直接输入及切入场景各 6/6 符合预期。所有切换事件均在观察区间内。这些来自六首歌的结果不代表大曲库误报率为零。

六首歌曲逐首使用修复后的真实 LMDB 删除操作，并关闭、重启 Java 进程。每次重启后被删除目标均不再返回，重新入库后恢复识别；没有使用候选排除集合或重建库替代删除。完整曲库重启前后的探针查询也一致。

另在中文数据目录完成真实歌曲的建库、查询、重启再查询和删除检查，全部通过。该检查使用最终启用 UTF-8 和显式映射容量的原生客户端，结果记录在 `cache/panako-native-unicode-result.json`。

完整 56 项单元测试通过，Python 编译检查通过；固定片段判定、108 次循环的事件完整性、六首删除记录与生产源码哈希检查通过。验证结束后工作进程已关闭。

## 正式交付边界

Windows 原生识别通路已可用于进一步集成。面向用户发布时应按操作系统和 CPU 架构打包 JNI 库，并随应用提供经过验证的 Java 运行时，避免要求用户自行配置 Java 路径。尚未生成这种完整安装包。

跨平台共用的是 Java 核心、PCM 协议和适配代码；Windows DLL、Linux SO、macOS dylib 需要分别构建验证。此次 Windows x64 实测不能替代 macOS 或 ARM64 测试。较大曲库扩容、异常恢复、实体采集设备和主应用引擎选择也仍需集成验证。
