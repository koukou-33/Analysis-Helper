"""Explicit field bindings and position-preserving wide-table inventory."""
import re
from collections import Counter, defaultdict


def bindings(header, contract, numeric_names):
    mapping = contract.get("field_mapping", {})
    aliases = mapping.get("aliases", {})
    counts = Counter(header)
    targets = set(numeric_names) | {"uwData", "uwTime"}
    absent = [n for n in contract["required_identity_columns"] if aliases.get(n, n) not in counts]
    if absent:
        raise ValueError(f"Missing required columns: {absent}")
    return {n: header.index(aliases.get(n, n)) for n in targets if aliases.get(n, n) in counts}


def duplicate_groups(header):
    positions = defaultdict(list)
    for i,name in enumerate(header):
        positions[name].append(i)
    return {name:indices for name,indices in positions.items() if len(indices)>1}


def merge_duplicate_values(row, groups, null_values):
    merged, conflicts = list(row), set()
    for name,indices in groups.items():
        values = {row[i].strip() for i in indices if i<len(row) and row[i].strip() not in null_values}
        if len(values)>1:
            conflicts.add(name)
        value = next(iter(values)) if len(values)==1 else ''
        for i in indices:
            if i<len(merged):
                merged[i] = value
    return merged,conflicts


def column_catalog(header, mapping):
    families = defaultdict(set)
    for name in header:
        match = re.fullmatch(r"(.+)\[(\d+)\]", name)
        if match:
            families[match[1]].add(int(match[2]))
    result = []
    for ordinal, name in enumerate(header):
        match = re.fullmatch(r"(.+)\[(\d+)\]", name)
        family, index, extent, domain = None, None, None, "SCALAR"
        if match:
            family, index = match[1], int(match[2])
            extent = max(families[family]) + 1
            domain = "UNCONFIRMED_ARRAY"
            if extent >= mapping.get("beam_min_array_length", 32):
                domain = "BEAM_PROVISIONAL"
            elif family == "instBfSinrGain" and index < 4:
                domain = "STREAM"
        result.append((ordinal, name, family, index, extent, domain))
    return result
