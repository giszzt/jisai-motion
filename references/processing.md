# 项目与命令

需要 Python 3.10+、Pillow 10+。视频需要 ffmpeg 和 ffprobe。命令中的 `<skill>`、`<project>` 替换为实际绝对路径；含空格路径加引号。清单 UTF-8，不把中文脚本通过 Windows 管道传给 Python。

## 新整表的通用入口

先确定单帧需要装下的完整运动范围，再选择单帧比例和最低像素。下例数字只是调用示例，不是主体或帧数默认值：

```text
python <skill>/scripts/layout.py plan --count 8 --cell 512 384 --out <project>/layout
python <skill>/scripts/layout.py check --sheet <project>/raw.png --layout <project>/layout/layout.json --out <project>/layout-check
python <skill>/scripts/motion.py split --sheet <project>/raw.png --layout <project>/layout/layout.json --out <project>/split
python <skill>/scripts/motion.py build --manifest <project>/split/motion.json --out <project>/export-v1
```

plan 自动选择较紧凑行列，也可 --cols 明确指定；允许尾部空格，不算动作帧。--margin/--gutter 指定像素外边距/格间距；--safe-fraction 为格内安全区的初始规划。--background scene 用于满幅连续背景，其边缘内容不自动视为跨帧；isolated 用于透明孤立主体/特效。键色图先抠成实际 alpha 再检查。

生成前查看 layout-guide.png 并作为构图参考引用，提示词声明红/黄线与编号仅用于位置指导、成图不画这些标记。该图只画布局，不代替动作内容。根据后端支持比例决定是否采用规划；工具不猜测后端能力。单帧宽高对应完整运动包络，不是只按静止主体外接框决定。

check 比较实际画布比例（1% 几何容差）、最低单元像素、缩放后格子等大性，以及孤立内容的 alpha 是否碰到裁剪边界。输出诊断图和 JSON，即使 blocked 也保留诊断，退出码 3；split --layout 再检查同一条件。geometry_passed 仍须逐帧看图，不能识别格内残片归属或被遮挡的内容。scene 的边界帧只供人工判断。显式改框必须有新的实图测量依据，不是避开失败检查的快捷方式。

split 直接生成 motion.json 与本地逐帧 preview.html，审核与时长可就地填写，随后用 build；不再需要手动从 grid.json 搬帧。多个任意动作单元各自重复该通路，合并源动作表与合并最终图集是不同决定。样板通过之后才复用其布局方案。

```text
python <skill>/scripts/motion.py doctor
python <skill>/scripts/motion.py init --out <project>/motion.json
python <skill>/scripts/motion.py split --sheet <project>/raw.png --cols 4 --rows 4 --out <project>/split
python <skill>/scripts/motion.py inspect --manifest <project>/motion.json
python <skill>/scripts/motion.py build --manifest <project>/motion.json --out <project>/export-v1
```

生图与抠图是另一套命令，只在没有内置图像工具时使用，见 [生图后端](imagegen-backend.md)：

```text
python <skill>/scripts/generate_frames.py precheck
python <skill>/scripts/generate_frames.py generate --prompt-file <project>/prompt.txt --out <project>/raw/sheet.png --record <project>/raw/generation-record.json --cells 4x4
python <skill>/scripts/generate_frames.py key --image <project>/raw/sheet.png --out <project>/raw/sheet-keyed.png --cells 4x4
```

`generate` 默认透明，返回的 PNG 已经抠成真 alpha，可以直接交给 split。校验不通过时它不写输出文件并返回 `keying_failed`，此时重生成，不要降阈值硬抠。`--opaque` 用于已确定的非透明背景。

init 生成待填写的清单。split 默认精确规则网格，支持 `--margin N`（四周外边距）和 `--gutter N`（格间距）；扣除后必须可整除。也可用 `--boxes boxes.json` 指定按时间排序的 `[left, top, right, bottom]` 数组，右/下坐标不包含在裁剪内。裁剪框必须等大、不越界、不重叠，不能靠缩小框裁掉主体。输出包括帧、grid.json 和 layout-overlay.png（红色裁剪边、黄色 8% 安全区，标记仅在检查图上）。

grid.json 的 frames 完整移入清单，保留 crop_provenance，路径改为相对 motion.json。查看原表、检查图和各帧，确认没有主体裁断、邻帧残片、附件遗漏后，逐帧设置 `crop_provenance.visual_layout_verified=true`。所有新切帧默认待审，包括没有 safe_area_risk 的帧；该检测不能证明语义归属。build/inspect 拒绝未审或像素哈希改变的帧。修复图片后重新检查并更新该帧哈希及审核状态，不能删除溯源字段。旧版无溯源清单保持可用，但仍须视觉验收。

`--duration-ms 120` 是 split 的默认临时时间，可明确覆盖；不会按帧数自动压缩到固定总时长。先规划各动作阶段，再编辑逐帧时长，可在帧上保留 `phase` 名称。高帧数平均停留不足 90 ms 会提示可读性风险，不自动改变快速运动。

只改时间：`python <skill>/scripts/motion.py retime --manifest <project>/motion.json --out <project>/motion-slow.json --factor 1.5`；也可用 `--total-ms 2400` 替代 factor。命令保持原有时长比例，按 10 ms 分配余数，保留图片及原清单；任何帧短于 20 ms 或总时长超限就拒绝。输出使用绝对帧路径，可放在不同目录。阶段停顿直接编辑对应帧 duration_ms，不复制姿态冒充新帧。每次 build 重新产生 pending 审核及绑定帧像素、时长、循环、GIF alpha 阈值的 review_binding；旧版审核不能当作新版验收。

## 清单

首次生图前记录 `background_policy` 和 `background_reason`，默认 `transparent` 且 `require_transparency=true`。用户明确指定非透明背景，或延续参考图的连续场景时，将策略记为 `user-specified` / `reference-scene` 并设 `require_transparency=false`。init 的透明初值需要按已判定的例外调整；导出 MP4 本身不是例外。

```json
{
  "schema_version": 1,
  "name": "sleepy-cat",
  "intent": "a sleepy cat nods then wakes",
  "style": "clay",
  "strategy": "sheet-repair",
  "provider": {"tool": "scripts/generate_frames.py", "model": "gpt-image-2.5-flare"},
  "references": [],
  "invariants": ["same face and costume", "fixed camera"],
  "motion_beats": ["settle", "nod", "wake", "return"],
  "loop": true,
  "require_transparency": true,
  "allow_empty_frames": false,
  "formats": ["gif", "sheet", "zip"],
  "sheet_columns": 4,
  "gif_alpha_threshold": 128,
  "video_background": "#ffffff",
  "audio": null,
  "frames": [
    {"path": "split/frame-0001.png", "duration_ms": 160, "origin": [128, 220], "events": []},
    {"path": "split/frame-0002.png", "duration_ms": 80, "origin": [128, 220], "events": []}
  ]
}
```

frames 顺序就是播放顺序，不用文件名排序推断。duration_ms 必须是 10 的整数倍，20–60000 ms；这使 GIF 和视频时基可表达同一时间。全动作最长 60 秒，最多 240 个逻辑帧，超出走更适合的工作流。可有意重复路径或画面表达停顿，报告会提示重复。

输入帧必须为 PNG、尺寸完全一致。脚本不重缩放、不消漂移、不生成或编辑角色。透明请求会检查每帧有透明区，空帧默认拒绝。frames 可携带 origin、events 等字段，原样保留，不自动猜测正确性。引用素材和 audio 可以用绝对路径或相对清单路径。

## 输出语义

- gif：统一 255 色可见调色板 + 一个透明索引，明确背景索引，disposal=2，不做差帧优化；按阈值转换为二值透明，RGBA 母版不变。实际编码后重新解码验时长。
- apng / webp：保留连续 alpha 的动画替代物；是否支持以目标播放器为准。
- sheet：按 sheet_columns 横向优先打包。非整除时末尾空单元不算动作帧，report 中的逻辑帧数为准。
- zip：含母版帧、可重新 build 的清单及可选音频。清单的参考来源属于溯源记录，原参考图不自动放进 ZIP。
- mp4：普通 H.264 不透明视频；在 video_background 上合成。使用 100 Hz 时间网格编码以保存 10 ms 精度，重复输出帧不会增加真实姿态数量。可选 audio 只对 MP4 生效；时长按动作裁切/补静音。循环意图保存在清单，MP4 自身不设置无限循环。

build 始终输出规范化 motion.json、PNG 母版、report.json、review.json 和 preview.html。导出里的清单路径全部指向打包母版；重新 build 仍可用。目标目录必须不存在，成功后一次提交；失败不会覆盖旧成品。preview.html 是离线本地播放器，有速度、背景切换和逐帧检查，不联网。

## 故障

没有 Pillow：在当前 Python 环境安装 `python -m pip install -r <skill>/requirements.txt`。没有 ffmpeg/ffprobe：仍可导出 PNG/GIF，MP4 请求明确报缺依赖，不能冒充已输出。网格不可整除或尺寸不一致：返回素材生成阶段修复。原图有编号/黑线：先由代理检查并制定裁切方案，split 不猜边框。透明检查失败：使用图像编辑工具修正背景，或按 [生图后端](imagegen-backend.md) 的键控失败分类重生成，禁止仅把文件转 RGBA 后宣称透明。生图后端报错先读响应体的 `code` 再判断，不看 HTTP 状态码。
