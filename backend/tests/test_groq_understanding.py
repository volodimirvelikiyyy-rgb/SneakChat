"""Simplified model contract with real backend/session/catalog and HTTP transport stubs."""

from copy import deepcopy
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.ai.groq_provider import GroqTurnPlanner
from app.ai.providers import ProviderUnavailable
from app.ai.turn_plan import TurnUnderstanding
from app.api.chat import send_message
from app.models import ContactRequest
from app.schemas import ChatRequest
from app.services.chat import _sessions
from app.ai.grounding import verify_products, verify_reply
import test_shopper_conversation as fixtures


def understanding(intent='recommend', **updates):
    return {'intent': intent, 'changes': [], 'model_queries': [], 'unresolved_preferences': [], **updates}


def response(payload):
    return BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': json.dumps(payload, ensure_ascii=False)}}]}).encode())


def api_error(code=400, remaining='100000', error_code='json_validate_failed'):
    return HTTPError('https://api.groq.com', code, 'error', {
        'x-ratelimit-remaining-tokens': remaining, 'x-ratelimit-remaining-requests': '999'},
        BytesIO(json.dumps({'error': {'type': 'invalid_request_error', 'code': error_code,
                                    'message': 'private provider body'}}).encode()))


class UnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ShopperConversationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db, self.sid = self.fixture.db, self.fixture.sid
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect', side_effect=AssertionError('External network forbidden')).start()
        patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'synthetic_only'}).start()
        self.opener = patch('app.ai.groq_provider.build_opener').start().return_value

    def send(self, message, intent='recommend', language='uk', **updates):
        self.opener.open.return_value = response(understanding(intent, **updates))
        result = send_message(ChatRequest(session_id=self.sid, message=message,
                              language=language, display_currency='USD'), self.db)
        self.assertEqual(result['mode'], 'groq', result)
        self.assertEqual(verify_products(self.db, result['products']), [])
        self.assertEqual(verify_reply(result['reply'], result['products'], result['filters']), [])
        return result

    def test_four_field_schema_and_backend_supplies_missing_explicit_constraints_ru_uk(self):
        self.assertEqual(set(TurnUnderstanding.model_json_schema()['properties']),
                         {'intent', 'changes', 'model_queries', 'unresolved_preferences'})
        for language, text in (('ru', 'Nike для бега, размер 43 до 150 долларов'),
                               ('uk', 'Nike для бігу, розмір 43 до 150 доларів')):
            self.sid += language
            result = self.send(text, language=language)
            self.assertEqual(result['filters']['size'], 43)
            self.assertEqual(result['filters']['max_price'], 150)
            self.assertEqual(result['filters']['currency'], 'USD')
            self.assertEqual(result['filters']['use_case'], 'Running')
            self.assertEqual(result['filters']['brand'], 'Nike')

    def test_complete_uk_dialogue_preferences_comfort_switch_selection_and_order(self):
        self.assertIn('EU', self.send('Привіт, шукаю кросівки на кожен день')['reply'])
        result = self.send('Бажано Nike, розмір 43', 'select')  # real model classified it as select
        self.assertNotIn('brand', result['filters'])
        self.assertEqual(_sessions[self.sid]['preferences']['brand'], 'Nike')
        self.assertEqual(len(result['products']), 2)
        self.send('До 150 доларів')
        result = self.send('А що зручніше?', 'compare')  # backend detects missing facts even if model omits them
        self.assertFalse(result['needs_clarification'])
        self.assertIn('лише на відомості каталогу', result['reply'])
        self.assertEqual(len(result['products']), 2)
        self.assertIn('110 USD', self.send('Порівняй ці дві моделі', 'compare')['reply'])
        switched = self.send('А якщо Adidas?', 'compare')  # real model inherited comparison intent
        self.assertTrue(all(p['brand'] == 'Adidas' for p in switched['products']))
        self.assertEqual(_sessions[self.sid]['filters']['brand'], 'Adidas')
        self.send('Ні, повернімося до Nike', changes=[{'field': 'brand', 'operation': 'set',
                  'value': 'Nike', 'evidence': 'Ні, повернімося до Nike'}])
        self.assertEqual(_sessions[self.sid]['filters']['brand'], 'Nike')
        self.send('Покажи дешевші', 'cheaper')
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 150)
        selected = self.send('Беру Dunk Low, 43 розмір', 'select', model_queries=['Dunk Low'])
        self.assertEqual(selected['products'][0]['name'], 'Dunk Low Retro')
        self.assertIn('110 USD', self.send('Ціна цієї пари', 'price')['reply'])
        ordered = self.send('Хочу оформити замовлення', 'order')
        self.assertIn('Залишити заявку', ordered['reply'])
        self.assertEqual(self.db.query(ContactRequest).count(), 0)

    def test_soft_brand_is_not_a_filter_when_preferred_brand_has_no_stock(self):
        result = self.send('Бажано Adidas, розмір 43')
        self.assertTrue(result['products'])
        self.assertNotIn('brand', result['filters'])
        self.assertEqual(_sessions[self.sid]['preferences']['brand'], 'Adidas')

    def test_ambiguous_two_of_three_is_a_backend_question(self):
        self.send('Розмір 42')
        result = self.send('Порівняй ці дві', 'compare')
        self.assertTrue(result['needs_clarification'])
        self.assertIn('Які дві', result['reply'])

    def test_unknown_model_and_compound_model_queries_are_read_only_lookups(self):
        result = self.send('Знайди Nike Phantom 999', 'locate', model_queries=['Nike Phantom 999'])
        self.assertEqual(result['products'], [])
        self.assertIn('Не знайшов', result['reply'])
        result = self.send('Порівняй Nike Dunk Low і Adidas Superstar', 'compare',
                           model_queries=['Nike Dunk Low', 'Adidas Superstar'])
        self.assertEqual(len(result['products']), 2)

    def test_topic_change_and_return_preserve_selected_bookmark(self):
        self.send('Nike Dunk Low, розмір 43', 'select', model_queries=['Nike Dunk Low'])
        before = deepcopy(_sessions[self.sid]['dialogue']['selected_task'])
        self.send('Яка сьогодні погода?', 'other_topic')
        self.assertEqual(_sessions[self.sid]['dialogue']['selected_task'], before)
        self.send('Покажи Adidas Superstar', 'locate', model_queries=['Adidas Superstar'])
        self.assertEqual(_sessions[self.sid]['dialogue']['selected_task'], before)
        result = self.send('Повернімося до обраної пари', 'resume')
        self.assertEqual(result['products'][0]['name'], 'Dunk Low Retro')

    def test_contradictory_delta_is_rejected_and_fallback_retains_constraints(self):
        self.send('Nike, розмір 43 до 150 доларів')
        self.opener.open.return_value = response(understanding(changes=[
            {'field': 'max_price', 'operation': 'set', 'value': 120, 'evidence': 'Покажи дешевші'}]))
        result = send_message(ChatRequest(session_id=self.sid, message='Покажи дешевші', language='uk'), self.db)
        self.assertEqual(result['mode'], 'fallback')
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 150)
        self.send('Тільки не Adidas')
        self.assertIn('Adidas', _sessions[self.sid]['filters']['exclude_brand'])

    def test_json_retry_once_with_verified_quota(self):
        self.opener.open.side_effect = [api_error(), response(understanding())]
        planner = GroqTurnPlanner('synthetic_only')
        planner.plan({}, 'До 150 доларів', 'uk')
        self.assertEqual(self.opener.open.call_count, 2)
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertTrue(payload['response_format']['json_schema']['strict'])
        print('SIMPLIFIED_REQUEST_BYTES=' + str(len(self.opener.open.call_args.args[0].data)))

    def test_second_json_failure_never_causes_third_attempt(self):
        self.opener.open.side_effect = [api_error(), api_error()]
        with self.assertRaises(ProviderUnavailable):
            GroqTurnPlanner('synthetic_only').plan({}, 'До 150 доларів', 'uk')
        self.assertEqual(self.opener.open.call_count, 2)

    def test_no_retry_for_429_low_or_unknown_quota_or_different_400(self):
        for error in (api_error(429), api_error(remaining='1'),
                      api_error(remaining='redacted'), api_error(error_code='invalid_json_schema')):
            self.opener.open.reset_mock()
            self.opener.open.side_effect = error
            with self.assertRaises(ProviderUnavailable):
                GroqTurnPlanner('synthetic_only').plan({}, 'До 150 доларів', 'uk')
            self.opener.open.assert_called_once()

    def test_private_message_never_leaves_process(self):
        with self.assertRaisesRegex(ProviderUnavailable, 'privacy_blocked'):
            GroqTurnPlanner('synthetic_only').plan({}, 'Мій телефон +380991234567', 'uk')
        self.opener.open.assert_not_called()

