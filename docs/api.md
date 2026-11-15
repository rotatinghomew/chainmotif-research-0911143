# 资料服务接口

服务只在本机提供 JSON，响应编码为 UTF-8。查询接口不修改资料。

| 方法和路径 | 当前行为 |
| --- | --- |
| `GET /health` | 数据指纹和存活状态 |
| `GET /tables` | 表、字段类型和记录数 |
| `GET /tables/{name}?offset=0&limit=50` | 原始资料分页；偏移非负，每页一至五百行 |
| `GET /view` | 当前领域经营资料摘要 |
| `GET /profiles` | 数值字段的观察数、缺失数、中位数及绝对中位差 |
| `GET /pattern-report?min_support=8&max_length=3&max_gap_hours=24&discovery_fraction=0.5&alpha=0.05&correction=bh&matches=1` | 时序组合发现报告；查询参数可省略，`matches=0` 时省略匹配明细只留统计 |
| `POST /pattern-reports` | 按 `label`、`params`、`notes` 运行发现并存入不可变研究记录（含完整资料副本与全部匹配明细）；返回新记录 |
| `GET /pattern-reports` | 组合研究记录索引 |
| `GET /pattern-reports/{id}` | 校验完整性并回读记录 |
| `POST /cases` | 保存研究备注和当前完整资料副本；返回新记录 |
| `GET /cases` | 研究记录索引 |
| `GET /cases/{id}` | 校验完整性并回读记录 |

组合发现的模式语义、时点口径、货批角色与统计方法见 `docs/patterns.md`。报告与保存的记录都列出每次匹配的真实事件 ID 与时间边界，供调度员核对流程；关联不等于致损原因。

创建研究备注的字段为 `label`、`options` 和 `notes`，分别是名称、参数对象与说明文字。名称一至一百二十字，后两项可省略。不接受其他字段，不允许覆盖旧记录。记录 ID 由服务生成。资料指纹由原始数据和结构共同形成，不表示计算结果的统计可信度。

源表未找到或记录 ID 无效返回 404；字段、参数或分页无效返回 400。默认监听 127.0.0.1:8080。服务当前提供资料浏览、数值探索、时序组合发现和研究备注，不将保存备注或组合报告视为已经完成经营分析。
