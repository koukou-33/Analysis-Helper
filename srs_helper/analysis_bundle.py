"""Content-addressed, immutable handoff between analysis and output stages."""
import json
import os
import tempfile
from pathlib import Path

from .sessions import dumps
from .provenance import identity
from .evidence import verify_artifact
from .result_summary import read_summary

GRAPH_VERSION = 'expert.v1'


def freeze_bundle(folder, payload):
    data = json.loads(dumps({'schema_version': 'analysis-evidence.v1', **payload}))
    for artifact in data['evidence']['artifacts']:
        verify_artifact(artifact)
        read_summary(artifact)
    return publish_json(folder, data, 'bundle')


def publish_json(folder, data, kind):
    data = json.loads(dumps(data))
    digest = identity(data)
    path = Path(folder) / (kind + '_' + digest + '.json')
    # Publish a fully written file atomically; interrupted temporary files are not bundles.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                                         prefix='.bundle_', delete=False) as stream:
            temporary = stream.name
            stream.write(dumps(data))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if identity(json.loads(path.read_text(encoding='utf-8'))) != digest:
                raise ValueError('Evidence bundle collision or corruption')
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return {'bundle_id': digest, 'hash': digest, 'path': str(path)}


def read_output(ref):
    data = json.loads(Path(ref['path']).read_text(encoding='utf-8'))
    if identity(data) != ref['hash']:
        raise ValueError('Evidence bundle hash mismatch')
    return data


def read_bundle(ref):
    data = read_output(ref)
    for artifact in data['evidence']['artifacts']:
        verify_artifact(artifact)
        read_summary(artifact)
    return data


def report_index(bundle, claims):
    artifacts = bundle['evidence']['artifacts']
    return {'bundle_schema': bundle['schema_version'],
            'steps': bundle['state'].get('analysis_steps',bundle['state']['steps']),
            'goals': bundle['state']['goals'],
            'claims': claims,
            'evidence': [{'artifact_id': a['artifact_id'], 'path': a['path'], 'sha256':a['sha256'],
                          'operator': a.get('operator'), 'purpose': a.get('purpose'),
                          'parents': a.get('parent_artifact_ids', []),
                          'query_plan': a.get('query_plan'),
                          'summary_ref': a.get('summary_ref'),
                          'scope':a.get('scope'), 'column_units':a.get('column_units'),
                          'summary': a.get('evidence_summary')} for a in artifacts]}
