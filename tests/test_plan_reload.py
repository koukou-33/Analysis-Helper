"""Exercise module reload in a subprocess without contaminating other tests."""
import subprocess
import sys
import unittest


class PlanReloadTests(unittest.TestCase):
    def test_cached_plan_accepts_reloaded_steps_and_still_validates(self):
        code = '''
import importlib
from pydantic import ValidationError
import srs_helper.plans as plans
CachedPlan = plans.QueryPlan
importlib.reload(plans)
step = plans.Step(step_id='s1', operator='GROUP_AGG', metrics=[
    plans.Metric(field='usrRsrp', aggregate='avg', output='rsrp_avg')])
plan = CachedPlan(dataset_id='d', dataset_fingerprint='f', question='q', steps=[step])
assert plan.steps[0].metrics[0].output == 'rsrp_avg'
bad = step.model_copy(update={'metrics': []})
try:
    CachedPlan(dataset_id='d', dataset_fingerprint='f', question='q', steps=[bad])
except ValidationError:
    pass
else:
    raise AssertionError('Normalization bypassed Step validation')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
