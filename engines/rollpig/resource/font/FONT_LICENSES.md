# 随包字体授权说明（RollPig Font Licenses）

本目录随 `astrbot_plugin_dynamics_games` 开源安装包仅随附以下授权明确允许自由再分发的开源字体文件。

## 1. 荆南麦圆体（`荆南麦圆体.otf`）

| 字段 | 详情 |
| --- | --- |
| 文件名 | `engines/rollpig/resource/font/荆南麦圆体.otf` |
| 二进制字体族名（name table） | `荆南麦圆体` / `Kingnam Maiyuan Beta`（PostScript: `KNMYT-Regular` / `KingnamMaiyuanBeta`） |
| 二进制版本（name table） | `0.01Beta`（`Version 0.01;August 31, 2021;FontCreator 13.0.0.2613 64-bit`） |
| 版权声明（name table） | `(c) 2021 荆南字坊 版权所有` / `(c) 2020 荆南字坊`（`KingnamType` / 荆南字坊） |
| 授权协议 | **SIL Open Font License, Version 1.1（SIL OFL 1.1）**，完整协议全文见同目录 [`OFL-1.1.txt`](OFL-1.1.txt) |
| 公开来源说明 | 荆南字坊（Kingnam Type Foundry）公开发布之 SIL OFL 1.1 免费商用开源字型项目（如 `maoken-fonts/KNMaiyuan` 等开源镜像归档） |
| SHA-256 校验值 | `F2BD10A109F5E0D7FC39E7A0ADFE0DD55DC8297522558B4B577B93A505F86F0D` |
| 用途 | 今日小猪（`rollpig`）卡片标题与正文默认渲染字体（通过字号区分标题与正文） |

### SIL OFL 1.1 核心条款摘要

- 允许个人与商业场景免费使用，并允许随开源/商业软件捆绑与再分发；
- 再分发时须随附版权声明与本目录的 `OFL-1.1.txt` 完整协议文本；
- 不得单独出售字体文件本身；对字体文件本身的修改衍生版本须继续以 SIL OFL 1.1 协议发布。

## 2. 用户自定义字体覆盖说明

`engines/rollpig/rollpig/text.py` 中的 `RegularFontLoader` 与 `BoldFontLoader` 仍按优先级检索本目录，允许部署者自行放入本地合法授权字体文件进行覆盖（这些用户自定义文件**不会**随本开源安装包分发）：

- 正文字体优先覆盖路径：`engines/rollpig/resource/font/可爱字体.ttf`（或 `SourceHanSansCN-Regular.otf`），未放置时默认使用随包的 `荆南麦圆体.otf`。
- 标题字体优先路径：`engines/rollpig/resource/font/荆南麦圆体.otf`（可替换为其他合法授权的 `.otf`/`.ttf` 字体）。
