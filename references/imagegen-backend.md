# 生图后端

两条路线共用同一套动作设计、同一套验收标准和同一份清单，只有调用机制和记录字段不同。

## 选择顺序

1. **内置生图工具**（Codex `image_gen`、自带图像 MCP 等）。参数表里没有参考图或输出路径选项不构成跳过理由，内置工具通过多模态上下文接收参考图。判断自己在不在这条路上靠有没有内置图像能力，不靠环境变量。
2. **`scripts/generate_frames.py`**（Labnana GPT-Image-2.5）。当前环境没有内置图像工具，或内置工具连续失败时使用。没有内置工具的 Agent 直接走这条，不必先征求用户同意选后端。
3. 用户明确点名的其他后端。

用户明确点名的后端优先于以上默认顺序。分辨率是路由条件：内置工具无法保证目标尺寸时优先分表，或在已授权的 Labnana 路线上生成高分辨率总图；不能默默接受低分辨率再放大。API 的 `--image-size` 默认 `auto`，带 `--cells` 且不少于 16 格时请求 4K，其余 2K；显式 1K/2K/4K 优先。独立多帧调用应按完整项目规划明确传尺寸。

请求 4K 不代表已经获得合格细节。记录 provider.returned_size、nominal_cell_size、estimated_safe_cell_size，逐次核验目标单帧像素及实际清晰度，resolution_review/layout_review 默认 pending。8% 安全区只是规划估算。请求未兑现或单帧像素不足时分表/重生成，不能把放大尺寸记为原生尺寸。本地离线测试仅验证参数选择，未证明后端实际 4K 返回质量。

`python <skill>/scripts/generate_frames.py precheck` 报告密钥、模型、比例表和账户额度。precheck 为正不等于可以跳过内置工具。

密钥来自 `.env`：skill 根目录、`~/.claude/image-backends.env`、或复用 `~/.claude/skills/figedit-v2` 与 `FigEcho` 已有配置。一把 `LABNANA_API_KEY` 服务所有生图 skill，不要求用户在对话里粘贴密钥。

## 模型

| `--model` | 实际 id | 用途 |
|---|---|---|
| `flare`（默认） | `gpt-image-2.5-flare` | 整表、关键帧、反复重生成。实测 2K 约 25–35 秒 |
| `sunburst` | `gpt-image-2.5-sunburst` | 要求精细度或清晰度更高时；局部修帧、细节密集的主体 |
| `gpt-image-2` | `gpt-image-2` | 旧模型，只在 2.5 两个变体都失败时兜底 |

两个 2.5 变体是 Labnana 官方 guide 没写但实测可用的（2026-09-12 验证）。模型名写错时后端在 1 秒内返回 `400 / code 29003 / "AI content generation failed"`，和参数错误同码，脚本会把已知 id 附在报错里。`gpt-image-2.5`（不带后缀）不是有效 id。

## 参数边界

Labnana 的 `imageConfig` 是严格 allowlist，只有三个字段：

- `imageSize`：`1K` `2K` `4K`
- `aspectRatio`：`1:1` `2:3` `3:2` `3:4` `4:3` `4:5` `5:4` `9:16` `16:9` `21:9`。带 --cells 的动作表遇到不支持的比例，在请求前拒绝并要求重规划；非表图沿用最近比例吸附与 notes 记录。
- `quality`：`low` `medium` `high`。OpenAI 官方的 `xhigh` 和 `max` 传进来直接被拒

参考图最多 4 张，走 `inlineData` base64，整个请求体上限 20 MB，脚本超限时自动降采样并记录。

**OpenAI 官方的 `background` 和 `output_format` 在这条通道上不存在**，传进 `imageConfig` 返回 `"is not allowed"`，放在顶层则被静默忽略。这决定了下面整节。

## 透明只能靠键控

只在提示词里要求真实 alpha，模型会把棋盘格画进 RGB 像素里冒充透明，返回的 PNG 根本没有 alpha 通道。这正是本技能禁止的那种假透明，不能接收。

脚本的做法是把透明拆成两步，模型只负责第一步：

1. 生成时强制把键控子句追加进提示词，要求主体之外每个像素都是同一个纯色平底，并禁止棋盘格、渐变、投影、地台和格线。整表还会加一句"所有格子底色完全相同"。这一步由脚本追加，写提示词的人不需要也不应该自己写。
2. 拿到图后在本地抠：取边缘环的中位色作为实测键色，按 Chebyshev 距离做双阈值软边抠图，然后校验实际 alpha。

键色默认 `auto`，从纯绿、纯品红、纯青、纯蓝、纯黄里挑一个离主体色最远的。**键色撞上主体主色会静默吃掉主体的一部分**，所以要把已知的主体色用 `--avoid-color` 传进去；带参考图时脚本会自己采样参考图的主色。记录里的 `key_margin_to_subject_palette` 就是这个安全距离，不要当装饰看。

校验不通过时脚本返回 `keying_failed` 并且不写输出文件，失败原因分得很细：

- `painted_checkerboard`：边缘环是两档近中性色的规则花纹，模型画了假透明。重生成，不要抠这张。
- 键色偏移过大：底色不是要求的那个键色。
- 抠出比例过低或过高：没有平底可抠，或者主体被一起抠掉了。
- `key_spill_fraction` 偏高：键色和主体撞色，换 `--key-color` 重生成。

整表额外给出每格的抠出占比和极差，仅作检查线索；姿态、变形和特效范围也会造成变化，不能据此断言底色不一致。分别检查实际底色和主体完整性，不调阈值掩盖失败。

`--opaque` 关掉整条键控链路，用于用户明确指定背景或延续参考场景的情况；此时清单的 `require_transparency` 也应该是 `false`。

## 命令

```text
python <skill>/scripts/generate_frames.py precheck
python <skill>/scripts/generate_frames.py generate --prompt-file <project>/prompt.txt --out <project>/raw/sheet.png --record <project>/raw/generation-record.json --model flare --aspect-ratio 1:1 --image-size 2K --cells 4x4 --avoid-color "#3B82F6"
python <skill>/scripts/generate_frames.py generate --prompt-file <project>/prompt-fix.txt --reference <project>/raw/sheet.png::identity --reference <project>/raw/frame-3.png::edit_target --out <project>/raw/fix-3.png --model sunburst
python <skill>/scripts/generate_frames.py key --image <project>/raw/sheet.png --out <project>/raw/sheet-keyed.png --cells 4x4
```

提示词用 `--prompt-file` 传，不走命令行字符串，避免 Windows 管道和引号破坏中文。`--out` 已存在时直接报错，不覆盖既有帧。`--keep-raw` 额外保留抠图前的原图，便于换阈值重抠。

参考图角色：`identity` `style` `motion` `composition` `edit_target` `supporting_insert`，默认 `identity`。身份锚必须真的作为图片输入绑定，只在提示词里写"参考图"不算绑定。

## 记录

`--record` 写出的 generation-record.json 要和项目清单一起保留，里面有实际模型、实际发出的比例、每次尝试的 http_status 与 code、键色与安全距离、抠图各项指标和每格占比。清单的 `provider` 字段按实际情况写 `{"tool": "scripts/generate_frames.py", "model": "gpt-image-2.5-flare"}`，不要继续写 `host-managed/unknown`。

## 失败分流

所有失败都是 HTTP 400，状态码分不出原因，必须读响应体的 `code`。脚本已经按这张表分流，读报错时也按它判断：

| code | 含义 | 处理 |
|---|---|---|
| 21007 / 26004 | 密钥无效 / 积分不足 | 账号级失败，立刻停，不要换付费后端偷偷顶上 |
| 29998 | 限流 | 20 秒起步退避重试 |
| 91002 | 内容安全审核拒绝 | 判定不稳定，先原样重试；仍被拒时改走带 `edit_target` 的定向修改 |
| 29003 / 26019 | 参数错误、模型名未知、比例不支持 | 不重试，改参数 |
| 5xx / 504 / 连接中断 | 传输层 | 原样重试，2K/4K 响应体几 MB，中断是常态 |

没有任何后端可用时，交付已完成的动作设计与提示词，明确说明 PNG 未生成。不要静默降级成"给了提示词就算完成"，也不要用代码画的几何图形冒充生成的角色。
