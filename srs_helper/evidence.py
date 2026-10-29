"""Three-valued feature rules and full-artifact numeric checks."""
import hashlib
import math
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

from .knowledge import FeatureSpec
from .plans import Expression, QueryPlan, Step, execute_plan


def evaluate(expr, row):
    expr = Expression.model_validate(expr) if isinstance(expr,dict) else expr
    if expr.op in {"and","or","not"}:
        values = [evaluate(e,row) for e in expr.args]
        if not values:
            raise ValueError("Empty logical rule")
        if expr.op == "not":
            if len(values)!=1:
                raise ValueError("NOT requires one operand")
            return None if values[0] is None else not values[0]
        if expr.op == "and":
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if expr.field not in row:
        return None
    value = row[expr.field]
    if expr.op == "is_null":
        return value is None
    right = row.get(expr.value_field) if expr.value_field is not None else expr.value
    if value is None or right is None:
        return None
    if any(isinstance(v,float) and not math.isfinite(v) for v in (value,right)):
        return None
    try:
        return {"eq":lambda:value==right,"ne":lambda:value!=right,
                "gt":lambda:value>right,"ge":lambda:value>=right,
                "lt":lambda:value<right,"le":lambda:value<=right}[expr.op]()
    except TypeError:
        return None


def feature_analysis(directory, scope, meta, feature, output_dir, seconds=30):
    feature = FeatureSpec.model_validate(feature)
    ids = {c.condition_id for c in feature.conditions}
    if len(ids)!=len(feature.conditions) or any(not name.startswith("c") or not name[1:].isdigit() for name in ids):
        raise ValueError("Feature condition IDs must be unique c<number>")
    if not feature.conditions:
        return {"status":"INSUFFICIENT","reason":"文档未定义条件","artifacts":[]}
    plan = QueryPlan(dataset_id=meta["dataset_id"],dataset_fingerprint=meta["dataset_fingerprint"],question=feature.title,
                     required_fields=[feature.flag_field] if feature.flag_field else [],
                     steps=[Step(step_id="s1",operator="CONDITION_COUNT",conditions={c.condition_id:Expression.model_validate(c.expression) for c in feature.conditions})])
    artifacts = execute_plan(directory,scope,plan,output_dir,seconds=seconds)
    rows_path = artifacts[0]["path"]
    conditions = {name:Counter() for name in ids}
    flags,expected,overlaps = Counter(),Counter(),Counter()
    cross_states, disabled_conditions = Counter(), {name:Counter() for name in ids}
    disabled_coverage=Counter()
    count = should_but_disabled = 0
    original_conditions={name:Counter() for name in ids}
    original_expected=Counter()
    complete_rule = feature.activation_logic is not None and feature.activation_source is not None
    if complete_rule:
        rule = Expression.model_validate(feature.activation_logic)
        def referenced_fields(expr):
            return ({expr.field} if expr.field else set()) | ({expr.value_field} if expr.value_field else set()) | set().union(*(referenced_fields(e) for e in expr.args))
        if not referenced_fields(rule)<=ids:
            raise ValueError("Activation logic must reference evaluated condition IDs")
    rule_conflict_count = 0
    for batch in pq.ParquetFile(rows_path).iter_batches(batch_size=2048):
        for row in batch.to_pylist():
            count += 1
            raw=dict(row)
            for key,value in row.items():
                if key.startswith('__cell_raw_'):
                    raw[key[len('__cell_raw_'):]]=value
            raw_conditions={c.condition_id:evaluate(c.expression,raw) for c in feature.conditions}
            for name,value in raw_conditions.items():
                original_conditions[name][str(value)]+=1
            original_expected[str(evaluate(feature.activation_logic,raw_conditions) if complete_rule else None)]+=1
            for name in ids:
                conditions[name][str(row[name])] += 1
            failed = sorted(name for name in ids if row[name] is False)
            overlaps[" & ".join(failed) or "none"] += 1
            actual = row.get(feature.flag_field)
            enabled = True if actual==feature.enabled_value else False if actual==feature.disabled_value else None
            flags[str(enabled)] += 1
            activation = evaluate(feature.activation_logic,row) if complete_rule else None
            prevented = any(row[c.condition_id] is True for c in feature.conditions if c.role=="BLOCKING") or any(row[c.condition_id] is False for c in feature.conditions if c.role=="NECESSARY")
            if complete_rule and activation is True and prevented:
                rule_conflict_count += 1
            if not complete_rule:
                sufficient = any(row[c.condition_id] is True for c in feature.conditions if c.role=="SUFFICIENT")
                prevented = any(row[c.condition_id] is True for c in feature.conditions if c.role=="BLOCKING") or any(row[c.condition_id] is False for c in feature.conditions if c.role=="NECESSARY")
                if sufficient and prevented:
                    rule_conflict_count += 1
                elif sufficient:
                    activation = True
                elif prevented:
                    activation = False
            expected[str(activation)] += 1
            cross_states[str(activation)+'|'+str(enabled)] += 1
            if enabled is False:
                category='KNOWN_UNMET_CONDITION' if any(row[name] is False for name in ids) else 'MISSING_CONDITIONS' if any(row[name] is None for name in ids) else 'EXPECTED_ACTIVE' if activation is True else 'RULE_OTHER'
                disabled_coverage[category]+=1
                for name in ids:
                    disabled_conditions[name][str(row[name])] += 1
            should_but_disabled += activation is True and enabled is False
    result = {"status":"EVALUATED" if count else "INSUFFICIENT","sample_cnt":count,
            "cell_parameter_inference":{"policy":meta["contract"].get("cell_parameter_policy"),
                "imputed_record_count":artifacts[0].get("imputed_record_count",0),
                "imputed_fields":artifacts[0].get("imputed_fields",{}),
                "original_conditions":dict(original_conditions),"original_activation_states":dict(original_expected)},
            "conditions":dict(conditions),"condition_overlap":dict(overlaps),"flag_states":dict(flags),
            "activation_states":dict(expected),"expected_but_disabled":should_but_disabled,
            "expected_actual_cross":dict(cross_states),"disabled_conditions":dict(disabled_conditions),"disabled_condition_coverage":dict(disabled_coverage),
            "complete_activation_rule":complete_rule,"rule_conflict_count":rule_conflict_count,"feature":feature.model_dump(mode="json"),
            "limitations":["必要条件满足不代表应生效；不自动确认根因","条件可能重叠，比例不可直接相加"],"artifacts":artifacts}
    errors = audit_feature_counts(result)
    result['validation'] = {'status':'FAILED' if errors else 'VERIFIED', 'errors':errors}
    if errors:
        result['status'] = 'INCONSISTENT'
    result['actual_metrics'] = {name:{'numerator':flags[state], 'denominator':count,
        'value':flags[state]/count if count else None, 'unit':'ratio', 'analysis_unit':'record',
        'denominator_definition':'ALL_BUSINESS_VALID_RECORDS',
        'artifact_ids':[a['artifact_id'] for a in artifacts]}
        for name,state in [('enabled','True'),('disabled','False'),('unknown','None')]}
    from .provenance import feature_cohorts
    result['cohorts'], cohort_artifacts = feature_cohorts(result,artifacts[0],output_dir)
    result['artifacts'].extend(cohort_artifacts)
    return result


def audit_feature_counts(result):
    errors=[]
    count=result['sample_cnt']
    for key in ('flag_states','activation_states'):
        if sum(result[key].values()) != count:
            errors.append(key+'_TOTAL_MISMATCH')
    for name,counts in result['conditions'].items():
        if sum(counts.values()) != count:
            errors.append(name+'_TOTAL_MISMATCH')
    cross=result['expected_actual_cross']
    for state in ('True','False','None'):
        if sum(cross.get(state+'|'+a,0) for a in ('True','False','None')) != result['activation_states'].get(state,0):
            errors.append('EXPECTED_MARGIN_MISMATCH')
        if sum(cross.get(a+'|'+state,0) for a in ('True','False','None')) != result['flag_states'].get(state,0):
            errors.append('ACTUAL_MARGIN_MISMATCH')
    if cross.get('True|False',0) != result['expected_but_disabled']:
        errors.append('DISABLED_MISMATCH')
    for name,counts in result['disabled_conditions'].items():
        if sum(counts.values()) != result['flag_states'].get('False',0):
            errors.append(name+'_DISABLED_TOTAL_MISMATCH')
    rule=result['feature'].get('activation_logic') or {}
    args=rule.get('args') or []
    if rule.get('op') == 'and' and args and all(a.get('op') == 'eq' and a.get('value') is True and a.get('field') in result['conditions'] for a in args):
        if any(result['activation_states'].get('True',0) > result['conditions'][a['field']].get('True',0) for a in args):
            errors.append('AND_UPPER_BOUND_VIOLATION')
    if result['rule_conflict_count']:
        errors.append('ACTIVATION_ROLE_CONFLICT')
    return sorted(set(errors))


def verify_artifact(artifact):
    path = Path(artifact["path"])
    if not path.is_file():
        raise ValueError("Evidence file is missing or modified")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream,"sha256").hexdigest()
    if digest!=artifact["sha256"]:
        raise ValueError("Evidence file is missing or modified")
    if pq.ParquetFile(path).metadata.num_rows != artifact["row_count"]:
        raise ValueError("Evidence row count mismatch")


def validate_numeric_claim(artifact, row_key, column, value, unit, expected_unit):
    verify_artifact(artifact)
    if unit != expected_unit or not row_key or not math.isfinite(float(value)):
        raise ValueError("Invalid evidence unit, key or value")
    matches = []
    for batch in pq.ParquetFile(artifact["path"]).iter_batches():
        for row in batch.to_pylist():
            if all(row.get(k)==v for k,v in row_key.items()):
                matches.append(row.get(column))
    if len(matches)!=1 or matches[0] is None or not math.isclose(float(matches[0]),float(value),rel_tol=1e-9,abs_tol=1e-9):
        raise ValueError("Claim does not match the full evidence result")
    return True
