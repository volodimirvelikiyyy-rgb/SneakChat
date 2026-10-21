"""Bounded HTTP stubs only. No external provider calls, production DB or live server."""

from copy import deepcopy
from io import BytesIO, StringIO
import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from pydantic import ValidationError

from app.ai.groq_provider import (
    GROQ_MODEL, GROQ_URL, MAX_RESPONSE_BYTES, GroqTurnPlanner, get_turn_plan_provider,
    plan_or_fallback, public_context,
)
from app.ai.providers import ProviderUnavailable, get_provider
from app.ai.turn_plan import TurnPlan, validate_plan_context
import test_shopper_conversation as fixtures


def setUpModule():
    global _network_guard
    _network_guard = patch('socket.socket.connect', side_effect=AssertionError('External network is forbidden in stub tests'))
    _network_guard.start()


def tearDownModule():
    _network_guard.stop()


def state():
    return {
        'filters': {'brand': 'Nike', 'size': 42, 'max_price': 120, 'currency': 'USD'},
        'shown_product_ids': [1, 2],
        'products': [{'id': 1, 'brand': 'Nike', 'name': 'Air Max Pulse'},
                     {'id': 2, 'brand': 'Adidas', 'name': 'Superstar'}],
        'clarification': {'selected_product_id': 1},
        'dialogue': {'selected_task': {'product_id': 1, 'filters': {'brand': 'Nike', 'size': 42}},
                     'last_question': None},
    }


def change(field, value, evidence, operation='set'):
    return {'field': field, 'value': value, 'evidence': evidence, 'operation': operation}


def action(tool='search_products', ids=(), queries=()):
    return {'tool': tool, 'product_ids': list(ids), 'model_queries': list(queries)}


def part(kind='catalog_results', ids=(), fields=('name',)):
    return {'kind': kind, 'product_ids': list(ids), 'fact_fields': list(fields)}


def plan(**updates):
    return {
        'intent': 'locate', 'transition': 'continue', 'changes': [], 'selected_product_id': None,
        'referenced_product_ids': [], 'actions': [action()], 'question': None,
        'reply_plan': [part()], 'unresolved_preferences': [], **updates,
    }


def understanding(intent='recommend', **updates):
    return {'intent': intent, 'changes': [], 'model_queries': [], 'unresolved_preferences': [], **updates}


def response(payload, finish='stop'):
    return BytesIO(json.dumps({'choices': [{'finish_reason': finish,
                                           'message': {'role': 'assistant', 'content': json.dumps(payload, ensure_ascii=False)}}]}).encode())


class ContractTests(unittest.TestCase):
    def test_brand_is_not_a_model_and_greeting_does_not_erase_purpose(self):
        context = public_context(state(), 'uk', 'USD')
        invalid = plan(changes=[change('brand', 'Nike', 'Nike'), change('size', 43.0, 'розмір 43')],
                       actions=[action(queries=['Nike'])])
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(invalid), 'Бажано Nike, розмір 43', context)
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(plan(
                changes=[change('name_query', 'Nike', 'Nike')])), 'Nike', context)
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(plan(intent='recommend')),
                                  'Привіт, шукаю кросівки на кожен день', context)

    def test_plan_cannot_omit_explicit_preferences(self):
        context = public_context({}, 'ru', 'USD')
        messages = ('Нужны Nike на каждый день', 'Хочу Nike', 'Размер EU 42',
                    'До 120 долларов', 'От 80 евро', 'Для бега',
                    'Потрібні Nike для бігу, розмір EU 42, до 120 доларів')
        for message in messages:
            with self.subTest(message=message), self.assertRaises(ValueError):
                validate_plan_context(TurnPlan.model_validate(plan(intent='recommend')), message, context)

    def test_preferences_may_already_be_in_state_but_not_be_contradicted(self):
        context = public_context({'filters': {'brand': 'Nike', 'size': 42, 'use_case': 'Lifestyle',
                                             'max_price': 120, 'currency': 'USD'}}, 'ru', 'USD')
        message = 'Nike на каждый день, размер EU 42, до 120 долларов'
        validate_plan_context(TurnPlan.model_validate(plan()), message, context)
        # A literal substring is evidence, but not consent to change the brand.
        conflicting = plan(changes=[change('brand', 'Adidas', 'Adidas')])
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(conflicting),
                                  'Не Adidas, нужны Nike на каждый день', context)

    def test_each_compound_constraint_must_be_covered_and_agree(self):
        context = public_context({}, 'ru', 'USD')
        messages = ('Хочу Nike для бега, размер EU 42, до 120 долларов',
                    'Хочу Nike для бігу, розмір EU 42, до 120 доларів')
        changes = [change('brand', 'Nike', 'Nike'), change('size', 42, 'EU 42'),
                   change('max_price', 120, 'до 120'), change('currency', 'USD', '120'),
                   change('use_case', 'Running', 'Nike')]
        for message in messages:
            valid = deepcopy(changes)
            valid[3]['evidence'] = 'долларов' if 'долларов' in message else 'доларів'
            valid[4]['evidence'] = 'для бега' if 'бега' in message else 'для бігу'
            validate_plan_context(TurnPlan.model_validate(plan(changes=valid)), message, context)
            for index in range(len(valid)):
                with self.subTest(message=message, omitted=valid[index]['field']), self.assertRaises(ValueError):
                    validate_plan_context(TurnPlan.model_validate(plan(changes=valid[:index] + valid[index + 1:])),
                                          message, context)
            for index, wrong in enumerate(('Adidas', 43, 150, 'EUR', 'Lifestyle')):
                contradictory = deepcopy(valid)
                contradictory[index]['value'] = wrong
                with self.subTest(message=message, contradicted=valid[index]['field']), self.assertRaises(ValueError):
                    validate_plan_context(TurnPlan.model_validate(plan(changes=contradictory)), message, context)

    def test_negations_must_survive_the_plan(self):
        messages = ('Только не Nike', 'Не для бега', 'Не чёрные', 'Тільки не Adidas', 'Не 43')
        for message in messages:
            with self.subTest(message=message), self.assertRaises(ValueError):
                validate_plan_context(TurnPlan.model_validate(plan()), message, public_context({}, 'ru', 'USD'))
        excluded = {'filters': {'exclude_brand': ['Nike']}}
        validate_plan_context(TurnPlan.model_validate(plan()), 'Только не Nike', public_context(excluded, 'ru', 'USD'))
        contradictory = plan(changes=[change('brand', 'Nike', 'не Nike', 'exclude')],
                             actions=[action(queries=['Nike'])])
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(contradictory), 'Только не Nike', public_context({}, 'ru', 'USD'))

    def test_strict_schema_every_property_required_and_no_open_objects(self):
        schema = TurnPlan.model_json_schema()

        def check(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertIs(node.get('additionalProperties'), False)
                    self.assertEqual(set(node['properties']), set(node['required']))
                self.assertNotIn('default', node)
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)
        check(schema)

    def test_fact_assignment_write_tools_and_free_text_are_not_in_contract(self):
        invalid = [plan(price=1), plan(stock=10), plan(sku='fake'),
                   plan(reply_plan=[{**part(), 'text': 'Удобные, дышащие, официальная гарантия'}]),
                   plan(actions=[action('create_contact_request')]), plan(actions=[action('execute_sql')])]
        invalid += [plan(changes=[change(field, 10, '10')]) for field in ('price', 'stock', 'sku', 'materials', 'comfort')]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                TurnPlan.model_validate(payload)

    def test_only_one_question_and_no_conflicting_slot_updates(self):
        for payload in (plan(question=[{'kind': 'eu_size', 'reason': 'missing'}]),
                        plan(changes=[change('max_price', 100, '100'), change('max_price', 120, '120')]),
                        plan(changes=[change('size', True, '42')]),
                        plan(changes=[change('size', 28.5, '28,5 см')])):
            with self.assertRaises(ValidationError):
                TurnPlan.model_validate(payload)

    def test_reject_invented_references_and_unsupported_attributes(self):
        for payload in (plan(selected_product_id=999), plan(referenced_product_ids=[999]),
                        plan(actions=[action('get_product_details', [999])]),
                        plan(reply_plan=[part(ids=[999])])):
            with self.assertRaises(ValueError):
                validate_plan_context(TurnPlan.model_validate(payload), 'Nike', public_context(state(), 'ru', 'USD'))
        with self.assertRaises(ValidationError):
            TurnPlan.model_validate(plan(changes=[change('feature', 'breathable', 'дышащие')]))

    def test_evidence_and_numeric_role_are_verified(self):
        invalid = [('До 100 долларов', change('max_price', 50, '100')),
                   ('До 42 долларов', change('size', 42, '42')),
                   ('Хочу Nike', change('brand', 'Nike', 'Adidas')),
                   ('Хочу Nike', change('brand', 'Nike', 'Nike', 'exclude')),
                   ('Хочу дышащие', change('feature', 'water_resistant', 'дышащие')),
                   ('Хочу Nike', change('name_query', 'Phantom 999', 'Nike'))]
        for text, item in invalid:
            with self.subTest(text=text, field=item['field']), self.assertRaises(ValueError):
                validate_plan_context(TurnPlan.model_validate(plan(changes=[item])), text, public_context(state(), 'ru', 'USD'))

    def test_resume_requires_the_actual_selected_product(self):
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(plan(transition='resume', selected_product_id=2)),
                                  'Вернуться к выбранной паре', public_context(state(), 'ru', 'USD'))

    def test_known_slots_and_budget_conflicts_cannot_be_ignored(self):
        context = public_context(state(), 'ru', 'USD')
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(plan(question={'kind': 'eu_size', 'reason': 'missing'})), 'Nike', context)
        conflict = plan(changes=[change('min_price', 150, 'от 150')])
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(conflict), 'от 150 долларов', context)
        conflict['question'] = {'kind': 'budget_boundary', 'reason': 'conflict'}
        validate_plan_context(TurnPlan.model_validate(conflict), 'от 150 долларов', context)

    def test_measurement_and_bare_size_use_existing_typed_context(self):
        context = public_context(state(), 'ru', 'USD')
        payload = plan(changes=[change('measurement_value', 28.5, '28,5 см'),
                                change('measurement_kind', 'insole_cm', 'стелька')])
        validate_plan_context(TurnPlan.model_validate(payload), 'стелька 28,5 см', context)
        context['pending_question'] = 'eu_size'
        validate_plan_context(TurnPlan.model_validate(plan(changes=[change('size', 43, '43')])), '43', context)

    def test_removal_needs_explicit_consent(self):
        payload = TurnPlan.model_validate(plan(changes=[change('max_price', None, 'цена не важна', 'remove')]))
        validate_plan_context(payload, 'цена не важна', public_context(state(), 'ru', 'USD'))
        with self.assertRaises(ValueError):
            validate_plan_context(TurnPlan.model_validate(plan(changes=[change('max_price', None, 'Nike', 'remove')])),
                                  'Nike', public_context(state(), 'ru', 'USD'))


class AdapterTests(unittest.TestCase):
    def test_legacy_executable_plans_are_not_a_public_model_contract(self):
        fixture = json.loads((Path(__file__).parent / 'data' / 'groq_rejected_plans.json').read_text(encoding='utf-8'))
        for case in fixture['cases']:
            self.opener.open.return_value = response(case['plan'])
            with self.subTest(step=case['step']), self.assertRaisesRegex(ProviderUnavailable, '^invalid_plan$'):
                self.provider.plan(case['state'], case['message'], 'uk')

    def test_parser_confirmed_evidence_normalization_never_changes_values(self):
        for language, message in (('uk', 'Nike, розмір 43, до 150 доларів'),
                                  ('ru', 'Nike, размер 43, до 150 долларов')):
            self.opener.open.return_value = response(understanding())
            result = self.provider.plan({}, message, language)
            self.assertEqual({c.field: c.value for c in result.changes},
                             {'brand': 'Nike', 'size': 43, 'max_price': 150, 'currency': 'USD'})
            for field, wrong in (('size', 44), ('max_price', 120), ('currency', 'EUR')):
                self.opener.open.return_value = response(understanding(changes=[change(field, wrong, message)]))
                with self.subTest(language=language, field=field), self.assertRaises(ProviderUnavailable):
                    self.provider.plan({}, message, language)

    def test_soft_preference_reassertion_does_not_become_hard_brand(self):
        original = {'filters': {'size': 43}, 'preferences': {'brand': 'Nike'}}
        self.opener.open.return_value = response(understanding(changes=[change('brand', 'Nike', 'Nike')]))
        result = self.provider.plan(original, 'До 150 доларів', 'uk')
        self.assertNotIn('brand', [c.field for c in result.changes])
        self.assertEqual({c.field: c.value for c in result.changes}, {'max_price': 150, 'currency': 'USD'})
        self.assertNotIn('brand', original['filters'])

    def test_reasserted_context_is_not_a_new_change_but_conflicts_are_rejected(self):
        context = {'filters': {'use_case': 'Lifestyle'}}
        self.opener.open.return_value = response(understanding(changes=[change('use_case', 'Lifestyle', 'Lifestyle')]))
        result = self.provider.plan(context, 'Бажано Nike, розмір 43', 'uk')
        self.assertEqual({c.field for c in result.changes}, {'brand', 'size'})
        self.opener.open.return_value = response(understanding(changes=[change('use_case', 'Running', 'Running')]))
        with self.assertRaises(ProviderUnavailable):
            self.provider.plan(context, 'Бажано Nike, розмір 43', 'uk')

    def setUp(self):
        self.transport = patch('app.ai.groq_provider.build_opener')
        self.opener = self.transport.start().return_value
        self.addCleanup(self.transport.stop)
        self.provider = GroqTurnPlanner('gsk_synthetic_stub_key')

    def test_requested_ru_uk_scenarios_through_http_stub(self):
        for language, text, payload in (
            ('ru', 'Хочу Nike размер 42 до 120 долларов', understanding()),
            ('uk', 'Хочу Adidas розмір 42 до 100 доларів', understanding()),
            ('ru', 'Сравни Nike Air Max Pulse и Adidas Superstar', understanding('compare',
                model_queries=['Nike Air Max Pulse', 'Adidas Superstar'])),
            ('uk', 'Повернімося до обраної пари', understanding('resume')),
            ('ru', 'Хочу дышащие кроссовки', understanding('clarification', unresolved_preferences=['дышащие'])),
            ('ru', 'Найди Nike Phantom 999', understanding('locate', model_queries=['Nike Phantom 999'])),
        ):
            with self.subTest(text=text):
                original = state()
                snapshot = deepcopy(original)
                self.opener.open.return_value = response(payload)
                result = self.provider.plan(original, text, language)
                self.assertIsInstance(result, TurnPlan)
                self.assertEqual(original, snapshot)
                request = self.opener.open.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.full_url, GROQ_URL)
                self.assertEqual(body['model'], GROQ_MODEL)
                self.assertTrue(body['response_format']['json_schema']['strict'])
                self.assertEqual(set(body['response_format']['json_schema']['schema']['properties']),
                                 {'intent', 'changes', 'model_queries', 'unresolved_preferences'})
                self.assertLessEqual(self.opener.open.call_args.kwargs['timeout'], 8)

    def test_contact_rows_raw_history_and_internal_keys_never_enter_payload(self):
        original = state()
        private = 'private.person@example.invalid'
        original.update(history=[{'role': 'user', 'content': private}], contact=private, api_key=private,
                        customer={'name': 'Приватный Клиент', 'phone': '+380991234567'},
                        contact_requests=[{'contact': private}])
        original['dialogue']['last_question'] = {'kind': 'color', 'text': private}
        original['filters']['contact'] = private
        original['products'][0].update(sku='PRIVATE_SKU', variants=[{'stock': 999}], description=private)
        self.opener.open.return_value = response(understanding())
        self.provider.plan(original, 'Хочу Nike')
        wire = self.opener.open.call_args.args[0].data.decode('utf-8')
        for forbidden in (private, '+380991234567', 'Приватный Клиент', 'PRIVATE_SKU', 'gsk_synthetic_stub_key'):
            self.assertNotIn(forbidden, wire)

    def test_sensitive_messages_fail_closed_before_http(self):
        for text in ('Мой телефон +380 99 123 45 67', 'Напишите @private_person', 'x@example.com',
                     'Меня зовут Иван', 'Іван Петренко шукає Nike', 'token=private',
                     'gsk_synthetic_stub_key', 'Bearer private', 'Улица Примерная, 12'):
            with self.subTest(text=text), self.assertRaisesRegex(ProviderUnavailable, '^privacy_blocked$'):
                self.provider.plan(state(), text)
        self.opener.open.assert_not_called()

    def test_private_values_in_allowed_state_slots_also_block_http(self):
        original = state()
        original['filters']['name_query'] = 'contact@example.com'
        with self.assertRaisesRegex(ProviderUnavailable, '^privacy_blocked$'):
            self.provider.plan(original, 'Хочу Nike')
        self.opener.open.assert_not_called()

    def test_preferences_and_exclusions_survive_public_projection(self):
        original = state()
        original['preferences'] = {'brand': 'Adidas', 'price_target': {'amount': 100, 'currency': 'USD'},
                                   'price_order': 'high', 'private_note': 'customer@example.com'}
        original['filters'].update(exclude_brand=['Puma'], exclude_attributes={'color': ['Black']})
        context = public_context(original, 'uk', 'UAH')
        self.assertEqual(context['preferences']['price_target'], {'amount': 100, 'currency': 'USD'})
        self.assertEqual(context['exclude_brand'], ['Puma'])
        self.assertEqual(context['exclude_attributes'], {'color': ['Black']})
        self.assertNotIn('private_note', context['preferences'])

    def test_invalid_context_or_header_key_fails_without_http(self):
        original = state()
        original['dialogue'] = ['corrupt']
        with self.assertRaisesRegex(ProviderUnavailable, '^invalid_context$'):
            self.provider.plan(original, 'Nike')
        with self.assertRaisesRegex(ProviderUnavailable, '^invalid_key$'):
            GroqTurnPlanner('bad\nheader').plan(state(), 'Nike')
        self.opener.open.assert_not_called()

    def test_missing_key_and_opt_in_factory(self):
        with patch.dict(os.environ, {'GROQ_API_KEY': '', 'AI_PROVIDER': 'groq'}):
            self.assertIsNone(get_turn_plan_provider())
            with self.assertRaisesRegex(ProviderUnavailable, '^missing_key$'):
                GroqTurnPlanner().plan(state(), 'Nike')
        with patch.dict(os.environ, {'GROQ_API_KEY': 'gsk_synthetic_stub_key', 'AI_PROVIDER': 'disabled'}):
            self.assertIsNone(get_turn_plan_provider())
        with patch.dict(os.environ, {'GROQ_API_KEY': 'gsk_synthetic_stub_key', 'AI_PROVIDER': 'groq'}):
            self.assertIsInstance(get_turn_plan_provider(), GroqTurnPlanner)
            self.assertIsNone(get_provider())  # Existing chat is deliberately not wired to TurnPlan.
        self.opener.open.assert_not_called()

    def test_bounded_context_response_and_timeout(self):
        with self.assertRaisesRegex(ProviderUnavailable, 'message_too_large'):
            self.provider.plan(state(), 'Nike ' * 500)
        original = state()
        original['shown_product_ids'] = list(range(1, 100))
        with self.assertRaisesRegex(ProviderUnavailable, 'context_too_large'):
            self.provider.plan(original, 'Nike')
        self.opener.open.assert_not_called()
        self.opener.open.return_value = BytesIO(b'x' * (MAX_RESPONSE_BYTES + 2))
        with self.assertRaisesRegex(ProviderUnavailable, 'response_too_large'):
            self.provider.plan(state(), 'Nike')
        self.assertEqual(GroqTurnPlanner('stub', timeout=1000).timeout, 15)

    def test_api_errors_have_no_retries_or_sensitive_error_output(self):
        for failure, reason in [
            (HTTPError(GROQ_URL, 429, 'gsk_synthetic_stub_key', {}, BytesIO(b'private response')), 'rate_limited'),
            (HTTPError(GROQ_URL, 503, 'private', {}, BytesIO(b'private response')), 'upstream_error'),
            (HTTPError(GROQ_URL, 401, 'private', {}, BytesIO(b'private response')), 'http_error'),
            (TimeoutError('private'), 'timeout'), (URLError('private'), 'network_error'),
        ]:
            with self.subTest(reason=reason):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = failure
                log = StringIO()
                handler = logging.StreamHandler(log)
                logging.getLogger().addHandler(handler)
                try:
                    with self.assertRaisesRegex(ProviderUnavailable, '^'+reason+'$'):
                        self.provider.plan(state(), 'Nike')
                finally:
                    logging.getLogger().removeHandler(handler)
                if reason == 'rate_limited':
                    self.assertIn('Groq HTTP 429', log.getvalue())
                    self.assertNotIn('gsk_synthetic_stub_key', log.getvalue())
                    self.assertNotIn('private response', log.getvalue())
                elif isinstance(failure, HTTPError) and failure.code == 400:
                    self.assertIn('Groq HTTP 400', log.getvalue())
                    self.assertNotIn('private', log.getvalue())
                else:
                    self.assertEqual(log.getvalue(), '')
                self.assertEqual(self.opener.open.call_count, 1)

    def test_bad_structured_output_logs_safe_code_without_body_or_retry(self):
        body = BytesIO(json.dumps({'error': {'type': 'invalid_request_error',
            'code': 'json_validate_failed', 'message': 'private user text',
            'failed_generation': 'private generated content'}}).encode())
        self.opener.open.side_effect = HTTPError(GROQ_URL, 400, 'private', {}, body)
        with self.assertLogs('uvicorn.error', level='WARNING') as captured:
            with self.assertRaisesRegex(ProviderUnavailable, '^http_error$'):
                self.provider.plan(state(), 'Nike')
        metadata = json.loads(captured.records[0].getMessage().split('Groq HTTP 400 ', 1)[1])
        self.assertEqual(metadata['error_code'], 'json_validate_failed')
        self.assertEqual(metadata['status'], 400)
        self.assertNotIn('private', captured.records[0].getMessage())
        self.opener.open.assert_called_once()
        self.assertTrue(body.closed)

    def test_rate_limit_logs_only_quota_headers_and_safe_error_identifiers(self):
        headers = {'Retry-After': '12', 'x-ratelimit-limit-requests': '1000',
                   'x-ratelimit-remaining-requests': '998', 'x-ratelimit-limit-tokens': '8000',
                   'x-ratelimit-remaining-tokens': '0', 'x-ratelimit-reset-tokens': '2m59.56s',
                   'x-ratelimit-reset-requests': '23h1m', 'Authorization': 'Bearer private-key'}
        body = BytesIO(json.dumps({'error': {'type': 'tokens', 'code': 'rate_limit_exceeded',
                                            'message': 'private customer message'}}).encode())
        self.opener.open.side_effect = HTTPError(GROQ_URL, 429, 'private reason', headers, body)
        with self.assertLogs('uvicorn.error', level='WARNING') as captured:
            with self.assertRaisesRegex(ProviderUnavailable, '^rate_limited$'):
                self.provider.plan(state(), 'Nike')
        self.assertEqual(len(captured.records), 1)
        metadata = json.loads(captured.records[0].getMessage().split('Groq HTTP 429 ', 1)[1])
        self.assertEqual(metadata['model'], GROQ_MODEL)
        self.assertEqual(metadata['error_type'], 'tokens')
        self.assertEqual(metadata['error_code'], 'rate_limit_exceeded')
        self.assertEqual(metadata['headers']['retry-after'], '12')
        for name, value in headers.items():
            if name.lower().startswith('x-ratelimit-'):
                self.assertEqual(metadata['headers'][name.lower()], value)
        self.assertNotIn('private', captured.records[0].getMessage())
        self.opener.open.assert_called_once()
        self.assertTrue(body.closed)

    def test_rate_limit_redacts_untrusted_values_and_bounds_error_body(self):
        private = 'gsk_synthetic_stub_key private.person@example.invalid'
        for detail in ({'type': private, 'code': private, 'message': private}, None):
            with self.subTest(detail=detail):
                self.opener.open.reset_mock()
                raw = json.dumps({'error': detail}).encode() if detail else b'x' * 5000
                body = Mock(wraps=BytesIO(raw))
                self.opener.open.side_effect = HTTPError(GROQ_URL, 429, private, {
                    'retry-after': private, 'x-ratelimit-remaining-tokens': '0\n' + private,
                    'x-ratelimit-' + private: '123', 'Set-Cookie': private}, body)
                with self.assertLogs('uvicorn.error', level='WARNING') as captured:
                    with self.assertRaisesRegex(ProviderUnavailable, '^rate_limited$'):
                        self.provider.plan(state(), 'Nike')
                message = captured.records[0].getMessage()
                self.assertNotIn('gsk_', message)
                self.assertNotIn('private.person', message)
                self.assertIn('redacted', message)
                self.assertNotIn('Set-Cookie', message)
                body.read.assert_called_once_with(4096)
                self.opener.open.assert_called_once()

    def test_bad_envelope_truncated_json_and_invented_facts_are_rejected(self):
        for wire in (b'not-json', b'{}', json.dumps({'choices': []}).encode(),
                     response(plan(), finish='length').read(), response(plan(price=1)).read(),
                     response(plan(selected_product_id=999)).read()):
            self.opener.open.return_value = BytesIO(wire)
            with self.assertRaisesRegex(ProviderUnavailable, '^invalid_plan$'):
                self.provider.plan(state(), 'Nike')

    def test_invalid_public_slots_are_rejected_before_transport(self):
        for field, value in (('size', True), ('name_query', 'x' * 121), ('brand', {'private': 'data'})):
            original = state()
            original['filters'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ProviderUnavailable, '^invalid_context$'):
                self.provider.plan(original, 'Nike')
        self.opener.open.assert_not_called()

    def test_unexpected_role_refusal_and_tool_calls_are_rejected(self):
        for update in ({'role': 'user'}, {'refusal': 'refused'}, {'tool_calls': [{'id': 'call'}]}):
            wire = json.loads(response(understanding()).read())
            wire['choices'][0]['message'].update(update)
            self.opener.open.return_value = BytesIO(json.dumps(wire).encode())
            with self.subTest(update=update), self.assertRaisesRegex(ProviderUnavailable, '^invalid_plan$'):
                self.provider.plan(state(), 'Nike')


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ShopperConversationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.transport = patch('app.ai.groq_provider.build_opener')
        self.opener = self.transport.start().return_value
        self.addCleanup(self.transport.stop)

    def test_api_failure_runs_real_existing_fallback_and_keeps_session(self):
        self.fixture.start_nike()
        from app.services.chat import _sessions
        context = deepcopy(_sessions[self.fixture.sid])
        for failure in (HTTPError(GROQ_URL, 429, 'private', {}, BytesIO()), URLError('private')):
            self.opener.open.side_effect = failure
            outcome = plan_or_fallback(GroqTurnPlanner('stub'), context, 'Есть мой размер?',
                                       fallback=lambda: self.fixture.send('Есть мой размер?'))
            self.assertEqual(outcome.mode, 'fallback')
            self.assertIsNone(outcome.plan)
            self.assertEqual(outcome.fallback_result['mode'], 'fallback')
            self.assertIn('EU 42', outcome.fallback_result['reply'])
            self.assertEqual(_sessions[self.fixture.sid]['filters']['max_price'], 120)
            self.assertEqual(_sessions[self.fixture.sid]['filters']['brand'], 'Nike')

    def test_no_key_or_private_message_uses_real_fallback_without_http(self):
        for provider, text in ((None, 'Nike размер 42'), (GroqTurnPlanner('stub'), 'Телефон +380991234567')):
            outcome = plan_or_fallback(provider, state(), text, fallback=lambda: self.fixture.send(text))
            self.assertEqual(outcome.mode, 'fallback')
            self.assertEqual(outcome.fallback_result['mode'], 'fallback')
        self.opener.open.assert_not_called()

    def test_success_is_only_a_plan_not_tool_execution_or_session_write(self):
        self.opener.open.return_value = response(understanding())
        fallback = Mock()
        original = state()
        before = deepcopy(original)
        outcome = plan_or_fallback(GroqTurnPlanner('stub'), original, 'Nike', fallback=fallback)
        self.assertEqual(outcome.mode, 'planned')
        self.assertIsInstance(outcome.plan, TurnPlan)
        self.assertIsNone(outcome.fallback_result)
        self.assertEqual(original, before)
        fallback.assert_not_called()
