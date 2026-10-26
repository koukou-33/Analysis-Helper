"""User-confirmed interpretation rules, independent of retrieved document excerpts."""

FOUR_STREAM_POLICY = (
    '用户已确认：instBfSinrGain[0]、[1]、[2]、[3]分别是第一、第二、第三、第四流，分类为 STREAM。'
    '它们不属于 UNCONFIRMED_ARRAY；其他短数组的通用规则不适用于这四个字段。'
    '不要要求再次确认四流映射。正常数据通常第一流>第二流>第三流>第四流，'
    '倒序仅作为待核查现象，不据此确认异常故障或根因。'
    '增益全空只报告缺测，不能推断采集故障或无线异常。缺测不改变字段已确认的语义。'
    '有效流按每行 ueLayerNum 筛选，selLayerNum 不替代；8 流只有四列时仅覆盖前四流。'
    '可疑和非法流数记录整行排除；责任层仅提供排查方向，未核查不等于正常或免责。'
    '只分析用户问题相关指标；不要求无关 RSRP/NI/增益必须非空；同侪比较只能由用户明确启用。'
    '旧知识片段或历史模型解释若与这些规则冲突，以本规则为准。'
)


def gains_missing(summary):
    return not any(row.get('gain_valid_cnt',0)>0 for row in summary.get('streams',[]))
