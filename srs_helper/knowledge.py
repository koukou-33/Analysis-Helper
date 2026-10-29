"""Versioned Markdown authority, safe image references, rebuildable retrieval."""
import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import yaml
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from rank_bm25 import BM25Okapi


class SourceRef(BaseModel):
    document_id: str
    revision: str
    section: str
    excerpt: str = ""


class FieldMeta(BaseModel):
    name: str
    description: str
    unit: str | None = None
    responsibility_layer: Literal["L1内部", "L1测量", "L2", "L3"] | None = None
    data_level: Literal["小区级", "用户级", "资源级", "流级", "波束级", "记录级", "待确认"] | None = None
    source_version: str | None = None
    source_line: int | None = None
    diagnostic_hint: str | None = None
    domain: Literal["SCALAR", "STREAM", "BEAM_PROVISIONAL", "UNCONFIRMED_ARRAY"] = "SCALAR"


class CaseCard(BaseModel):
    model_config = ConfigDict(extra="allow")
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    revision: int = Field(default=1, ge=1)
    title: str
    review_status: Literal["DRAFT", "REVIEWED"] = "DRAFT"
    root_cause_status: Literal["UNKNOWN", "HYPOTHESIS", "CONFIRMED"] = "UNKNOWN"
    source_refs: list[SourceRef] = Field(default_factory=list)
    applicability: str = "待确认"
    assertions: list[dict] = Field(default_factory=list)


class FeatureCondition(BaseModel):
    condition_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    role: Literal["NECESSARY", "SUFFICIENT", "BLOCKING", "CORROBORATING"]
    expression: dict
    source: SourceRef


class VerificationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    cohort: str
    expression: dict
    quantifier: Literal["ALL", "ANY"] = "ANY"
    source: SourceRef


class FeatureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_id: str
    revision: int = Field(default=1, ge=1)
    title: str
    flag_field: str
    enabled_value: int | float = 1
    disabled_value: int | float = 0
    conditions: list[FeatureCondition] = Field(default_factory=list)
    # An explicit full rule is required before making an "expected active" assertion.
    activation_logic: dict | None = None
    activation_source: SourceRef | None = None
    verification_rules: list[VerificationRule] = Field(default_factory=list)


def safe_asset(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or ":" in relative or Path(relative).is_absolute():
        raise ValueError("Image must be a relative local path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Image escapes its case directory")
    return path


def inspect_image(path):
    path = Path(path)
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Image exceeds 10 MB")
    with Image.open(path) as im:
        if im.format not in {"PNG", "JPEG", "WEBP"} or im.width * im.height > 20_000_000:
            raise ValueError("Unsupported image format or size")
        im.verify()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_document(path, kind="case"):
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Markdown exceeds 2 MB")
    text = path.read_text(encoding="utf-8-sig")
    metadata, body = {}, text
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise ValueError("Unclosed YAML frontmatter")
        metadata = yaml.safe_load(parts[0][4:]) or {}
        body = parts[1].lstrip("\r\n")
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter must be a mapping")
    if kind == "case":
        metadata = CaseCard.model_validate(metadata).model_dump(mode="json")
    elif kind == "feature":
        if not metadata:
            metadata = {"feature_id":path.stem,"title":path.stem,"unstructured":True}
        else:
            metadata = FeatureSpec.model_validate(metadata).model_dump(mode="json")
    elif kind == "field":
        from .field_metadata import compile_mapping
        metadata["field_catalog"] = compile_mapping(path)
    images, warnings = [], []
    for caption, relative in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body):
        asset = safe_asset(path.parent, relative)  # unsafe paths reject the document
        try:
            image_hash = inspect_image(asset)
            images.append({"path": relative, "caption": caption, "sha256": image_hash})
        except (OSError, ValueError) as exc:
            warnings.append(f"Image unavailable: {relative} ({type(exc).__name__})")
    content_hash = hashlib.sha256((text + json.dumps(images, sort_keys=True)).encode()).hexdigest()
    return {"metadata": metadata, "body": body, "images": images, "warnings": warnings,
            "version": content_hash, "kind": kind}


def tokens(text):
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower())
    return words + [a+b for a,b in zip(words,words[1:]) if len(a)==len(b)==1]


class KnowledgeIndex:
    def __init__(self, assets, storage):
        self.assets, self.storage = Path(assets).resolve(), Path(storage).resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY,version TEXT,payload TEXT,status TEXT,error TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS vectors (version TEXT,model TEXT,vector TEXT,PRIMARY KEY(version,model))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.storage / "knowledge_registry.sqlite")
        try:
            with db:
                yield db
        finally:
            db.close()

    def sync(self):
        statuses, seen = [], set()
        for folder, kind in [("cases", "case"), ("features", "feature"), ("field_mappings", "field"), ("scenarios", "scenario")]:
            for path in sorted((self.assets / folder).rglob("*.md")):
                if any(part.startswith(".pending-") for part in path.parts):
                    continue
                key = path.relative_to(self.assets).as_posix()
                seen.add(key)
                try:
                    if not path.resolve().is_relative_to(self.assets):
                        raise ValueError("Knowledge path escapes asset root")
                    doc = parse_document(path, kind)
                    doc["path"] = key
                    archive = self.storage / "versions" / doc["version"]
                    archive.mkdir(parents=True, exist_ok=True)
                    archive_record = archive / "document.json"
                    if not archive_record.exists():
                        archive_record.write_text(json.dumps({k:v for k,v in doc.items() if k!="path"}, ensure_ascii=False), encoding="utf-8")
                    for img in doc["images"]:
                        dst = safe_asset(archive, img["path"])
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if not dst.exists():
                            shutil.copyfile(safe_asset(path.parent, img["path"]), dst)
                        elif hashlib.sha256(dst.read_bytes()).hexdigest()!=img["sha256"]:
                            raise ValueError("Historical image archive is modified")
                    with self.connect() as db:
                        old = db.execute("SELECT version,status FROM documents WHERE path=?", (key,)).fetchone()
                        db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)",
                                   (key, doc["version"], json.dumps(doc, ensure_ascii=False), "READY", None))
                    statuses.append({"path": key, "status": "UNCHANGED" if old == (doc["version"], "READY") else "UPDATED"})
                except Exception as exc:
                    with self.connect() as db:
                        db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)", (key, None, None, "FAILED", type(exc).__name__))
                    statuses.append({"path": key, "status": "FAILED", "error": type(exc).__name__})
        with self.connect() as db:
            for (key,) in db.execute("SELECT path FROM documents").fetchall():
                if key not in seen:
                    db.execute("DELETE FROM documents WHERE path=?", (key,))
        return statuses

    def documents(self):
        with self.connect() as db:
            docs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM documents WHERE status='READY' ORDER BY path")]
        regular = [d for d in docs if d["kind"] != "case"]
        latest = {}
        for doc in docs:
            if doc["kind"]=="case" and doc["metadata"]["review_status"]=="REVIEWED":
                key = doc["metadata"]["case_id"]
                if key not in latest or doc["metadata"]["revision"]>latest[key]["metadata"]["revision"]:
                    latest[key] = doc
        return regular + list(latest.values())

    def search(self, question, limit=3, embedder=None):
        docs = self.documents()
        from .field_metadata import field_chunks
        docs = [chunk for doc in docs for chunk in (field_chunks(doc) if doc['kind']=='field' else [doc])]
        if not docs:
            return [], "无可用知识源"
        corpus = [tokens(json.dumps(d["metadata"], ensure_ascii=False)+d["body"]+json.dumps(d["images"],ensure_ascii=False)) or ["_"] for d in docs]
        query = tokens(question)
        scores = BM25Okapi(corpus).get_scores(query)
        ranked = sorted([i for i,c in enumerate(corpus) if set(c)&set(query)], key=lambda i: scores[i], reverse=True)
        ranks = {i: 1/(60+r) for r,i in enumerate(ranked,1)}
        mode = "BM25（语义检索未配置）"
        if embedder is not None:
            try:
                from qdrant_client import QdrantClient, models
                signature = hashlib.sha256(embedder.identity.encode()).hexdigest()[:16]
                vectors = []
                for doc in docs:
                    with self.connect() as db:
                        cached = db.execute("SELECT vector FROM vectors WHERE version=? AND model=?",(doc["version"],signature)).fetchone()
                    if cached:
                        vector = json.loads(cached[0])
                    else:
                        vector = embedder.embed([doc["body"]+json.dumps(doc["images"],ensure_ascii=False)])[0]
                        with self.connect() as db:
                            db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?)",(doc["version"],signature,json.dumps(vector)))
                    vectors.append(vector)
                query_vector = embedder.embed([question])[0]
                if any(len(v)!=len(query_vector) for v in vectors):
                    raise ValueError("Embedding dimension changed; rebuild this model cache")
                collection = "knowledge_" + signature + "_" + str(len(query_vector))
                client = QdrantClient(path=str(self.storage / "qdrant"))
                try:
                    if not client.collection_exists(collection):
                        client.create_collection(collection, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
                    points = [models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL,d["path"]+d["version"])),vector=v,
                                                 payload={"path":d["path"],"version":d["version"]}) for d,v in zip(docs,vectors)]
                    current_ids = {p.id for p in points}
                    offset = None
                    stale = []
                    while True:
                        page,offset = client.scroll(collection,limit=256,offset=offset,with_payload=False,with_vectors=False)
                        stale.extend(p.id for p in page if p.id not in current_ids)
                        if offset is None:
                            break
                    if stale:
                        client.delete(collection,points_selector=models.PointIdsList(points=stale))
                    existing_ids = {p.id for p in client.retrieve(collection,ids=list(current_ids),with_payload=False,with_vectors=False)}
                    new_points = [p for p in points if p.id not in existing_ids]
                    if new_points:
                        client.upsert(collection,points=new_points)
                    hits = client.query_points(collection,query=query_vector,limit=max(limit,len(docs))).points
                    current = {(d["path"],d["version"]):i for i,d in enumerate(docs)}
                    for rank, hit in enumerate(hits,1):
                        idx = current.get((hit.payload["path"],hit.payload["version"]))
                        if idx is not None:
                            ranks[idx] = ranks.get(idx,0) + 1/(60+rank)
                    mode = "BM25 + Qdrant（倒数排名融合）"
                finally:
                    client.close()
            except Exception:
                mode = "BM25（语义检索失败，已降级）"
        return [{**docs[i], "retrieval_score":score} for i,score in sorted(ranks.items(),key=lambda pair:pair[1],reverse=True)[:limit]], mode
