# NR SRS 示例字段映射

版本：sample-2026-10-22。机器配置唯一来源为同目录 `nr_srs_sample.json`。

| 标准字段 | 示例源字段 | 处理 |
| --- | --- | --- |
| uwData | uwDate | 保持设计中的标准名；源日期为 `%Y/%m/%d` |
| uwTime | uwTime | 示例精度为秒，格式 `%H:%M:%S` |
| slot | slotId | 选用前部时隙字段，不采用后部同义全空列 |
| phyCellInst | l2PhyCellInst | 辅助小区信息；会话身份仍使用 logicCellId |
| freqRbStart / freqRbNum | freqUserRbStart / freqUserRbNum | RB 起点与数量，结束点为 start + num - 1 |
| instBfSinrGain | instBfSinrGain[0..3] | 用户明确的四流增益；沿用 SDS 的 dB 口径，不缩放、不把 0 当缺失 |

截至此版本，仅 instBfSinrGain 被明确映射为流指标。按用户临时规则，数组下标跨度 `max(index)+1 >= 32` 的字段族标记 BEAM_PROVISIONAL；例如 `[0..31]` 即达到 32。该规则针对数组维度，不是数值大小。若只有高下标列，按相同跨度保守归类。其余短数组标记 UNCONFIRMED_ARRAY，不因长度为 4 自动展开。波束域暂仅分类并保留原始数据，不计算假定物理语义的指标。

重复辅助列以 `source_file + column_ordinal`（0 起始）定位；原始行按列表保存，清单保存完整表头。计算所用字段若有重复同名列，文件失败并提示歧义，不随意选首列或末列。全量扫描 field_catalog 记录实际列的 AVAILABLE / PARTIAL_MISSING / ALL_NULL / NO_RECORDS；缺失映射列记录在清单 unavailable_mapped_fields 中。

样例本身的增益、RSRP、NI 全空，因此尚不能验证实际增益单位或比例转换；当前 dB 原值口径来自设计，不声称已用真实数值校准。
