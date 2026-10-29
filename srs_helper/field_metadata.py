"""Compile user-maintained field semantics without guessing blank/conflicting entries."""
import hashlib
import re
from pathlib import Path

DATA_LEVELS = {"小区级", "用户级", "资源级", "流级", "波束级", "记录级", "待确认"}
LAYERS = {"L1内部", "L1测量", "L2", "L3"}


def compile_mapping(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    version = hashlib.sha256(text.encode()).hexdigest()
    fields, conflicts = {}, []
    in_semantics = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip('`') for c in line.strip().strip('|').split('|')]
        if cells in (['字段名称','含义','归属'], ['字段名称','含义','归属','数据层级']):
            in_semantics = True
            continue
        if not in_semantics:
            continue
        if len(cells) not in {3,4} or cells[0] in {"字段名称", "字段", "Field"} or set(cells[0]) <= {"-", ":", " "}:
            continue
        name, description, layer = cells[:3]
        data_level = cells[3] if len(cells)==4 else ""
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+(?:[~-]\d+)?\])?", name):
            continue
        match = re.fullmatch(r"(.+)\[(\d+)[~-](\d+)\]", name)
        if match:
            start, end = int(match[2]), int(match[3])
            if not 0 <= start <= end <= 4095:
                conflicts.append({"field": name, "line": line_no, "reason": "INVALID_RANGE"})
                continue
            names = [f"{match[1]}[{i}]" for i in range(start, end + 1)]
        else:
            names = [name]
        for expanded in names:
            entry = fields.setdefault(expanded, {"name": expanded, "description": "", "responsibility_layer": None,
                                                "data_level": None, "data_level_conflict": False, "sources": [], "status": "UNCONFIRMED"})
            entry["sources"].append({"line": line_no, "field": name, "description": description, "layer": layer, "data_level": data_level})
            if data_level and data_level!="待确认":
                if data_level not in DATA_LEVELS or (entry["data_level"] and entry["data_level"]!=data_level):
                    entry["data_level_conflict"]=True
                    conflicts.append({"field":expanded,"line":line_no,"reason":"data_level"})
                else:
                    entry["data_level"]=data_level
            for key, value in (("description", description), ("responsibility_layer", layer if layer in LAYERS else None)):
                if value and entry[key] and value != entry[key]:
                    entry["status"] = "CONFLICT"
                    conflicts.append({"field": expanded, "line": line_no, "reason": key})
                elif value:
                    entry[key] = value
            if entry["status"] != "CONFLICT" and entry["responsibility_layer"]:
                entry["status"] = "CONFIRMED"
    # Conflicting annotations remain auditable but must not guide classification.
    for entry in fields.values():
        if entry["data_level_conflict"]:
            entry["data_level"]=None
        if entry["status"] == "CONFLICT":
            entry["description"], entry["responsibility_layer"] = "", None
    return {"version": version, "source": str(path), "fields": fields, "conflicts": conflicts}


def relevant_fields(catalog, question, limit=30):
    """Return exact names/families and described concepts, rather than a prefix of a huge table."""
    result = []
    for entry in catalog["fields"].values():
        family = entry["name"].split('[')[0]
        if family.lower() in question.lower() or (entry["description"] and entry["description"] in question):
            result.append(entry)
    return result[:limit]


def field_chunks(document):
    """Stable field-family retrieval units; the parent document remains the authority."""
    groups = {}
    for name,entry in document['metadata'].get('field_catalog',{}).get('fields',{}).items():
        if entry['description'] or entry['responsibility_layer'] or entry.get('data_level') or entry['status']=='CONFLICT':
            groups.setdefault(name.split('[')[0],[]).append(entry)
    if not groups:
        return [document]
    result=[]
    for family,entries in groups.items():
        for offset in range(0,len(entries),64):
            batch=entries[offset:offset+64]
            body='\n'.join(f"{e['name']}: {e['description']} | {e['responsibility_layer'] or '待确认'} | {e.get('data_level') or '层级待确认'} | {e['status']}" for e in batch)
            version=hashlib.sha256((document['version']+family+str(offset)).encode()).hexdigest()
            result.append({**document,'version':version,'source_version':document['version'],'body':body,
                'metadata':{'title':family,'field_catalog':{'fields':{e['name']:e for e in batch}}}})
    return result
