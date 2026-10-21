"""Real loopback HTTP regression; temporary catalog, no TestClient or cloud calls."""

import json
import threading
import time
import unittest
import uuid
from urllib.request import Request, urlopen
from unittest.mock import patch

from sqlalchemy.orm import Session
import uvicorn

from app.database import get_db
from app.main import app
from app.services.chat import _sessions
from app.services.chat_sessions import load_session_state
import test_shopper_conversation as fixtures


class ChatHelpHttpTests(unittest.TestCase):
    def test_complete_help_sequence_over_http_and_persisted_context(self):
        fixture = fixtures.ShopperConversationTests()
        fixture.setUp()
        previous_overrides = dict(app.dependency_overrides)

        def isolated_db():
            with Session(fixture.engine) as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, lifespan='off',
                                              log_config=None, log_level=None, access_log=False))
        listener = server.config.bind_socket()
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        sessions = []

        def send(sid, message, language='uk'):
            payload = json.dumps({'session_id': sid, 'message': message, 'language': language,
                                  'display_currency': 'USD'}).encode()
            request = Request(base + '/api/chat', data=payload, headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)

        try:
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(server.started, 'Isolated HTTP server did not start')
            for selected in (False, True):
                sid = 'help-http-' + uuid.uuid4().hex
                sessions.append(sid)
                if selected:
                    send(sid, 'Найди Nike Air Max Pulse, размер 42, до 120 USD')
                    send(sid, 'Хочу именно эту пару')
                else:
                    opening = send(sid, 'розмір ціна?')
                    self.assertTrue(opening['needs_clarification'])
                    self.assertIn('модель', opening['reply'])
                with Session(fixture.engine) as db:
                    original = load_session_state(db, sid)
                with patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'synthetic_unused'}), \
                        patch('app.ai.groq_provider.GroqTurnPlanner.plan',
                              side_effect=AssertionError('Cloud planning forbidden')) as planner:
                    for message in ('варіанті', 'підскажіть', 'так допоможіть'):
                        # Every HTTP request must work from persisted state, not only the cache.
                        _sessions.pop(sid, None)
                        result = send(sid, message)
                        self.assertEqual(result['mode'], 'fallback')
                        self.assertTrue(result['products'], result)
                        self.assertFalse(result['needs_clarification'])
                        self.assertNotIn('Я можу допомогти з каталогом', result['reply'])
                        with Session(fixture.engine) as db:
                            state = load_session_state(db, sid)
                            self.assertEqual(state['filters'], original['filters'])
                        if selected:
                            self.assertEqual([p['name'] for p in result['products']], ['Air Max Pulse'])
                            self.assertEqual(state['clarification']['selected_product_id'],
                                             original['clarification']['selected_product_id'])
                    planner.assert_not_called()
            # The complete buyer journey must survive an ordinary compound phrasing.
            conversations = {
                'uk': ('Шукаю кросівки на щодень. Допоможіть із вибором.',
                       'Хочу Nike, розмір EU 43, бюджет до 150 доларів.',
                       "Порівняй Nike Dunk Low Retro і Nike Air Force 1 '07.",
                       'Обираю Nike Dunk Low Retro, розмір EU 43.',
                       'Яка ціна та чи є мій розмір у наявності?',
                       'Як оформити заявку на цю пару?'),
                'ru': ('Ищу кроссовки на каждый день. Помогите с выбором.',
                       'Хочу Nike, размер EU 43, бюджет до 150 долларов.',
                       "Сравни Nike Dunk Low Retro и Nike Air Force 1 '07.",
                       'Выбираю Nike Dunk Low Retro, размер EU 43.',
                       'Какая цена и есть мой размер в наличии?',
                       'Как оформить заявку на эту пару?'),
            }
            for language, messages in conversations.items():
                sid = 'buyer-http-' + uuid.uuid4().hex
                sessions.append(sid)
                for index, message in enumerate(messages):
                    _sessions.pop(sid, None)
                    result = send(sid, message, language)
                    self.assertNotIn('помочь с каталогом', result['reply'])
                    self.assertNotIn('допомогти з каталогом', result['reply'])
                    with Session(fixture.engine) as db:
                        saved = load_session_state(db, sid)
                    self.assertEqual(saved['filters']['use_case'], 'Lifestyle')
                    if index >= 1:
                        for key, value in {'brand': 'Nike', 'size': 43, 'max_price': 150}.items():
                            self.assertEqual(saved['filters'][key], value)
                    if index == 2:
                        self.assertEqual({p['name'] for p in result['products']},
                                         {'Dunk Low Retro', "Air Force 1 '07"})
                    if index >= 3:
                        self.assertEqual([p['name'] for p in result['products']], ['Dunk Low Retro'])
                        self.assertEqual(saved['clarification']['selected_product_id'],
                                         fixture.products['Dunk Low Retro'].id)
                    if index == 4:
                        self.assertIn('110 USD', result['reply'])
                        self.assertIn('EU 43', result['reply'])
                    if index == 5:
                        self.assertIn('Залишити заявку' if language == 'uk' else 'Оставить заявку', result['reply'])
                product = result['products'][0]
                variant = next(v for v in product['variants'] if v['size'] == 43)
                request = Request(base + '/api/requests', data=json.dumps({
                    'product_id': product['id'], 'variant_id': variant['id'],
                    'name': 'DEMO TEST', 'contact': 'test@example.invalid',
                }).encode(), headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 201)
                    identifier = json.load(response)['id']
                from app.services.requests import list_contact_requests
                with Session(fixture.engine) as db:
                    created = next(row for row in list_contact_requests(db) if row['id'] == identifier)
                self.assertEqual(created['product'], 'Nike Dunk Low Retro')
                self.assertEqual(created['size'], 43)
                self.assertEqual(created['price'], variant['price'])
            sid = 'natural-http-' + uuid.uuid4().hex
            sessions.append(sid)
            messages = ('Привіт, шукаю кросівки на кожен день', 'Бажано Nike, розмір 43',
                        'До 150 доларів', 'А що зручніше?', 'Порівняй ці дві моделі',
                        'А якщо Adidas?', 'Ні, повернімося до Nike', 'Покажи дешевші',
                        'Беру Dunk Low, 43 розмір', 'Скільки коштують і чи є в наявності?',
                        'Хочу оформити замовлення')
            for index, message in enumerate(messages):
                _sessions.pop(sid, None)
                result = send(sid, message)
                self.assertNotIn('Я можу допомогти з каталогом', result['reply'])
                with Session(fixture.engine) as db:
                    saved = load_session_state(db, sid)
                self.assertEqual(saved['filters']['use_case'], 'Lifestyle')
                if index >= 1:
                    self.assertEqual(saved['filters']['size'], 43)
                if index >= 2:
                    self.assertEqual(saved['filters']['max_price'], 150)
                if index == 1:
                    self.assertEqual(saved['preferences']['brand'], 'Nike')
                if index == 3:
                    self.assertIn('немає', result['reply'])
                    self.assertIn('комфорт', result['reply'])
                if index in (5, 6):
                    self.assertEqual(saved['filters']['brand'], 'Adidas' if index == 5 else 'Nike')
                if index >= 8:
                    self.assertEqual(saved['clarification']['selected_product_id'],
                                     fixture.products['Dunk Low Retro'].id)
                    self.assertEqual([p['name'] for p in result['products']], ['Dunk Low Retro'])
                if index == 9:
                    self.assertIn('110 USD', result['reply'])
                if index == 10:
                    self.assertIn('Залишити заявку', result['reply'])
        finally:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=5)
            listener.close()
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)
            for sid in sessions:
                _sessions.pop(sid, None)
            fixture.tearDown()
        self.assertFalse(thread.is_alive(), 'Isolated HTTP server was not stopped')
