"""Backend-owned internal plan execution, session safety and transport failures.

The four-field external protocol is exercised in test_groq_understanding.
"""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from sqlalchemy import select

from app.ai.grounding import verify_products, verify_reasons, verify_reply
from app.models import ChatLog, ContactRequest
from app.schemas import ChatRequest, ChatResponse
from app.api.chat import send_message
from app.services.chat import chat, _sessions
import test_groq_turn_plan as contract
import test_shopper_conversation as fixtures


def setUpModule():
    global _network_guard
    _network_guard = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in Groq integration tests'))
    _network_guard.start()


def tearDownModule():
    _network_guard.stop()


class GroqChatTests(unittest.TestCase):
    def test_captured_invented_budget_uses_fallback_without_partial_changes(self):
        with patch.dict('os.environ', {'AI_PROVIDER': 'disabled'}):
            self.fixture.send('Хочу Nike, размер 43 до 150 долларов')
        captured = json.loads((Path(__file__).parent / 'data' / 'groq_rejected_plans.json').read_text(encoding='utf-8'))
        case = next(c for c in captured['cases'] if c['step'] == 8)
        self.opener.open.return_value = contract.response(contract.understanding(changes=case['plan']['changes'], unresolved_preferences=[]))
        with patch('app.ai.turn_execution.execute_turn_plan', side_effect=AssertionError('Unsafe plan executed')) as execute:
            result = chat(self.db, self.sid, case['message'], 'uk', 'USD')
        execute.assert_not_called()
        self.opener.open.assert_called_once()
        self.assertEqual(result['mode'], 'fallback')
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 150)
        self.assertEqual(_sessions[self.sid]['filters']['size'], 43)
        self.assertEqual(_sessions[self.sid]['filters']['brand'], 'Nike')
        self.assertEqual(self.db.query(ContactRequest).count(), 0)

    def test_captured_budget_plan_executes_without_hardening_soft_brand(self):
        with patch.dict('os.environ', {'AI_PROVIDER': 'disabled'}):
            self.fixture.send('Шукаю кросівки на кожен день, розмір 43, бажано Nike', 'uk')
        before = deepcopy(_sessions[self.sid])
        self.assertEqual(before['preferences']['brand'], 'Nike')
        self.assertNotIn('brand', before['filters'])
        captured = json.loads((Path(__file__).parent / 'data' / 'groq_rejected_plans.json').read_text(encoding='utf-8'))
        case = next(c for c in captured['cases'] if c['step'] == 3)
        self.opener.open.return_value = contract.response(contract.understanding())
        result = chat(self.db, self.sid, case['message'], 'uk', 'USD')
        self.assertEqual(result['mode'], 'groq')
        self.assertTrue(result['products'])
        self.assertEqual(result['filters']['max_price'], 150)
        self.assertEqual(result['filters']['currency'], 'USD')
        self.assertEqual(result['filters']['size'], 43)
        self.assertEqual(_sessions[self.sid]['preferences']['brand'], 'Nike')
        self.assertNotIn('brand', result['filters'])
        self.assertEqual(self.db.query(ContactRequest).count(), 0)

    def test_stated_unsupported_criterion_is_not_asked_again(self):
        self.recommend()
        result = self.send('А що зручніше?', contract.plan(intent='clarification', transition='refine',
            actions=[], question={'kind': 'criterion', 'reason': 'ambiguous'},
            reply_plan=[contract.part('clarification')], unresolved_preferences=['зручніше']), 'uk')
        self.assertFalse(result['needs_clarification'])
        self.assertNotIn('важливіше', result['reply'])
        self.assertIn('Можу порівняти ціну', result['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['brand'], 'Nike')
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)

    def test_short_requests_for_selection_help_do_not_call_groq_or_suspend(self):
        for language, message in (('uk', 'так допоможіть'), ('ru', 'подскажите')):
            self.opener.open.reset_mock()
            result = chat(self.db, self.sid, message, language, 'USD')
            self.opener.open.assert_not_called()
            self.assertEqual(result['mode'], 'fallback')
            self.assertTrue(result['products'])
            self.assertNotEqual(_sessions[self.sid]['dialogue']['transition'], 'suspend')

    def test_pure_greetings_bypass_planner_and_never_start_discovery(self):
        for language, message in (('uk', 'Вітаю'), ('ru', 'Привет!')):
            self.opener.open.reset_mock()
            self.opener.open.return_value = contract.response(contract.plan(intent='recommend', actions=[],
                question={'kind': 'need', 'reason': 'missing'}, reply_plan=[contract.part('clarification')]))
            result = chat(self.db, self.sid, message, language, 'USD')
            self.opener.open.assert_not_called()
            self.assertEqual(result['mode'], 'fallback')
            self.assertFalse(result['needs_clarification'])
            self.assertIn('Чим можу' if language == 'uk' else 'Чем могу', result['reply'])

    def test_generic_need_question_with_known_preferences_uses_fallback(self):
        self.recommend()
        self.opener.open.return_value = contract.response(contract.plan(intent='recommend',
            question={'kind': 'need', 'reason': 'missing'}, reply_plan=[contract.part('clarification')]))
        with patch('app.ai.turn_execution.execute_turn_plan', side_effect=AssertionError('Rejected plan executed')):
            result = chat(self.db, self.sid, 'Хочу Nike размер 42', 'ru', 'USD')
        self.assertEqual(result['mode'], 'fallback')
        self.assertTrue(result['products'])
        self.assertEqual(result['filters']['size'], 42)
        self.assertEqual(result['filters']['max_price'], 120)
        self.assertFalse(result['needs_clarification'])

    def test_repeated_discovery_plan_shows_catalog_even_if_model_resets_task(self):
        for language, message in (('ru', 'Посоветуй кроссовки'), ('uk', 'Порадь кросівки')):
            self.sid += '-' + language
            payload = contract.plan(intent='recommend', actions=[], question={'kind':'need','reason':'missing'},
                                    reply_plan=[contract.part('clarification')])
            initial = self.send(message, payload, language)
            self.assertEqual(initial['reply'].count('?'), 1)
            repeated = self.send(message, {**payload, 'transition': 'new_task'}, language)
            self.assertTrue(repeated['products'])
            self.assertFalse(repeated['needs_clarification'])
            self.assertNotEqual(repeated['reply'], initial['reply'])

    def test_short_fact_questions_bypass_planner_without_losing_need_context(self):
        self.fixture.send('Посоветуй кроссовки', 'uk')
        for language, message in (('uk', 'розмір ціна?'), ('ru', 'размер цена?')):
            with self.subTest(language=language):
                self.opener.open.reset_mock()
                self.opener.open.return_value = contract.response(contract.plan(
                    actions=[contract.action(queries=[message])]))
                result = chat(self.db, self.sid, message, language, 'USD')
                self.opener.open.assert_not_called()
                self.assertEqual(result['mode'], 'fallback')
                self.assertTrue(result['needs_clarification'])
                self.assertEqual(result['products'], [])
                self.assertEqual(_sessions[self.sid]['dialogue']['last_question']['kind'], 'model')

    def test_turn_plan_cannot_use_catalog_field_questions_as_model_names(self):
        from app.ai.groq_provider import public_context
        from app.ai.turn_plan import TurnPlan, validate_plan_context
        for message in ('розмір ціна?', 'размер цена?', 'наявність?', 'стоимость?'):
            for payload in (contract.plan(actions=[contract.action(queries=[message])]),
                            contract.plan(changes=[contract.change('name_query', message, message)])):
                with self.subTest(message=message, payload=payload), self.assertRaisesRegex(ValueError, 'not a model name'):
                    validate_plan_context(TurnPlan.model_validate(payload), message, public_context({}, 'ru', 'USD'))

    def test_lost_nike_and_purpose_rejected_before_execution(self):
        # Replay the structurally valid but incomplete real-model response.
        self.opener.open.return_value = contract.response(contract.plan(intent='recommend', actions=[],
            question={'kind': 'need', 'reason': 'missing'}, reply_plan=[contract.part('clarification')]))
        with patch('app.ai.turn_execution.execute_turn_plan', side_effect=AssertionError('Rejected plan executed')) as execute:
            result = chat(self.db, self.sid, 'Нужны Nike на каждый день', 'ru', 'USD')
        execute.assert_not_called()
        self.assertEqual(result['mode'], 'fallback')
        self.assertEqual(_sessions[self.sid]['filters']['brand'], 'Nike')
        self.assertEqual(_sessions[self.sid]['filters']['use_case'], 'Lifestyle')

    def test_lost_compound_constraints_use_fallback_without_partial_changes(self):
        for language, message in (
            ('ru', 'Хочу Nike для бега, размер EU 42, до 120 долларов'),
            ('uk', 'Хочу Nike для бігу, розмір EU 42, до 120 доларів'),
        ):
            with self.subTest(language=language):
                _sessions.pop(self.sid, None)
                self.sid += '-' + language
                self.opener.open.return_value = contract.response(contract.plan(intent='recommend'))
                with patch('app.ai.turn_execution.execute_turn_plan', side_effect=AssertionError('Rejected plan executed')) as execute:
                    result = chat(self.db, self.sid, message, language, 'USD')
                execute.assert_not_called()
                self.assertEqual(result['mode'], 'fallback')
                filters = _sessions[self.sid]['filters']
                for field, value in {'brand': 'Nike', 'size': 42, 'max_price': 120,
                                     'currency': 'USD', 'use_case': 'Running'}.items():
                    self.assertEqual(filters[field], value)

    def test_ignored_negation_uses_fallback_and_preserves_size_budget(self):
        self.recommend()
        self.opener.open.return_value = contract.response(contract.plan(transition='refine'))
        with patch('app.ai.turn_execution.execute_turn_plan', side_effect=AssertionError('Rejected plan executed')) as execute:
            result = chat(self.db, self.sid, 'Только не Nike', 'ru', 'USD')
        execute.assert_not_called()
        self.assertEqual(result['mode'], 'fallback')
        filters = _sessions[self.sid]['filters']
        self.assertIn('Nike', filters['exclude_brand'])
        self.assertEqual(filters['size'], 42)
        self.assertEqual(filters['max_price'], 120)
        self.assertTrue(all(product['brand'] != 'Nike' for product in result['products']))

    def test_contradictory_purpose_uses_fallback_without_executing_plan(self):
        self.recommend()
        self.opener.open.return_value = contract.response(contract.plan(transition='refine', changes=[
            contract.change('size', 43, 'EU 43'), contract.change('max_price', 100, 'до 100'),
            contract.change('use_case', 'Lifestyle', 'для бега')]))
        with patch('app.ai.turn_execution.execute_turn_plan', side_effect=AssertionError('Rejected plan executed')) as execute:
            result = chat(self.db, self.sid, 'Хочу Nike для бега, размер EU 43, до 100 долларов', 'ru', 'USD')
        execute.assert_not_called()
        self.assertEqual(result['mode'], 'fallback')
        filters = _sessions[self.sid]['filters']
        self.assertEqual(filters['brand'], 'Nike')
        self.assertEqual(filters['size'], 43)
        self.assertEqual(filters['max_price'], 100)
        self.assertEqual(filters['use_case'], 'Running')

    def setUp(self):
        self.fixture = fixtures.ShopperConversationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.env = patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'gsk_synthetic_stub_key'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = patch('app.ai.groq_provider.build_opener')
        self.opener = self.transport.start().return_value
        self.addCleanup(self.transport.stop)
        self.db, self.sid = self.fixture.db, self.fixture.sid

    def send(self, text, payload, language='ru'):
        self.opener.open.return_value = contract.response(payload)
        # Internal plans are backend data now, not model HTTP responses.
        with patch('app.ai.groq_provider.GroqTurnPlanner.plan', return_value=contract.TurnPlan.model_validate(payload)):
            result = chat(self.db, self.sid, text, language, 'USD')
        self.assertEqual(result['mode'], 'groq', result)
        ChatResponse.model_validate(result)
        self.assertEqual(verify_products(self.db, result['products']), [])
        self.assertEqual(verify_reasons(result['products']), [])
        self.assertEqual(verify_reply(result['reply'], result['products'], result['filters']), [])
        return result

    def recommend(self):
        return self.send('Тянет к Nike: ношу 42, потолок 120 долларов', contract.plan(intent='recommend', changes=[
            contract.change('brand', 'Nike', 'Nike'), contract.change('size', 42, 'ношу 42'),
            contract.change('max_price', 120, 'потолок 120'), contract.change('currency', 'USD', 'долларов')]))

    def locate(self):
        return self.send('Присматриваюсь к Nike Air Max Pulse', contract.plan(transition='new_task',
            actions=[contract.action(queries=['Nike Air Max Pulse'])], reply_plan=[contract.part('model_lookup')]))

    def select(self):
        return self.send('Мне ближе первая, остановимся на ней', contract.plan(selected_product_id=1,
            referenced_product_ids=[1], actions=[contract.action('get_product_details', [1])]))

    def test_free_wording_uses_plan_and_catalog_without_old_intent_gate(self):
        result = self.recommend()
        self.assertTrue(result['products'])
        self.assertTrue(all(item['brand'] == 'Nike' and 42 in item['available_sizes'] for item in result['products']))
        self.assertEqual(result['filters']['max_price'], 120)
        self.assertEqual(_sessions[self.sid]['dialogue']['intent'], 'recommend')

    def test_uk_open_recommendation_one_question_then_short_size(self):
        initial = self.send('Допоможете визначитися з парою?', contract.plan(intent='recommend', actions=[],
            question={'kind': 'need', 'reason': 'missing'}, reply_plan=[contract.part('clarification')]), 'uk')
        self.assertEqual(initial['products'], [])
        self.assertEqual(initial['reply'].count('?'), 1)
        self.send('Тоді Adidas', contract.plan(intent='recommend', changes=[contract.change('brand', 'Adidas', 'Adidas')],
            question={'kind': 'eu_size', 'reason': 'missing'}, reply_plan=[contract.part('clarification')]), 'uk')
        result = self.send('42', contract.plan(intent='recommend', changes=[contract.change('size', 42, '42')]), 'uk')
        self.assertEqual(result['filters']['brand'], 'Adidas')
        self.assertEqual(result['filters']['size'], 42)
        self.assertFalse(result['needs_clarification'])
        self.assertIn('За каталогом', result['reply'])

    def test_two_models_resolved_by_catalog_not_model_product_ids(self):
        self.recommend()
        result = self.send('Сопоставьте Nike Air Max Pulse с Adidas Superstar', contract.plan(intent='compare',
            actions=[contract.action(queries=['Nike Air Max Pulse', 'Adidas Superstar'])],
            reply_plan=[contract.part('comparison')]))
        self.assertEqual({item['name'] for item in result['products']}, {'Air Max Pulse', 'Superstar'})
        self.assertIn('97', result['reply'])
        self.assertIn('85', result['reply'])

    def test_budget_negation_color_and_removal_keep_other_preferences(self):
        self.recommend()
        self.send('Бюджет больше не нужен', contract.plan(transition='refine', changes=[
            contract.change('max_price', None, 'Бюджет больше не нужен', 'remove')]))
        result = self.send('Только не Nike', contract.plan(transition='refine', changes=[
            contract.change('brand', 'Nike', 'не Nike', 'exclude')]))
        self.assertTrue(result['products'])
        self.assertTrue(all(item['brand'] != 'Nike' for item in result['products']))
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)
        self.assertNotIn('max_price', _sessions[self.sid]['filters'])
        result = self.send('Хочется угольного оттенка', contract.plan(transition='refine', changes=[
            contract.change('color', 'Black', 'угольного оттенка')]))
        self.assertTrue(result['products'])
        self.assertTrue(all(item['color'] == 'Black' for item in result['products']))

    def test_selected_bookmark_survives_topic_change_restart_and_resume(self):
        self.recommend()
        self.select()
        self.send('Как погода на Марсе?', contract.plan(intent='clarification', transition='suspend', actions=[],
            reply_plan=[contract.part('acknowledge')]))
        self.send('Давайте вместо этого Adidas Superstar', contract.plan(transition='new_task',
            actions=[contract.action(queries=['Adidas Superstar'])], reply_plan=[contract.part('model_lookup')]))
        _sessions.pop(self.sid)
        result = self.send('Повернімося до моєї обраної пари', contract.plan(transition='resume', selected_product_id=1,
            actions=[contract.action('get_product_details', [1])], referenced_product_ids=[1]), 'uk')
        self.assertEqual([item['id'] for item in result['products']], [1])
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 120)
        self.assertEqual(_sessions[self.sid]['filters']['name_query'], 'Air Max Pulse')

    def test_price_stock_and_order_use_variant_and_never_create_request(self):
        self.recommend()
        self.select()
        result = self.send('Во сколько обойдётся моя пара?', contract.plan(intent='price',
            actions=[contract.action('get_product_details', [1])], reply_plan=[contract.part('price', [1])]))
        self.assertIn('97 USD', result['reply'])
        result = self.send('Мой EU ещё остался?', contract.plan(intent='availability',
            actions=[contract.action('check_stock', [1])], reply_plan=[contract.part('availability', [1])]))
        self.assertIn('2 пар', result['reply'])
        result = self.send('Готов оформить, что дальше?', contract.plan(intent='order', selected_product_id=1,
            actions=[contract.action('get_product_details', [1])], reply_plan=[contract.part('request_instructions', [1])]))
        self.assertIn('заявк', result['reply'].lower())
        self.assertEqual(self.db.scalars(select(ContactRequest)).all(), [])

    def test_price_filter_uses_variant_and_no_constraint_relaxation(self):
        result = self.send('Nike EU 42 до 98 долларов', contract.plan(intent='recommend', changes=[
            contract.change('brand', 'Nike', 'Nike'), contract.change('size', 42, 'EU 42'),
            contract.change('max_price', 98, 'до 98'), contract.change('currency', 'USD', 'долларов')]))
        self.assertEqual([item['id'] for item in result['products']], [1])
        result = self.send('Теперь до 90 долларов', contract.plan(transition='refine', changes=[contract.change('max_price', 90, 'до 90')]))
        self.assertEqual(result['products'], [])
        self.assertEqual(_sessions[self.sid]['filters']['brand'], 'Nike')
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)

    def test_free_budget_wording_does_not_need_a_parser_keyword(self):
        self.recommend()
        result = self.send('Могу выделить 110 на пару', contract.plan(transition='refine', changes=[
            contract.change('max_price',110,'Могу выделить 110 на пару')]))
        self.assertEqual(result['filters']['max_price'],110)
        self.assertEqual(result['filters']['size'],42)
        self.opener.open.return_value = contract.response(contract.plan(transition='refine',changes=[
            contract.change('max_price',100,'Бюджет от 100')]))
        result=chat(self.db,self.sid,'Бюджет от 100 долларов','ru','USD')
        self.assertEqual(result['mode'],'fallback')
        self.assertEqual(_sessions[self.sid]['filters']['max_price'],110)

    def test_missing_model_and_unknown_properties_no_automatic_substitution(self):
        result = self.send('Хочу Nike Phantom 999', contract.plan(transition='new_task',
            actions=[contract.action(queries=['Nike Phantom 999'])], reply_plan=[contract.part('model_lookup')]))
        self.assertEqual(result['products'], [])
        self.assertIn('Не нашёл', result['reply'])
        result = self.send('А насколько они дышащие?', contract.plan(intent='clarification', actions=[],
            unresolved_preferences=['дышащие'], question={'kind': 'model', 'reason': 'ambiguous'},
            reply_plan=[contract.part('unknown_attribute'), contract.part('clarification')]))
        self.assertEqual(result['products'], [])
        self.assertIn('Не буду предполагать', result['reply'])

    def test_ambiguous_answer_and_measurement_never_guess_eu(self):
        self.locate()
        self.send('42', contract.plan(intent='clarification', actions=[], question={'kind':'confirm_size','reason':'ambiguous'},
            reply_plan=[contract.part('clarification')]))
        result = self.send('так', contract.plan(changes=[contract.change('size',42,'так')]), 'uk')
        self.assertEqual(result['filters']['size'],42)
        self.assertFalse(result['needs_clarification'])
        self.send('Размер не важен', contract.plan(changes=[contract.change('size',None,'Размер не важен','remove')]))
        result = self.send('стелька 28,5 см', contract.plan(changes=[contract.change('measurement_value',28.5,'28,5 см'),
            contract.change('measurement_kind','insole_cm','стелька')]))
        self.assertNotIn('size', _sessions[self.sid]['filters'])
        self.assertIn('EU', result['reply'])

    def test_more_options_excludes_shown_products(self):
        first = self.recommend()
        next_result = self.send('Есть ещё что-нибудь в том же духе?', contract.plan(intent='recommend', reply_plan=[contract.part('more_results')]))
        self.assertFalse({item['id'] for item in first['products']} & {item['id'] for item in next_result['products']})

    def test_api_failures_and_missing_key_preserve_real_fallback_context(self):
        self.recommend()
        for error in (HTTPError('https://api.groq.com',429,'private',{},BytesIO()),
                      HTTPError('https://api.groq.com',503,'private',{},BytesIO()),URLError('private'),TimeoutError()):
            self.opener.open.side_effect=error
            result=chat(self.db,self.sid,'Есть мой размер?', 'ru','USD')
            self.assertEqual(result['mode'],'fallback')
            self.assertIn('EU 42', result['reply'])
            self.assertEqual(_sessions[self.sid]['filters']['max_price'],120)
        self.opener.open.reset_mock()
        with patch.dict('os.environ', {'GROQ_API_KEY':''}):
            self.assertEqual(chat(self.db,self.sid,'Есть мой размер?')['mode'],'fallback')
        self.opener.open.assert_not_called()

    def test_private_message_invalid_plan_and_executor_failure_do_not_leak_state(self):
        self.recommend()
        self.opener.open.reset_mock()
        result=chat(self.db,self.sid,'Телефон +380991234567', 'ru','USD')
        self.assertEqual(result['mode'],'fallback')
        self.opener.open.assert_not_called()
        self.opener.open.return_value=contract.response(contract.plan(selected_product_id=999))
        result=chat(self.db,self.sid,'Есть мой размер?', 'ru','USD')
        self.assertEqual(result['mode'],'fallback')
        self.assertEqual(_sessions[self.sid]['filters']['size'],42)
        original = deepcopy(_sessions[self.sid]['filters'])
        # Known selection but unrelated tool result: fails after changes to the private copy.
        self.opener.open.return_value=contract.response(contract.plan(transition='new_task', selected_product_id=1,
            actions=[contract.action('get_product_details',[3])]))
        result=chat(self.db,self.sid,'Есть мой размер?', 'ru','USD')
        self.assertEqual(result['mode'],'fallback')
        self.assertEqual(_sessions[self.sid]['filters'],original)
        self.assertIsNone(_sessions[self.sid]['clarification'].get('selected_product_id'))

    def test_unknown_variant_is_not_given_a_variant_price(self):
        self.locate()
        result=self.send('Мой размер EU 45',contract.plan(intent='availability',changes=[contract.change('size',45,'EU 45')],
            actions=[contract.action('get_product_details',[1])],reply_plan=[contract.part('availability',[1])]))
        self.assertIn('нет в наличии',result['reply'])
        self.assertIn('без привязки',result['reply'])

    def test_api_schema_and_chat_logging_accept_groq_mode(self):
        self.opener.open.return_value=contract.response(contract.understanding('locate', model_queries=['Nike Air Max Pulse']))
        result=send_message(ChatRequest(session_id=self.sid,message='Nike Air Max Pulse',display_currency='USD'),self.db)
        self.assertEqual(ChatResponse.model_validate(result).mode,'groq')
        self.assertEqual(self.db.scalar(select(ChatLog)).mode,'groq')
