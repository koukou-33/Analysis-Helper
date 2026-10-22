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
    if value is None or expr.value is None:
        return None
    try:
        return {"eq":lambda:value==expr.value,"ne":lambda:value!=expr.value,
                "gt":lambda:value>expr.value,"ge":lambda:value>=expr.value,
                "lt":lambda:value<expr.value,"le":lambda:value<=expr.value}[expr.op]()
    except TypeError:
        return None


def feature_analysis(directory, scope, meta, feature, output_dir):
    feature = FeatureSpec.model_validate(feature)
    ids = {c.condition_id for c in feature.conditions}
    if len(ids)!=len(feature.conditions) or any(not name.startswith("c") or not name[1:].isdigit() for name in ids):
        raise ValueError("Feature condition IDs must be unique c<number>")
    if not feature.conditions:
        return {"status":"INSUFFICIENT","reason":"文档未定义条件","artifacts":[]}
    plan = QueryPlan(dataset_id=meta["dataset_id"],dataset_fingerprint=meta["dataset_fingerprint"],question=feature.title,
                     steps=[Step(step_id="s1",operator="CONDITION_COUNT",conditions={c.condition_id:Expression.model_validate(c.expression) for c in feature.conditions})])
    artifacts = execute_plan(directory,scope,plan,output_dir)
    rows_path = artifacts[0]["path"]
    conditions = {name:Counter() for name in ids}
    flags,expected,overlaps = Counter(),Counter(),Counter()
    count = should_but_disabled = 0
    complete_rule = feature.activation_logic is not None and feature.activation_source is not None
    if complete_rule:
        rule = Expression.model_validate(feature.activation_logic)
        def referenced_fields(expr):
            return ({expr.field} if expr.field else set()) | set().union(*(referenced_fields(e) for e in expr.args))
        if not referenced_fields(rule)<=ids:
            raise ValueError("Activation logic must reference evaluated condition IDs")
    rule_conflict_count = 0
    for batch in pq.ParquetFile(rows_path).iter_batches(batch_size=2048):
        for row in batch.to_pylist():
            count += 1
            for name in ids:
                conditions[name][str(row[name])] += 1
            failed = sorted(name for name in ids if row[name] is False)
            overlaps[" & ".join(failed) or "none"] += 1
            actual = row.get(feature.flag_field)
            enabled = True if actual==feature.enabled_value else False if actual==feature.disabled_value else None
            flags[str(enabled)] += 1
            activation = evaluate(feature.activation_logic,row) if complete_rule else None
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
            should_but_disabled += activation is True and enabled is False
    return {"status":"EVALUATED" if count else "INSUFFICIENT","sample_cnt":count,
            "conditions":dict(conditions),"condition_overlap":dict(overlaps),"flag_states":dict(flags),
            "activation_states":dict(expected),"expected_but_disabled":should_but_disabled,
            "complete_activation_rule":complete_rule,"rule_conflict_count":rule_conflict_count,"feature":feature.model_dump(mode="json"),
            "limitations":["必要条件满足不代表应生效；不自动确认根因","条件可能重叠，比例不可直接相加"],"artifacts":artifacts}


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
