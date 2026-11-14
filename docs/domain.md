# 不完整冷链事件中的时序组合发现资料约定

序列按发生时间排序，但按接收时间判断当时是否可用。事件 ID 唯一，重复环节可能是真实重复操作，不能按环节名直接去重。并行时刻没有预定全序。货批是结果单位，扫描次数不是独立结局；窗口之外以及缺失区间内不能声称某事件没有发生。业务时间跨度用于定义前置观察机会，不由退货结果倒推。

## 数据表

### `shipments`

货批物流历程，观察窗可能比业务历程短。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `start_at` | `time` |
| `end_at` | `time` |
| `route` | `text` |
| `weight_kg` | `number` |

### `events`

发生与接收时刻分开；相同发生时刻不声明内部先后，两个扫描器的事件可并行。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `shipment_id` | `text` |
| `event` | `text` |
| `occurred_at` | `time` |
| `received_at` | `time` |
| `scanner` | `text` |

### `outcomes`

退货结果在 known_at 才确认，decision_at 是计划使用流程信息的时点。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `shipment_id` | `text` |
| `returned` | `bool` |
| `known_at` | `time` |
| `decision_at` | `time` |

### `observation_windows`

端点左闭右开，gap 表示整段采集缺失；无事件和缺少观测不能互换。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `shipment_id` | `text` |
| `from_at` | `time` |
| `to_at` | `time` |
| `gap_from` | `time?` |
| `gap_to` | `time?` |

## 文件和数值语义

空单元格只在带问号的类型中表示缺失；零和空值不同。金额由十进制定点字符串保存，浮点数仅用于探索性统计。所有数据均为本地生成的虚构经营记录，不对应真实公司、客户或住址。数值剖面只反映观察数据，不能代替总体推断。

研究备注的参数为业务自由对象，不代表已执行了任何统计模型。原始表与已有视图可单独查询。
