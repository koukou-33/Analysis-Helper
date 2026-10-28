"""Deterministic business statements; model prose cannot supply these numbers."""

def feature_report(feature):
    if not feature or 'actual_metrics' not in feature:
        return ''
    metrics=feature['actual_metrics']
    title=feature.get('feature',{}).get('feature_id','特性')
    n=feature['sample_cnt']
    actual=feature['flag_states']
    rate=metrics['enabled']['value']
    text=(f"{title} 实际生效条目 {actual.get('True',0)} / {n}；生效比例 "
          +(f"{rate:.2%}。" if rate is not None else "不适用（无业务有效条目）。")
          +f"未生效 {actual.get('False',0)} 条，状态未知 {actual.get('None',0)} 条。"
          +"分母为全部业务有效条目，包含标志未知条目；未知不等于未生效。")
    if feature.get('validation',{}).get('status') != 'VERIFIED':
        # Do not publish unverified derived values from a failed consistency gate.
        return f"{title} 特性统计未通过一致性校验，暂不发布该部分比例及规则结论。"
    expected=feature['activation_states']
    text+=f" 规则预期生效 {expected.get('True',0)} 条、预期未生效 {expected.get('False',0)} 条、无法判断 {expected.get('None',0)} 条；预期生效但实际关闭 {feature['expected_but_disabled']} 条。"
    def condition_label(condition):
        expr=condition['expression']
        signs={'eq':'=','ne':'≠','le':'≤','lt':'<','ge':'≥','gt':'>'}
        if expr.get('op') in signs:
            return f"{expr.get('field')} {signs[expr['op']]} {expr.get('value_field') or expr.get('value')}"
        return condition['condition_id']
    labels={c['condition_id']:condition_label(c) for c in feature['feature']['conditions']}
    failed=[f"{labels.get(key,key)}：{counts.get('False',0)} 条" for key,counts in sorted(feature['disabled_conditions'].items()) if counts.get('False',0)]
    if failed:
        def ratio(value):
            return f"{value:.2%}" if value is not None else '不适用'
        failed=[f"{labels.get(key,key)}：{counts.get('False',0)} 条（占未生效条目 {ratio(feature['reason_metrics'][key]['disabled']['value'])}，占全部业务有效条目 {ratio(feature['reason_metrics'][key]['all']['value'])}）"
                for key,counts in sorted(feature['disabled_conditions'].items()) if counts.get('False',0) and key in feature.get('reason_metrics',{})]
        text+=' 实际未生效条目中，条件不满足分布：'+'；'.join(failed)+'。各条件可重叠，不能相加归因。'
    missing=feature.get('disabled_condition_coverage',{}).get('MISSING_CONDITIONS',0)
    if missing:
        text+=f' 实际未生效条目中有 {missing} 条未发现已知不满足条件但存在条件缺失，原因证据不足。'
    text+=' 以上为条目统计及规则核验，不确认根因，也不代表用户观测会话占比。'
    return text
