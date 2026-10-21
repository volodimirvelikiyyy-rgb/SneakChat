"""Execute validated read-only TurnPlans with existing catalog tools and reply templates.

No provider prose, product facts or database writes enter this module. The chat
service alone commits the resulting session after successful execution/grounding.
"""

from copy import deepcopy
import re

from .providers import ProviderUnavailable
from .turn_plan import TurnPlan, ParameterChange
from .tools import check_stock, search_products
from ..services.products import get_product, serialize_product
from ..services.dialogue_state import discovery_question
from ..catalog_rules import detect_absent_properties
from ..services.request_safety import catalog_model_filters, conflict_response, measurement_response

_FILTER_KEYS = {"brand", "color", "size", "min_price", "max_price", "currency", "gender",
                "use_case", "feature", "name_query", "exclude_brand", "exclude_attributes", "sku_query"}
_QUESTIONS = {
    "need": ("Что для вас важно при выборе кроссовок?", "Що для вас важливо у виборі кросівок?"),
    "eu_size": ("Какой EU-размер проверить?", "Який EU-розмір перевірити?"),
    "measurement_kind": ("Это длина стопы или стельки?", "Це довжина стопи чи устілки?"),
    "measurement_unit": ("Это сантиметры или EU-размер?", "Це сантиметри чи EU-розмір?"),
    "model": ("Какую модель вы имеете в виду?", "Яку модель ви маєте на увазі?"),
    "comparison_models": ("Какие две модели сравнить?", "Які дві моделі порівняти?"),
    "color": ("Какой цвет вам нужен?", "Який колір вам потрібен?"),
    "criterion": ("Что для вас важнее при выборе?", "Що для вас важливіше у виборі?"),
    "budget_boundary": ("Какую границу бюджета изменить?", "Яку межу бюджету змінити?"),
    "condition": ("Какое условие вы хотите изменить?", "Яку умову ви хочете змінити?"),
    "confirm_show_model": ("Показать доступные размеры этой модели?", "Показати доступні розміри цієї моделі?"),
    "confirm_size": ("Это EU-размер?", "Це EU-розмір?"),
    "confirm_length": ("Это значение в сантиметрах?", "Це значення в сантиметрах?"),
}


def build_turn_plan(understanding, message, context):
    """Backend compiles an understanding delta into the existing internal plan."""
    from ..services.assistant import parse_request
    from ..catalog_rules import detect_negations, detect_removals, BRAND_CANONICAL, product_fact_question

    parsed = parse_request(message)
    changes = {change.field: change for change in understanding.changes}
    if len(changes) != len(understanding.changes):
        raise ValueError("Conflicting updates")
    for field, change in changes.items():
        if change.operation == 'set' and field in parsed and field != 'name_query' and parsed[field] != change.value:
            raise ValueError("Understanding contradicts parser evidence")
        if change.operation == 'set' and parsed.get(field) == change.value and isinstance(change.value, str):
            literal = re.search(re.escape(change.value), message, re.I)
            if literal:
                changes[field] = change.model_copy(update={'evidence': literal.group()})
    queries = understanding.model_queries
    for query in queries:
        if (not query.strip() or len(query) > 120 or query.casefold() not in message.casefold()
                or query.casefold().strip() in BRAND_CANONICAL or product_fact_question(query)):
            raise ValueError("Model query must be a literal model name")
    shown = context['shown_products']
    named = [item['id'] for item in shown if (
        item['name'].casefold() == str(parsed.get('name_query', '')).casefold()
        or any(query.casefold() in (item.get('brand', '') + ' ' + item['name']).casefold() for query in queries))]
    for field, value in parsed.items():
        if field not in _FILTER_KEYS or field in changes or not isinstance(value, (str, float, int)):
            continue
        if field == 'name_query' and (named or str(value).casefold() not in message.casefold()):
            continue  # Canonical aliases are resolved by tools, not invented evidence.
        if len(message) <= 160:
            literal = re.search(re.escape(str(value)), message, re.I) if isinstance(value, str) else None
            evidence = literal.group() if literal else message
            changes[field] = ParameterChange(field=field, operation='set', value=value, evidence=evidence)
    for field, values in detect_negations(message).items():
        if field not in changes and len(values) == 1 and len(message) <= 160:
            changes[field] = ParameterChange(field=field, operation='exclude', value=values[0], evidence=message)
    for field in detect_removals(message):
        fields = ('min_price', 'max_price', 'currency') if field == 'price' else (field,)
        for slot in fields:
            if slot in _FILTER_KEYS and slot not in changes and len(message) <= 160:
                changes[slot] = ParameterChange(field=slot, operation='remove', value=None, evidence=message)

    intent = understanding.intent
    brand_change = parsed.get('brand') and parsed['brand'] != context['filters'].get('brand')
    if brand_change and not queries and not parsed.get('name_query') and intent != 'other_topic':
        from ..services.request_safety import _COMPARE_INTENT
        if not _COMPARE_INTENT.search(message):
            intent = 'recommend'
    if ((intent == 'resume' and context['selected_product_id'] is None)
            or (intent == 'locate' and not queries and not parsed.get('name_query'))):
        if changes:
            intent = 'recommend'
    transition = 'refine' if changes else 'continue'
    selected, refs, question, actions = None, [], None, []
    parts = ['catalog_results']
    unresolved = list(understanding.unresolved_preferences)
    if detect_absent_properties(message) and len(message) <= 160:
        unresolved = [message]  # Backend knows the attribute has no confirmed catalog data.
    if intent == 'other_topic':
        if changes or queries:
            raise ValueError("Unrelated topic cannot alter shopping constraints")
        transition, intent, parts = 'suspend', 'clarification', ['acknowledge']
    elif intent == 'resume':
        selected = context['selected_product_id']
        if selected is None:
            question = 'model'
        else:
            transition, refs = 'resume', [selected]
            actions = [{'tool': 'get_product_details', 'product_ids': refs, 'model_queries': []}]
        intent = 'locate'
    elif queries and (not named or intent == 'compare' and len(named) != len(queries)):
        actions = [{'tool': 'search_products', 'product_ids': [], 'model_queries': queries}]
        transition = 'new_task'
        if intent == 'select':
            parts = ['select_product']
            intent = 'locate'
    elif intent == 'compare':
        refs = named or ([] if brand_change else [item['id'] for item in shown])
        if len(refs) != 2:
            refs, question = [], 'comparison_models'
        else:
            actions = [{'tool': 'get_product_details', 'product_ids': refs, 'model_queries': []}]
            parts = ['comparison']
    elif intent in {'select', 'order', 'price', 'availability', 'locate'}:
        refs = named or ([context['selected_product_id']] if context['selected_product_id'] else [])
        if not refs and len(shown) == 1:
            refs = [shown[0]['id']]
        if len(refs) != 1:
            question = 'model'
        else:
            selected = refs[0] if intent in {'select', 'order'} or refs[0] == context['selected_product_id'] else None
            actions = [{'tool': 'get_product_details', 'product_ids': refs, 'model_queries': []}]
        if intent == 'select':
            intent = 'locate'
    else:
        parts = ['more_results'] if intent == 'more' else ['cheaper_results'] if intent == 'cheaper' else ['catalog_results']
        if intent in {'more', 'cheaper'}:
            transition = 'new_task'
        actions = [{'tool': 'recommend_products', 'product_ids': [], 'model_queries': []}]
        # Relative cheaper is rendered from a temporary query, never saved as a budget.
        intent = 'recommend' if intent in {'more', 'cheaper'} else intent
    future = {**context['filters'], **{c.field: c.value for c in changes.values() if c.operation == 'set'}}
    if intent == 'recommend' and not unresolved and future.get('use_case') and not future.get('size'):
        question = 'eu_size'
    if unresolved:
        parts = ['unknown_attribute']
        question = None  # An already named criterion does not need another criterion question.
        refs = [item['id'] for item in shown]
        actions = [{'tool': 'get_product_details', 'product_ids': refs, 'model_queries': []}] if refs else []
        intent = 'clarification'
    if question:
        parts = ['clarification']
    return TurnPlan.model_validate({
        'intent': intent, 'transition': transition, 'changes': [c.model_dump() for c in changes.values()],
        'selected_product_id': selected, 'referenced_product_ids': refs, 'actions': actions,
        'question': {'kind': question, 'reason': 'ambiguous' if question in {'model', 'comparison_models'} else 'missing'} if question else None,
        'reply_plan': [{'kind': kind, 'product_ids': [], 'fact_fields': []} for kind in parts],
        'unresolved_preferences': unresolved,
    })


def _result(reply, products=(), question=None, *, facts_only=False):
    return {"reply": reply, "products": list(products), "needs_clarification": question is not None,
            "suggestions": [], "_question": question, "_facts_only": facts_only}


def _apply(state, plan):
    filters = state.setdefault("filters", {})
    clarification = state.setdefault("clarification", {})
    turn = state.setdefault("dialogue", {})
    if plan.transition == "resume":
        bookmark = turn.get("selected_task")
        if not bookmark or bookmark.get("product_id") != plan.selected_product_id:
            raise ProviderUnavailable("invalid_resume")
        filters = state["filters"] = deepcopy(bookmark["filters"])
        clarification.pop("measurement", None)
        if bookmark.get("measurement"):
            clarification["measurement"] = deepcopy(bookmark["measurement"])
    if plan.transition == "new_task":
        filters.pop("name_query", None)
        filters.pop("sku_query", None)
        state["comparison"] = None
        state["products"] = []
        clarification.pop("selected_product_id", None)
        for key in ("awaiting_stock_size", "choosing_product", "pending_number"):
            clarification.pop(key, None)
    if plan.transition in {"new_task", "suspend", "resume"}:
        turn["last_question"] = None
        turn["missing"] = []
    for change in plan.changes:
        field = change.field
        if field in {"measurement_value", "measurement_kind"}:
            measurement = clarification.setdefault("measurement", {})
            slot = "value" if field == "measurement_value" else "kind"
            if change.operation == "remove":
                measurement.pop(slot, None)
            else:
                measurement[slot] = change.value
            clarification.pop("measurement_deferred", None)
        elif change.operation == "remove":
            filters.pop(field, None)
            if field == "brand":
                filters.pop("name_query", None)
        elif change.operation == "exclude":
            if filters.get(field) == change.value:
                filters.pop(field)
            if field == "brand":
                filters.pop("name_query", None)
                rejected = filters.setdefault("exclude_brand", [])
            else:
                code = {"use_case": "purpose", "feature": "tags"}.get(field, field)
                rejected = filters.setdefault("exclude_attributes", {}).setdefault(code, [])
            if change.value not in rejected:
                rejected.append(change.value)
        else:
            if field in {"brand", "name_query"} and filters.get(field) != change.value and plan.selected_product_id is None:
                clarification.pop("selected_product_id", None)
                if field == "brand":
                    filters.pop("name_query", None)
            filters[field] = change.value
    clarification["explicit_eu"] = filters.get("size")
    if plan.selected_product_id is not None:
        clarification["selected_product_id"] = plan.selected_product_id
    state["hard_constraints"] = deepcopy(filters)
    turn.update(intent=plan.intent, transition=plan.transition)
    if plan.unresolved_preferences or plan.transition == "new_task":
        turn["unresolved_preferences"] = list(plan.unresolved_preferences)
    state["shopping"] = plan.transition != "suspend"


def _lookup(db, filters, display_currency):
    """Price predicates use the requested variant, never its model's base price."""
    from ..services.chat import _price_for_size
    from ..services.currency import convert_amount, get_rates

    query = {key: value for key, value in filters.items() if key in _FILTER_KEYS}
    minimum, maximum = query.pop("min_price", None), query.pop("max_price", None)
    query.pop("currency", None)
    products = search_products(db, **query, display_currency=display_currency)
    if minimum is None and maximum is None:
        return products
    currency = filters.get("currency") or display_currency
    kept = []
    for product in products:
        price, source = _price_for_size(product, filters.get("size"))
        amount = convert_amount(price, source, currency, get_rates().get("rates") if source != currency else None)
        if amount is not None and (minimum is None or amount >= minimum) and (maximum is None or amount <= maximum):
            kept.append(product)
    return kept


def execute_turn_plan(db, state, plan, message, language, display_currency):
    """Mutate only the caller's private state copy; return grounded reply instructions."""
    from ..services.chat import _comparison_reply, _price_for_size, _soft_preference_updates, _rank_soft_preferences

    uk = language == "uk"
    say = lambda ru, ua: ua if uk else ru
    opening = discovery_question(state, message, language)
    preferences = state.setdefault('preferences', {})
    explicit = {c.field: c.value for c in plan.changes if c.operation == 'set'}
    soft = _soft_preference_updates(message, dict(explicit), display_currency)
    if 'brand' in explicit and 'brand' not in soft:
        preferences.pop('brand', None)
    preferences.update(soft)
    anchors = state.get('products') or state.get('last_successful_products') or []
    _apply(state, plan)
    filters, clarification = state["filters"], state["clarification"]
    if 'brand' in soft:
        filters.pop('brand', None)
        filters.pop('name_query', None)
    if 'price_target' in soft:
        for field in ('min_price', 'max_price', 'currency'):
            filters.pop(field, None)
    state['hard_constraints'] = deepcopy(filters)
    if plan.transition == "suspend":
        state["products"] = []
        return _result(say("Я могу помочь с каталогом и выбором кроссовок. Контекст выбранной пары сохранён.",
                           "Я можу допомогти з каталогом і вибором кросівок. Контекст обраної пари збережено."))
    conflict = conflict_response(state, filters, language)
    if conflict:
        return conflict

    products, missing = [], []
    point_lookup = plan.intent in {"compare", "price", "availability", "order"} or plan.transition == "resume"
    more = any(part.kind == "more_results" for part in plan.reply_plan)
    cheaper = any(part.kind == 'cheaper_results' for part in plan.reply_plan)
    query_filters = dict(filters)
    if cheaper and anchors:
        from ..services.currency import convert_amount, get_rates
        target = filters.get('currency') or display_currency
        prices = []
        for item in anchors:
            amount, source = _price_for_size(item, filters.get('size'))
            amount = convert_amount(amount, source, target, get_rates().get('rates') if source != target else None)
            if amount is not None:
                prices.append(float(amount))
        if not prices:
            raise ProviderUnavailable('price_reference_unavailable')
        query_filters['max_price'] = min(filters.get('max_price', float('inf')), min(prices) - .01)
        query_filters['currency'] = target
    for action in plan.actions:
        if action.tool in {"get_product_details", "check_stock"}:
            for identifier in action.product_ids:
                stored = get_product(db, identifier)
                if stored is None:
                    missing.append(identifier)
                    continue
                if action.tool == "check_stock":
                    check_stock(db, identifier, filters["size"])
                products.append(serialize_product(stored, display_currency))
        elif action.model_queries:
            for query in action.model_queries:
                model = catalog_model_filters(db, query) or {"name_query": query}
                conditions = {key: value for key, value in filters.items() if key not in {"name_query", "brand", "sku_query"}}
                if point_lookup:
                    conditions = {}
                matches = _lookup(db, {**conditions, **model}, display_currency)
                if not matches:
                    missing.append(query)
                products.extend(matches[:3])
                if len(action.model_queries) == 1 and len(plan.actions) == 1:
                    filters.update(model)
                    state["hard_constraints"] = deepcopy(filters)
        else:
            matches = _rank_soft_preferences(_lookup(db, query_filters, display_currency), preferences)
            preferred = [item for item in matches if item['brand'] == preferences.get('brand')]
            if preferred:
                matches = preferred
            if action.product_ids:
                matches = [item for item in matches if item["id"] in action.product_ids]
            if more:
                matches = [item for item in matches if item["id"] not in state.get("shown_product_ids", [])]
            products.extend(matches[:3])
    products = list({item["id"]: item for item in products}.values())[:6]

    # A reference may focus an existing card, but its facts are always read again.
    if not products and not plan.actions and not plan.question:
        ids = plan.referenced_product_ids or ([plan.selected_product_id] if plan.selected_product_id else [])
        for identifier in ids:
            stored = get_product(db, identifier)
            if stored is not None:
                products.append(serialize_product(stored, display_currency))
    selected = plan.selected_product_id
    if (selected is None and any(part.kind == 'select_product' for part in plan.reply_plan) and len(products) == 1
            and sum(len(action.model_queries) for action in plan.actions) == 1):
        selected = products[0]['id']
        clarification['selected_product_id'] = selected
    if selected is not None:
        focused = next((item for item in products if item["id"] == selected), None)
        if focused is None:
            raise ProviderUnavailable("selection_not_resolved")
        filters.update(name_query=focused["name"], brand=focused["brand"])
        state["hard_constraints"] = deepcopy(filters)
    state["products"] = products

    if missing:
        labels = "; ".join(str(value) for value in missing)
        prefix = say(f"Не нашёл в каталоге по указанным условиям: {labels}.",
                     f"Не знайшов у каталозі за вказаними умовами: {labels}.")
        if not products:
            return _result(prefix)
    else:
        prefix = ""
    if clarification.get("measurement") and not clarification.get("explicit_eu"):
        measured = measurement_response(db, state, filters, language, display_currency)
        if measured is not None:
            return measured
    question = plan.question.kind if plan.question else None
    unsupported_criterion = bool(detect_absent_properties(message))
    if unsupported_criterion:
        question = None  # The shopper has already named the criterion; its data is missing.
    if question in {"confirm_size", "confirm_length", "measurement_unit"} and re.fullmatch(r"\d+(?:[.,]\d+)?", message.strip()):
        clarification["pending_number"] = message.strip()
    if plan.intent == "recommend" and not filters and not plan.unresolved_preferences:
        question, products = "need", []
    opening = opening if question == "need" else None
    if question == "need" and opening is None:
        question = None
        products = products or _lookup(db, filters, display_currency)[:3]
    if plan.intent == "availability" and filters.get("size") is None:
        question = "eu_size"
    if plan.intent == "compare" and len(products) < 2 and not missing:
        question = "comparison_models"
    size = filters.get("size")
    parts = []
    if prefix:
        parts.append(prefix)
    if plan.unresolved_preferences or any(part.kind == "unknown_attribute" for part in plan.reply_plan):
        parts.append(say("Не буду предполагать свойства обуви: могу опираться только на сведения каталога.",
                         "Не буду припускати властивості взуття: можу спиратися лише на відомості каталогу."))
        if unsupported_criterion:
            parts.append(say("Могу сравнить цену, размеры и наличие.", "Можу порівняти ціну, розміри та наявність."))
    if plan.intent == "order":
        if len(products) != 1:
            question = "model"
        elif size is None:
            question = "eu_size"
        elif not check_stock(db, products[0]["id"], size):
            parts.append(say(f"EU {size:g} у этой модели нет в наличии.", f"EU {size:g} цієї моделі немає в наявності."))
        elif not any(item["id"] == products[0]["id"] for item in _lookup(db, filters, display_currency)):
            question = "condition"
            parts.append(say("Эта пара не соответствует сохранённым условиям.", "Ця пара не відповідає збереженим умовам."))
        else:
            parts.append(say("Откройте карточку, выберите размер и нажмите «Оставить заявку». Укажите имя и контакт в форме; здесь заявка ещё не создана.",
                             "Відкрийте картку, оберіть розмір і натисніть «Залишити заявку». Вкажіть ім’я та контакт у формі; тут заявку ще не створено."))
    elif plan.intent == "compare" and products:
        parts.append(_comparison_reply([item["name"] for item in products], products, message, language, size, compact=True))
    elif plan.intent in {"price", "availability"} and products:
        for product in products:
            amount, currency = _price_for_size(product, size)
            variant = next((item for item in product["variants"] if item["size"] == size), None)
            stock = variant["stock"] if variant else (product["total_stock"] if size is None else 0)
            suffix = f", EU {size:g}" if size is not None else ""
            status = say(f"в наличии {stock} пар", f"у наявності {stock} пар") if stock else say("нет в наличии", "немає в наявності")
            if size is not None and variant is None:
                parts.append(f"{product['brand']} {product['name']}{suffix}: {status}. " + say(
                    f"Цена модели без привязки к этому размеру — {amount:g} {currency}.",
                    f"Ціна моделі без прив’язки до цього розміру — {amount:g} {currency}."))
            else:
                parts.append(f"{product['brand']} {product['name']}{suffix}: {amount:g} {currency}; {status}.")
    elif products and not question:
        parts.append(say("По каталогу подходят: ", "За каталогом підходять: ") + "; ".join(
            f"{item['brand']} {item['name']}" for item in products) + ".")
    elif plan.actions and not products and not question and not prefix:
        parts.append(say("Дешевле показанных по этим условиям моделей нет.", "Дешевше за показані за цими умовами моделей немає.") if cheaper else
                     say("Других моделей по этим условиям больше нет.", "Інших моделей за цими умовами більше немає.") if more else
                     say("По этим условиям моделей в наличии не нашёл. Условия сохранены.",
                         "За цими умовами моделей у наявності не знайшов. Умови збережено."))
    if question:
        parts.append(opening if question == "need" else _QUESTIONS[question][1 if uk else 0])
    if not parts:
        parts.append(say("Хорошо. Ваши условия сохранены.", "Добре. Ваші умови збережено.") if filters else
                     say("Здравствуйте! Чем могу вам помочь?", "Вітаю! Чим можу вам допомогти?"))
    return _result(" ".join(parts), products, question, facts_only=point_lookup)
