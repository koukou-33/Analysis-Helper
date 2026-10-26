# NR SRS 示例字段映射

版本：sample-2026-10-26。机器配置唯一来源为同目录 `nr_srs_sample.json`。以下四流定义已由用户确认，优先于其他短数组的通用分类规则。

| 标准字段 | 示例源字段 | 处理 |
| --- | --- | --- |
| uwData | uwDate | 内部日期名映射到实际 CSV 日期列；源日期为 `%Y-%m-%d` |
| uwTime | uwTime | 示例精度为秒，格式 `%H:%M:%S` |
| slot | slotId | 选用前部时隙字段，不采用后部同义全空列 |
| phyCellInst | l2PhyCellInst | 辅助小区信息；会话身份仍使用 logicCellId |
| freqRbStart / freqRbNum | freqUserRbStart / freqUserRbNum | RB 起点与数量，结束点为 start + num - 1 |
| instBfSinrGain | instBfSinrGain[0..3] | 用户明确的四流增益；沿用 SDS 的 dB 口径，不缩放、不把 0 当缺失 |

`instBfSinrGain[0]`、`[1]`、`[2]`、`[3]` 分别确定对应第一、第二、第三、第四流，全部属于 **STREAM**，不属于 UNCONFIRMED_ARRAY。无需再次确认四流映射，即使列全空，其字段语义也不改变。

正常数据通常第一流＞第二流＞第三流＞第四流。倒序仅为待核查现象，不是直接判定故障、异常根因或采集问题的充分依据。增益全空时只报告缺测，不推断采集故障或无线异常；没有有效增益样本不能比较流间增益。

对上述已确认四流字段之外的数组：下标跨度 `max(index)+1 >= 32` 暂标记 BEAM_PROVISIONAL，例如 `[0..31]`；这是数组维度规则，不是字段数值阈值。其他未确认短数组才标记 UNCONFIRMED_ARRAY，不能因长度为 4 自动当作四流。波束域仅分类和保留原始数据，不假定物理含义。

重复辅助列以 `source_file + column_ordinal`（0 起始）定位；原始行按列表保存，清单保存完整表头。计算所用字段若有重复同名列，文件失败并提示歧义，不随意选首列或末列。全量扫描 field_catalog 记录实际列的 AVAILABLE / PARTIAL_MISSING / ALL_NULL / NO_RECORDS；缺失映射列记录在清单 unavailable_mapped_fields 中。

样例本身的增益、RSRP、NI 全空，因此尚不能验证实际增益单位或比例转换；当前 dB 原值口径来自设计，不声称已用真实数值校准。
