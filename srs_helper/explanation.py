"""Structured explanation checks with safe, actionable diagnostic locations."""
from .evidence import validate_numeric_claim
from .performance import validation_event
from .result_summary import read_summary
from .evidence import verify_artifact
import math

UNITS = {'gain_avg':'dB','instBfSinrGain':'dB','comparison_diff':'dB',
         'rsrp_avg':'dBm','ni_avg':'dBm','usrRsrp':'dBm','srsUeNiDb':'dBm'}


class ExplanationError(ValueError):
    def __init__(self, code, location):
        self.code, self.location = code, location
        super().__init__(f'{code} at {location}')


def validate_explanation(output, artifacts, references):
    def fail(code, location):
        raise ExplanationError(code, location)
    if not isinstance(output,dict) or not {'claims','hypotheses'} <= output.keys():
        fail('MISSING_EXPLANATION_FIELDS','$')
    claims, items = output['claims'], output['hypotheses']
    if not isinstance(claims,list) or len(claims)>20:
        fail('INVALID_CLAIMS_LIST','claims')
    known = {a['artifact_id']:a for a in artifacts}
    def normalize_ref(value, location):
        if isinstance(value,str) and value.startswith(('artifact:', 'artifact：')):
            normalized = value[9:].strip()
            if normalized in known:
                validation_event('reference_normalization',1,{'location':location,'operation':'REMOVE_ARTIFACT_PREFIX'})
                return normalized
        return value
    for i,c in enumerate(claims):
        loc = f'claims[{i}]'
        if not isinstance(c,dict) or not {'artifact_id','row_key','column','value','unit'} <= c.keys():
            fail('MISSING_CLAIM_FIELDS',loc)
        c['artifact_id'] = normalize_ref(c['artifact_id'],loc+'.artifact_id')
        if not isinstance(c['artifact_id'],str) or c['artifact_id'] not in known:
            fail('UNKNOWN_ARTIFACT',loc+'.artifact_id')
        artifact = known[c['artifact_id']]
        summary = read_summary(artifact)
        if not isinstance(c['row_key'],dict) or not c['row_key']:
            fail('INVALID_ROW_KEY',loc+'.row_key')
        if isinstance(c['value'],bool) or not isinstance(c['value'],(int,float)) or not math.isfinite(c['value']):
            fail('INVALID_NUMERIC_VALUE',loc+'.value')
        if summary and set(c['row_key'])=={'fact_id'}:
            fact=next((f for f in summary['facts'] if f['fact_id']==c['row_key']['fact_id']),None)
            if c['column']!='value' or not fact:
                fail('UNSUPPORTED_CLAIM_COLUMN',loc+'.column')
            if c['unit']!=fact['unit']: fail('UNIT_MISMATCH',loc+'.unit')
            verify_artifact(artifact)
            if not isinstance(fact['value'],(int,float)) or not math.isclose(fact['value'],c['value'],rel_tol=1e-9,abs_tol=1e-9):
                fail('EVIDENCE_MISMATCH',loc)
            continue
        units = summary['column_units'] if summary else UNITS
        if not isinstance(c['column'],str) or c['column'] not in units:
            fail('UNSUPPORTED_CLAIM_COLUMN',loc+'.column')
        if c['unit'] != units[c['column']]:
            fail('UNIT_MISMATCH',loc+'.unit')
        try:
            validate_numeric_claim(artifact,c['row_key'],c['column'],c['value'],c['unit'],units[c['column']])
        except (ValueError,TypeError,KeyError):
            fail('EVIDENCE_MISMATCH',loc)
    if not isinstance(items,list) or len(items)>5:
        fail('INVALID_HYPOTHESES_LIST','hypotheses')
    allowed = set(known) | {r['path'] for r in references}
    for i,item in enumerate(items):
        loc = f'hypotheses[{i}]'
        if not isinstance(item,dict) or not isinstance(item.get('text'),str):
            fail('INVALID_HYPOTHESIS',loc)
        refs = item.get('references')
        if isinstance(refs,list):
            refs = [normalize_ref(r,loc+'.references') for r in refs]
            item['references'] = refs
        if not isinstance(refs,list) or not refs or any(not isinstance(r,str) or r not in allowed for r in refs):
            fail('UNBOUND_HYPOTHESIS',loc+'.references')
    return claims,items
