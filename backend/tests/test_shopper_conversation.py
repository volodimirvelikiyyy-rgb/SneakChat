"""Short buyer follow-ups against an isolated, synthetic catalog; no HTTP or LLM."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.grounding import verify_products, verify_reasons, verify_reply
from app.catalog import set_attribute_values, sync_attributes
from app.catalog_rules import SNEAKER_CONFIG
from app.database import Base, validate_test_database_url
from app.models import Product, Variant
from app.services.chat import _sessions, chat
from app.services.chat_sessions import load_session_state


class ShopperConversationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'AI_PROVIDER': 'disabled'})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory(prefix='sneakchat-shopper-')
        url = 'sqlite:///' + (Path(self.temp.name) / 'tests.sqlite3').as_posix()
        self.engine = create_engine(validate_test_database_url(url))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        attrs = sync_attributes(self.db, SNEAKER_CONFIG)
        self.products = {}
        rows = [
            ('Nike', 'Air Max Pulse', 100, 'White', 'Walking', ((42, 2, 97), (43, 2, 100))),
            ('Nike', 'Dunk Low Retro', 110, 'Black', 'Lifestyle', ((42, 0, 110), (43, 2, 110))),
            ('Nike', "Air Force 1 '07", 115, 'Black', 'Lifestyle', ((42, 3, 115), (43, 2, 115))),
            ('Nike', 'Winflo 11', 120, 'Black', 'Running', ((42, 0, 120), (43, 2, 120))),
            ('Adidas', 'Superstar', 85, 'White', 'Lifestyle', ((42, 4, 85),)),
            ('Adidas', 'Samba OG', 90, 'Black', 'Lifestyle', ((42, 1, 90),)),
            ('Puma', 'Palermo', 80, 'Black', 'Lifestyle', ((42, 1, 80),)),
        ]
        for brand, name, price, color, purpose, variants in rows:
            product = Product(brand=brand, name=name, category='Sneakers', base_price=price, currency='USD')
            set_attribute_values(product, {'color': color, 'purpose': purpose, 'gender': 'Unisex'}, attrs)
            for size, stock, variant_price in variants:
                variant = Variant(price=variant_price, stock=stock, sku=f'{name}-{size}')
                set_attribute_values(variant, {'size': size}, attrs)
                product.variants.append(variant)
            self.db.add(product)
            self.products[name] = product
        self.db.commit()
        self.sid = 'shopper-' + Path(self.temp.name).name

    def tearDown(self):
        _sessions.pop(self.sid, None)
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()
        self.env.stop()

    def send(self, message, language='ru'):
        result = chat(self.db, self.sid, message, language, 'USD')
        self.assertEqual(result['mode'], 'fallback')
        self.assertEqual(verify_products(self.db, result['products']), [])
        self.assertEqual(verify_reasons(result['products']), [])
        self.assertEqual(verify_reply(result['reply'], result['products'], result['filters']), [])
        return result

    def start_nike(self, size=42):
        return self.send(f'Хочу Nike до 120 долларов, размер {size}')

    def test_short_selection_help_after_model_question_shows_real_options_ru_uk(self):
        for language, messages in (('uk', ('варіанті', 'підскажіть', 'так допоможіть')),
                                   ('ru', ('варианты', 'подскажите', 'да помогите'))):
            self.sid += '-' + language
            self.send('розмір ціна?' if language == 'uk' else 'размер цена?', language)
            for message in messages:
                result = self.send(message, language)
                self.assertTrue(result['products'], result)
                self.assertFalse(result['needs_clarification'])
                self.assertNotIn('name_query', _sessions[self.sid]['filters'])
                self.assertNotIn('Я можу допомогти з каталогом', result['reply'])

    def test_selection_help_keeps_constraints_and_does_not_capture_other_topics(self):
        self.start_nike()
        original = dict(_sessions[self.sid]['filters'])
        for message in ('так допоможіть', 'подскажите'):
            result = self.send(message)
            self.assertTrue(result['products'])
            self.assertEqual(_sessions[self.sid]['filters'], original)
        result = self.send('подскажите Adidas EU 43')
        self.assertEqual(result['filters']['brand'], 'Adidas')
        self.assertEqual(result['filters']['size'], 43)
        self.assertEqual(result['filters']['max_price'], 120)
        from app.services.dialogue_state import selection_help_request
        for message in ('подскажите погоду', 'допоможіть написати лист', 'так', 'нет вариантов', 'варианты кредита'):
            self.assertFalse(selection_help_request(message), message)
        saved = dict(_sessions[self.sid]['filters'])
        self.send('подскажите погоду')
        self.assertEqual(_sessions[self.sid]['filters'], saved)

    def test_now_brand_switch_preserves_or_replaces_size_without_losing_context(self):
        for language, connector in (('ru', 'Теперь'), ('uk', 'Тепер')):
            with self.subTest(language=language):
                self.sid += '-' + language
                self.send('Adidas Superstar, EU 42, до 120 USD', language)
                switched = self.send(f'{connector} Nike', language)
                self.assertEqual(switched['filters']['brand'], 'Nike')
                self.assertEqual(switched['filters']['size'], 42)
                self.assertEqual(switched['filters']['max_price'], 120)
                self.assertNotIn('name_query', _sessions[self.sid]['filters'])
                self.assertTrue(switched['products'])
                self.assertTrue(all(product['brand'] == 'Nike' for product in switched['products']))
                message = (f'{connector} Nike для бега, EU 43, до 100 долларов' if language == 'ru'
                           else f'{connector} Nike для бігу, EU 43, до 100 доларів')
                updated = self.send(message, language)
                for field, value in {'brand': 'Nike', 'size': 43, 'max_price': 100, 'use_case': 'Running'}.items():
                    self.assertEqual(updated['filters'][field], value)
                saved = dict(_sessions[self.sid]['filters'])
                self.send(f'{connector} какая погода?' if language == 'ru' else f'{connector} яка погода?', language)
                self.assertEqual(_sessions[self.sid]['filters'], saved)
                compared = self.send('Сравни Nike Air Max Pulse и Adidas Superstar' if language == 'ru'
                                     else 'Порівняй Nike Air Max Pulse і Adidas Superstar', language)
                self.assertEqual({product['brand'] for product in compared['products']}, {'Nike', 'Adidas'})
                self.assertEqual(_sessions[self.sid]['filters']['size'], 43)
                self.assertEqual(_sessions[self.sid]['filters']['max_price'], 100)

    def test_neutral_greetings_and_open_recommendation(self):
        self.assertEqual(self.send('Здравствуйте')['reply'], 'Здравствуйте! Чем могу вам помочь?')
        result = self.send('Посоветуй кроссовки')
        self.assertEqual(result['products'], [])
        self.assertEqual(result['reply'], 'Для чего ищете пару — на каждый день или для спорта?')

    def test_opening_follows_intent_and_does_not_repeat_unanswered_question(self):
        for language, recommendation in (('ru', 'Посоветуй кроссовки'), ('uk', 'Порадь кросівки')):
            self.sid += '-' + language
            greeting = self.send('Вітаю!' if language == 'uk' else 'Здравствуйте.', language)
            self.assertIn('допомогти' if language == 'uk' else 'помочь', greeting['reply'])
            self.assertFalse(greeting['needs_clarification'])
            first = self.send(recommendation, language)
            self.assertIn('Для чого' if language == 'uk' else 'Для чего', first['reply'])
            self.assertEqual(first['reply'].count('?'), 1)
            repeated = self.send(recommendation, language)
            self.assertTrue(repeated['products'])
            self.assertNotEqual(repeated['reply'], first['reply'])
            self.assertFalse(repeated['needs_clarification'])
            self.sid += '-open'
            opened = self.send('Кросівки' if language == 'uk' else 'Кроссовки', language)
            self.assertIn('конкретну модель' if language == 'uk' else 'конкретную модель', opened['reply'])

    def test_greeting_preserves_pending_size_and_known_preferences(self):
        for language in ('ru', 'uk'):
            self.sid += '-' + language
            self.send('Nike', language)
            self.assertEqual(_sessions[self.sid]['dialogue']['last_question']['kind'], 'eu_size')
            original = dict(_sessions[self.sid]['filters'])
            greeted = self.send('Вітаю!' if language == 'uk' else 'Привет!', language)
            self.assertEqual(greeted['reply'], 'Вітаю! Чим можу вам допомогти?' if language == 'uk'
                             else 'Здравствуйте! Чем могу вам помочь?')
            self.assertEqual(_sessions[self.sid]['filters'], original)
            self.assertEqual(_sessions[self.sid]['dialogue']['last_question']['kind'], 'eu_size')
            sized = self.send('42', language)
            self.assertEqual(sized['filters']['size'], 42)
            self.assertEqual(sized['filters']['brand'], 'Nike')

    def test_budget_and_specific_model_can_show_catalog_without_size_interview(self):
        for language in ('ru', 'uk'):
            for message in ('Nike Air Max Pulse', 'EU 42', 'Nike до 120 USD'):
                self.sid += '-specific'
                result = self.send(message, language)
                self.assertTrue(result['products'], result)
                self.assertFalse(result['needs_clarification'])
                self.assertNotIn('need', _sessions[self.sid]['dialogue']['missing'])
            self.sid += '-budget'
            result = self.send('Кросівки до 120 USD' if language == 'uk' else 'Кроссовки до 120 USD', language)
            self.assertTrue(result['products'])
            self.assertEqual(result['filters']['max_price'], 120)
            self.assertFalse(result['needs_clarification'])


    def test_short_fact_questions_without_model_ask_for_model_ru_uk(self):
        for language, messages in (
            ('ru', ('размер цена?', 'цена?', 'какие размеры есть?', 'наличие?', 'сколько стоят?')),
            ('uk', ('розмір ціна?', 'ціна?', 'які розміри є?', 'наявність?', 'скільки коштують?')),
        ):
            self.sid += '-' + language
            self.send('Посоветуй кроссовки', language)
            for message in messages:
                with self.subTest(language=language, message=message):
                    result = self.send(message, language)
                    self.assertEqual(result['reply'], 'Какую модель кроссовок вы ищете?' if language == 'ru'
                                     else 'Яку модель кросівок ви шукаєте?')
                    self.assertTrue(result['needs_clarification'])
                    self.assertEqual(result['products'], [])
                    self.assertNotIn('name_query', _sessions[self.sid]['filters'])

    def test_short_fact_questions_use_selected_model_and_fresh_variant_facts(self):
        for language, message in (('ru', 'цена размеры наличие?'), ('uk', 'ціна розміри наявність?')):
            self.sid += '-' + language
            self.send('Найди Nike Air Max Pulse, размер 42', language)
            self.send('Хочу именно эту пару', language)
            original = dict(_sessions[self.sid]['filters'])
            # Only the temporary fixture changes: the reply must re-read current facts.
            variant = self.products['Air Max Pulse'].variants[0]
            variant.price, variant.stock = 96, 1
            self.db.commit()
            _sessions.pop(self.sid)
            result = self.send(message, language)
            self.assertEqual([p['name'] for p in result['products']], ['Air Max Pulse'])
            self.assertIn('96 USD', result['reply'])
            self.assertIn('EU 42', result['reply'])
            self.assertIn('EU 43', result['reply'])
            self.assertIn('1 пара', result['reply'])
            self.assertEqual(_sessions[self.sid]['filters'], original)
            self.assertFalse(result['needs_clarification'])

    def test_short_fact_question_recovers_previously_misclassified_model(self):
        from app.services.chat_sessions import save_session_state
        self.send('Посоветуй кроссовки', 'uk')
        state = _sessions[self.sid]
        state['filters']['name_query'] = 'розмір ціна?'
        state['hard_constraints']['name_query'] = 'розмір ціна?'
        save_session_state(self.db, self.sid, state)
        _sessions.pop(self.sid)
        result = self.send('розмір ціна?', 'uk')
        self.assertEqual(result['reply'], 'Яку модель кросівок ви шукаєте?')
        self.assertNotIn('name_query', _sessions[self.sid]['filters'])
        self.assertNotIn('name_query', _sessions[self.sid]['hard_constraints'])

    def test_fact_question_detector_does_not_consume_constraints_or_topics(self):
        from app.catalog_rules import product_fact_question
        for message in ('Nike Air Max Pulse', 'цена до 120 долларов', 'размер EU 42',
                        'розмір 42', 'размер не важен', 'цена нефти?', 'есть новости?'):
            with self.subTest(message=message):
                self.assertFalse(product_fact_question(message))

    def test_multiturn_model_measurement_unknown_confirmation_and_size_ru_uk(self):
        for language in ('ru', 'uk'):
            for title in ('Adidas Superstar', 'Puma Palermo', 'Nike Air Max Pulse'):
                with self.subTest(language=language, model=title):
                    _sessions.pop(self.sid, None)
                    self.sid += '-next'
                    self.send(title, language)
                    measured = self.send('28,5 см', language)
                    self.assertIn('EU', measured['reply'])
                    self.assertNotIn('стопи чи устілки', measured['reply'])
                    self.assertNotIn('стопы или стельки', measured['reply'])
                    self.assertEqual(_sessions[self.sid]['dialogue']['missing'], ['eu_size'])
                    self.send('не знаю', language)
                    _sessions.pop(self.sid, None)
                    confirmed = self.send('так' if language == 'uk' else 'да', language)
                    self.assertEqual([p['brand']+' '+p['name'] for p in confirmed['products']], [title])
                    sized = self.send('42', language)
                    self.assertEqual(sized['filters']['size'], 42)
                    self.assertEqual(_sessions[self.sid]['clarification']['measurement']['value'], '28.5')
                    self.assertIsNone(_sessions[self.sid]['clarification']['measurement']['kind'])
                    self.assertEqual(_sessions[self.sid]['filters']['name_query'], title.split(' ', 1)[1])
                    explicit = self.send('EU 42', language)
                    self.assertEqual(explicit['filters']['size'], 42)
                    self.assertEqual([p['brand']+' '+p['name'] for p in explicit['products']], [title])

    def test_selected_task_restores_after_new_model_and_cache_reload(self):
        for language, back in (('ru', 'Вернёмся к выбранной паре'), ('uk', 'Повернімося до обраної пари')):
            with self.subTest(language=language):
                _sessions.pop(self.sid, None)
                self.sid += '-next'
                self.send('Nike Air Max Pulse до $120', language)
                self.send('Хочу именно эту пару' if language == 'ru' else 'Хочу саме цю пару', language)
                self.send('42', language)
                selected = _sessions[self.sid]['clarification']['selected_product_id']
                self.send('Adidas Samba OG', language)
                self.assertEqual(_sessions[self.sid]['dialogue']['transition'], 'new_task')
                _sessions.pop(self.sid, None)
                restored = self.send(back, language)
                self.assertEqual([p['id'] for p in restored['products']], [selected])
                self.assertEqual(restored['filters']['size'], 42)
                self.assertEqual(restored['filters']['max_price'], 120)
                self.assertEqual(_sessions[self.sid]['dialogue']['transition'], 'resume')

    def test_selected_bookmark_survives_alternative_search(self):
        self.send('Nike Air Max Pulse размер 42')
        self.send('Хочу именно эту пару')
        selected = _sessions[self.sid]['clarification']['selected_product_id']
        self.send('Покажи другие варианты')
        result = self.send('Вернёмся к выбранной паре')
        self.assertEqual([p['id'] for p in result['products']], [selected])
        self.assertEqual(_sessions[self.sid]['clarification']['selected_product_id'], selected)

    def test_politeness_preserves_a_pending_confirmation(self):
        self.send('Adidas Superstar')
        self.send('28,5 см')
        self.send('не знаю')
        question = _sessions[self.sid]['dialogue']['last_question'].copy()
        self.send('Спасибо')
        self.assertEqual(_sessions[self.sid]['dialogue']['last_question'], question)
        self.assertEqual([p['name'] for p in self.send('да')['products']], ['Superstar'])

    def test_yes_no_do_not_fill_non_binary_slots_or_restart_model_search(self):
        self.send('Nike Air Max Pulse')
        self.send('Есть мой размер?')
        for answer in ('так', 'ні'):
            response = self.send(answer, 'uk')
            self.assertIn('EU', response['reply'])
            self.assertNotIn('size', _sessions[self.sid]['filters'])
            self.assertEqual(_sessions[self.sid]['filters']['name_query'], 'Air Max Pulse')
        response = self.send('42')
        self.assertIn('EU 42', response['reply'])

    def test_ambiguous_decimal_gets_unit_confirmation_before_measurement(self):
        self.send('Adidas Superstar')
        response = self.send('28,5')
        self.assertEqual(_sessions[self.sid]['dialogue']['missing'], ['measurement_unit'])
        self.assertNotIn('measurement', _sessions[self.sid]['clarification'])
        self.send('см')
        self.assertEqual(_sessions[self.sid]['clarification']['measurement']['value'], '28.5')
        self.assertNotIn('size', _sessions[self.sid]['filters'])

    def test_number_after_criterion_question_is_not_silently_a_size(self):
        self.start_nike()
        self.send('А что лучше?')
        result = self.send('43')
        self.assertEqual(result['reply'], 'Это EU-размер?')
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)
        self.send('да')
        self.assertEqual(_sessions[self.sid]['filters']['size'], 43)

    def test_off_topic_turn_suspends_pending_question_without_losing_model(self):
        self.send('Adidas Superstar')
        self.send('Есть мой размер?')
        result = self.send('Какая сегодня погода?')
        self.assertNotIn('важно при выборе', result['reply'])
        self.assertEqual(_sessions[self.sid]['dialogue']['transition'], 'suspend')
        _sessions.pop(self.sid, None)
        self.send('42')
        self.assertEqual(_sessions[self.sid]['filters']['name_query'], 'Superstar')
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)

    def test_absent_model_blocks_measurement_and_alternatives_across_turns(self):
        self.send('Nike Air Max Pulse')
        for message in ('Найди Nike Phantom 999', '28,5 см', '42', 'Сколько стоят?', 'Есть мой размер?', 'Как оформить заказ?'):
            response = self.send(message)
            self.assertEqual(response['products'], [])
            self.assertIn('отсутствует', response['reply'])
            self.assertIn('phantom 999', response['reply'])
            self.assertNotIn('?', response['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['name_query'], 'phantom 999')

    def test_contextual_price_uses_variant_and_keeps_constraints(self):
        self.start_nike()
        result = self.send('Сколько стоят?')
        self.assertIn('Air Max Pulse — 97 USD, EU 42', result['reply'])
        self.assertIn("Air Force 1 '07 — 115 USD, EU 42", result['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 120)
        self.assertNotIn('Какой', result['reply'])

    def test_better_clarifies_instead_of_restarting_selection(self):
        self.start_nike()
        result = self.send('А что лучше?')
        self.assertEqual(result['reply'], 'Что для вас важнее при выборе между этими моделями?')
        self.assertEqual(len(result['products']), 2)
        self.assertEqual(_sessions[self.sid]['filters']['size'], 42)

    def test_ukrainian_price_comparison_and_politeness(self):
        self.assertEqual(self.send('Вітаю', 'uk')['reply'], 'Вітаю! Чим можу вам допомогти?')
        self.send('Хочу Nike до 120 доларів, розмір 42', 'uk')
        comparison = self.send('Порівняй Nike Air Max Pulse і Nike Dunk Low Retro', 'uk')
        self.assertIn('Порівняння', comparison['reply'])
        self.assertIn('немає в наявності', comparison['reply'])
        for untranslated in ('цена ', 'доступные размеры', 'остаток ', 'цвет ', 'Walking', 'Lifestyle'):
            self.assertNotIn(untranslated, comparison['reply'])
        self.assertIn('97 USD', self.send('Скільки коштують?', 'uk')['reply'])
        self.assertEqual(self.send('Дякую', 'uk')['reply'], 'Будь ласка!')

    def test_general_recommendation_does_not_prioritize_cheapest(self):
        from app.ai.tools import recommend_products
        products = recommend_products(self.db, size=42)
        self.assertEqual(products[0]['name'], 'Air Max Pulse')
        self.assertNotEqual(products[0]['name'], 'Palermo')

    def test_bare_other_options_keep_conditions(self):
        self.start_nike(43)
        result = self.send('Другие')
        self.assertEqual([p['name'] for p in result['products']], ['Winflo 11'])
        self.assertEqual(result['filters']['brand'], 'Nike')
        self.assertEqual(result['filters']['size'], 43)

    def test_daily_recommendation_asks_size_once(self):
        self.assertTrue(self.send('Посоветуй кроссовки для повседневной носки')['needs_clarification'])
        self.send('42')
        result = self.send('Есть мой размер?')
        self.assertIn('EU 42', result['reply'])
        self.assertNotIn('Какой', result['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['use_case'], 'Lifestyle')

    def test_color_refinement_keeps_budget_brand_and_size(self):
        before = self.start_nike()
        result = self.send('А какие из них есть в чёрном цвете?')
        self.assertEqual([p['name'] for p in result['products']], ["Air Force 1 '07"])
        self.assertTrue({p['id'] for p in result['products']} <= {p['id'] for p in before['products']})
        self.assertEqual(result['filters']['max_price'], 120)
        self.assertEqual(result['filters']['size'], 42)
        self.assertEqual(result['filters']['brand'], 'Nike')
        self.assertEqual(result['filters']['color'], 'Black')

    def test_color_refinement_does_not_add_unshown_models(self):
        before = self.start_nike(43)
        self.assertNotIn('Winflo 11', [p['name'] for p in before['products']])
        result = self.send('А какие из них есть в чёрном цвете?')
        self.assertNotIn('Winflo 11', [p['name'] for p in result['products']])

    def test_comparison_reference_and_cheaper_use_variant_price_without_changing_budget(self):
        self.start_nike()
        comparison = self.send('Сравни эти две модели')
        self.assertEqual(len(comparison['products']), 2)
        self.assertIn('97 USD', comparison['reply'])
        result = self.send('А что дешевле?')
        self.assertIn('Air Max Pulse', result['reply'])
        self.assertIn('97 USD', result['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 120)
        self.assertLess(len(result['reply']), 180)

    def test_three_visible_models_require_explicit_comparison_choice(self):
        self.send('На каждый день, размер 42')
        result = self.send('Сравни эти две модели')
        self.assertTrue(result['needs_clarification'])
        self.assertFalse(result['products'])
        self.assertIn('Какие две', result['reply'])

    def test_ambiguous_selection_then_named_choice_survives_session_reload(self):
        self.start_nike()
        result = self.send('Хочу именно эту пару')
        self.assertTrue(result['needs_clarification'])
        self.assertIn('Какую пару', result['reply'])
        self.send('Nike Air Max Pulse')
        _sessions.pop(self.sid)
        result = self.send('Как оформить заказ?')
        self.assertEqual([p['name'] for p in result['products']], ['Air Max Pulse'])
        self.assertIn('EU 42', result['reply'])
        self.assertIn('Оставить заявку', result['reply'])
        self.assertNotIn('Какой размер', result['reply'])
        state = load_session_state(self.db, self.sid)
        self.assertEqual(state['filters']['max_price'], 120)
        self.assertEqual(state['clarification']['selected_product_id'], self.products['Air Max Pulse'].id)

    def test_single_model_selection_and_order_use_real_request_flow(self):
        self.send('Найди Nike Air Max Pulse, размер 42')
        self.assertFalse(self.send('Хочу именно эту пару')['needs_clarification'])
        result = self.send('Как оформить заказ?')
        self.assertIn('выберите этот размер', result['reply'])
        self.assertIn('имя и контакт', result['reply'])
        self.assertIn('без оплаты', result['reply'])

    def test_missing_stock_in_comparison_is_not_recommended_as_available(self):
        self.start_nike()
        self.send('Сравни Nike Air Max Pulse и Nike Dunk Low Retro')
        cheaper = self.send('А что дешевле?')
        self.assertIn('Air Max Pulse', cheaper['reply'])
        stock = self.send('Есть мой размер?')
        self.assertIn('Nike Dunk Low Retro, EU 42: нет в наличии', stock['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['max_price'], 120)

    def test_unknown_size_is_asked_once_and_then_remembered(self):
        self.send('Найди Nike Air Max Pulse')
        self.assertTrue(self.send('Есть мой размер?')['needs_clarification'])
        self.assertIn('EU 42: в наличии 2 пары', self.send('42')['reply'])
        self.assertFalse(self.send('Есть мой размер?')['needs_clarification'])

    def test_more_results_do_not_repeat_or_drop_constraints(self):
        first = self.send('На каждый день, размер 42')
        result = self.send('Покажи другие варианты')
        self.assertTrue(result['products'])
        self.assertFalse({p['id'] for p in first['products']} & {p['id'] for p in result['products']})
        self.assertEqual(result['filters']['size'], 42)
        self.assertEqual(result['filters']['use_case'], 'Lifestyle')

    def test_empty_refinement_does_not_make_old_model_orderable(self):
        self.send('Найди Nike Air Max Pulse, размер 42')
        self.assertFalse(self.send('А какие из них есть в чёрном цвете?')['products'])
        result = self.send('Хочу именно эту пару')
        self.assertTrue(result['needs_clarification'])
        self.assertNotIn('Оставить заявку', result['reply'])
        self.assertEqual(_sessions[self.sid]['filters']['color'], 'Black')

    def test_other_options_release_only_the_selected_model(self):
        self.start_nike()
        self.send('Хочу именно эту пару')
        self.send('Nike Air Max Pulse')
        result = self.send('Покажи другие варианты')
        self.assertEqual([p['name'] for p in result['products']], ["Air Force 1 '07"])
        self.assertEqual(result['filters']['brand'], 'Nike')
        self.assertEqual(result['filters']['size'], 42)
        self.assertEqual(result['filters']['max_price'], 120)

    def test_comparison_does_not_select_a_pair_without_explicit_choice(self):
        self.start_nike()
        self.send('Сравни Nike Air Max Pulse и Nike Dunk Low Retro')
        result = self.send('Хочу именно эту пару')
        self.assertTrue(result['needs_clarification'])
        self.assertNotIn('Оставить заявку', result['reply'])

    def test_selection_rechecks_stock_after_catalog_changes(self):
        self.send('Найди Nike Air Max Pulse, размер 42')
        self.products['Air Max Pulse'].variants[0].stock = 0
        self.db.commit()
        result = self.send('Хочу именно эту пару')
        self.assertTrue(result['needs_clarification'])
        self.assertFalse(result['products'])

    def test_explicit_constraints_are_not_relaxed_when_nothing_matches(self):
        result = self.send('Хочу Nike до 50 долларов, размер 42')
        self.assertFalse(result['products'])
        self.assertEqual(result['filters']['max_price'], 50)
        self.assertEqual(result['filters']['size'], 42)

    def test_ukrainian_contextual_stock_selection_and_order(self):
        self.send('Nike розмір 42 до 120 USD', 'uk')
        self.assertIn('у наявності', self.send('Є мій розмір?', 'uk')['reply'])
        self.assertTrue(self.send('Хочу саме цю пару', 'uk')['needs_clarification'])
        self.send('Nike Air Max Pulse', 'uk')
        self.assertIn('Залишити заявку', self.send('Як оформити замовлення?', 'uk')['reply'])

    def test_order_without_a_model_does_not_invent_an_order(self):
        result = self.send('Как оформить заказ?')
        self.assertTrue(result['needs_clarification'])
        self.assertFalse(result['products'])
        self.assertIn('выберите модель', result['reply'])


if __name__ == '__main__':
    unittest.main()
