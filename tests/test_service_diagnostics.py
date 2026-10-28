import json
import unittest
from unittest.mock import patch
import httpx
from langchain_core.messages import HumanMessage
from srs_helper.services import ModelService, ServiceConfig, ServiceError, call_model


class ServiceDiagnosticsTests(unittest.TestCase):
    def run_response(self, response, **options):
        service = ModelService(ServiceConfig('local_openai','http://test','test', **options),
                               httpx.MockTransport(lambda r:httpx.Response(200,json=response)))
        return call_model(service,[HumanMessage(content='PRIVATE_PROMPT')],'plan')

    def test_classification_and_redaction(self):
        cases = [(None,'stop','CONTENT_EMPTY'),('', 'stop','CONTENT_EMPTY'),
                 ('{"x":','length','OUTPUT_TRUNCATED'),('{"x":','stop','INVALID_JSON'),
                 ('[]','stop','RESPONSE_SHAPE'),({},'stop','RESPONSE_SHAPE')]
        for content,reason,code in cases:
            with self.subTest(code=code), patch('srs_helper.services.service_event') as log:
                with self.assertRaises(ServiceError) as caught:
                    self.run_response({'choices':[{'message':{'content':content,'reasoning_content':'PRIVATE_REASON'},'finish_reason':reason}],
                                       'usage':{'completion_tokens':20,'private':'PRIVATE_USAGE'}})
                self.assertEqual(caught.exception.code,code)
                text=str(log.call_args_list)
                self.assertNotIn('PRIVATE_',text)
                self.assertIn('plan',text)
                self.assertIn(code,text)

    def test_valid_and_missing_shape(self):
        for text in ('{"ok":true}','```json\n{"ok":true}\n```'):
            self.assertEqual(self.run_response({'choices':[{'message':{'content':text}}]}),{'ok':True})
        for response in ([],{}, {'choices':[]},{'choices':[None]}):
            with self.assertRaises(ServiceError) as caught:
                self.run_response(response)
            self.assertEqual(caught.exception.code,'RESPONSE_SHAPE')

    def test_recovery_requires_changed_budget_and_is_bounded(self):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':None},'finish_reason':'length'}]})
        config=ServiceConfig('local_openai','http://test','test',output_token_parameter='max_tokens',
                             plan_output_tokens=100,recovery_output_tokens=200,structured_retries=1)
        with self.assertRaises(ServiceError):
            call_model(ModelService(config,httpx.MockTransport(handler)),[HumanMessage(content='x')],'plan')
        self.assertEqual([r['max_tokens'] for r in requests],[100,200])
        self.assertTrue(all('max_completion_tokens' not in r for r in requests))
        requests.clear()
        config.recovery_output_tokens=100
        with self.assertRaises(ServiceError):
            call_model(ModelService(config,httpx.MockTransport(handler)),[HumanMessage(content='x')],'plan')
        self.assertEqual(len(requests),1)

    def test_verification_uses_planning_token_budget(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)['max_tokens'],123)
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"tasks":[]}'}}]})
        config=ServiceConfig('local_openai','http://test','test',output_token_parameter='max_tokens',
                             plan_output_tokens=123,explain_output_tokens=456)
        result=call_model(ModelService(config,httpx.MockTransport(handler)),[HumanMessage(content='x')],'verify')
        self.assertEqual(result,{'tasks':[]})

    def test_no_unconfigured_vendor_parameters(self):
        def handler(request):
            self.assertEqual(set(json.loads(request.content)),{'model','messages','response_format'})
            return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]})
        service=ModelService(ServiceConfig('local_openai','http://test','test'),httpx.MockTransport(handler))
        self.assertEqual(service.chat_json([HumanMessage(content='x')]),{})

    def test_http_error_and_budget(self):
        config=ServiceConfig('local_openai','http://test','test',call_budget_seconds=.1)
        calls=[]
        def fail(request):
            calls.append(request)
            raise httpx.ReadTimeout('PRIVATE_URL')
        with self.assertRaises(ServiceError) as caught:
            ModelService(config,httpx.MockTransport(fail)).chat_json([HumanMessage(content='x')])
        self.assertEqual(caught.exception.code,'BUDGET_EXHAUSTED')
        self.assertEqual(len(calls),1)
        self.assertNotIn('PRIVATE',str(caught.exception))

    def test_successful_recovery_and_purpose_budgets(self):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':None if len(requests)==1 else '{"ok":true}'}}]})
        config=ServiceConfig('local_openai','http://test','test',output_token_parameter='max_completion_tokens',
                             explain_output_tokens=50,recovery_output_tokens=100,structured_retries=1)
        result=call_model(ModelService(config,httpx.MockTransport(handler)),[HumanMessage(content='x')],'explain')
        self.assertEqual(result,{'ok':True})
        self.assertEqual([r['max_completion_tokens'] for r in requests],[50,100])

    def test_bigmodel_profile_is_explicit_and_scoped(self):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'request_id':'safe-id-123','choices':[{'message':{'content':'{}'}}]})
        config=ServiceConfig('openai_compatible','https://open.bigmodel.cn/api/paas/v4','glm-5.3-flash','test-key',
            output_token_parameter='max_tokens',plan_output_tokens=8192,
            capability_profile='bigmodel_glm53flash',reasoning_effort='low')
        service=ModelService(config,httpx.MockTransport(handler))
        self.assertEqual(call_model(service,[HumanMessage(content='test')],'plan'),{})
        self.assertEqual(requests[0]['thinking'],{'type':'enabled'})
        self.assertEqual(requests[0]['reasoning_effort'],'low')
        self.assertEqual(requests[0]['max_tokens'],8192)
        for field,value in [('base_url','https://example.com/v1'),('reasoning_effort','none'),
                            ('output_token_parameter','max_completion_tokens'),('model','other')]:
            original=getattr(config,field)
            setattr(config,field,value)
            with self.assertRaises(ServiceError) as caught:
                call_model(service,[HumanMessage(content='test')],'plan')
            self.assertEqual(caught.exception.code,'CONFIG')
            setattr(config,field,original)
        self.assertEqual(len(requests),1)

    def test_analysis_budget_prevents_further_requests(self):
        from srs_helper.services import model_budget
        def handler(request):
            self.fail('Expired analysis must not send a request')
        service=ModelService(ServiceConfig('local_openai','http://test','test'),httpx.MockTransport(handler))
        with model_budget(0), self.assertRaises(ServiceError) as caught:
            service.chat_json([HumanMessage(content='test')])
        self.assertEqual(caught.exception.code,'BUDGET_EXHAUSTED')
