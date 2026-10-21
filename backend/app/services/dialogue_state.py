"""Typed turn transitions over the existing filters, clarification and product state.

No catalog facts live here. Short answers fill only the slot actually requested;
the ordinary parser, catalog search and grounding still produce the response.
"""

from copy import deepcopy
import re

from ..catalog_rules import NEW_SEARCH_PHRASES, assistance_word, is_recommendation_request, shopping_intent, size_axis_bounds, vocabulary_words

_YES = {"да", "так", "ага"}
_NO = {"нет", "ні"}
_UNKNOWN = {"не знаю", "не помню", "не пам'ятаю", "не пам’ятаю"}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def selection_help_request(message):
    """Short requests for assistance/options, optionally with catalog constraints.

    Inflected help verbs and the options noun express an intent, not a model name.
    Other words must still belong to the existing domain vocabulary.
    """
    words = re.findall(r"[\w’'-]+", message.casefold())
    if not words or len(words) > 12 or any(word in {"не", "ні", "нет", "без"} for word in words):
        return False
    matched = [assistance_word(word) for word in words]
    return any(matched) and all(
        is_help or word in vocabulary_words() or word in _YES or _NUMBER.fullmatch(word)
        for word, is_help in zip(words, matched))


def dialogue(state):
    value = state.setdefault("dialogue", {})
    value.setdefault("intent", "locate")
    value.setdefault("missing", [])
    if "last_question" not in value:
        # Lazy compatibility for existing JSON sessions; no database migration.
        clarification = state.get("clarification", {})
        measurement = clarification.get("measurement") or {}
        kind = ("eu_size" if clarification.get("awaiting_stock_size") or (
                    clarification.get("asked_eu") and clarification.get("explicit_eu") is None) else
                "measurement_kind" if clarification.get("asked_length") and not measurement.get("kind") else None)
        history = state.get("history") or []
        value["last_question"] = {"kind": kind, "text": history[-1].get("content", "") if history else ""} if kind else None
        value["missing"] = [kind] if kind else []
    return value


def record_reply(state, result):
    """Every response records its question explicitly, without parsing its wording."""
    turn = dialogue(state)
    question = result.pop("_question", None)
    if not result.pop("_preserve_question", False):
        turn["last_question"] = {"kind": question, "text": result["reply"]} if question else None
        turn["missing"] = [question] if question else []
    turn["intent"] = result.pop("_intent", turn["intent"])
    selected = state.get("clarification", {}).get("selected_product_id")
    for product in result.get("products", []):
        if product["id"] == selected and state["filters"].get("name_query") == product["name"]:
            turn["selected_task"] = {
                "product_id": selected,
                "filters": deepcopy(state["filters"]),
                "measurement": deepcopy(state.get("clarification", {}).get("measurement")),
            }


def discovery_question(state, message, language):
    """One relevant opening question; repeating an unanswered opener allows browsing."""
    if (dialogue(state).get("last_question") or {}).get("kind") == "need":
        return None
    if is_recommendation_request(message):
        return ("Для чого шукаєте пару — на щодень чи для спорту?" if language == "uk" else
                "Для чего ищете пару — на каждый день или для спорта?")
    return ("Шукаєте конкретну модель чи допомогти з вибором?" if language == "uk" else
            "Ищете конкретную модель или помочь с выбором?")


def prepare_turn(state, message, parsed, language):
    """Classify the turn before dispatch. Return normalized input or a safe clarification."""
    turn = dialogue(state)
    text = message.strip().lower().strip(" ?!.")
    uk = language == "uk"
    clarification = state.setdefault("clarification", {})
    previous = state.get("filters", {})
    question = (turn.get("last_question") or {}).get("kind")
    help_requested = selection_help_request(message)
    task_input = help_requested or shopping_intent(message, parsed, established=state.get("shopping", False))
    if parsed.get("name_query"):
        remainder = re.sub(r"(?<!\w)" + re.escape(parsed["name_query"].lower()) + r"(?!\w)", " ", text)
        task_input = task_input or not remainder.strip() or shopping_intent(remainder, parsed)
    reset = task_input and any(phrase in text for phrase in NEW_SEARCH_PHRASES)
    new_model = task_input and parsed.get("name_query") and parsed["name_query"] != previous.get("name_query")
    new_brand = task_input and parsed.get("brand") and parsed["brand"] != previous.get("brand")
    turn["transition"] = "new_task" if reset or new_model or new_brand else "refine" if parsed else "continue"
    if reset or new_model or new_brand:
        turn["last_question"] = None
        turn["missing"] = []
        question = None
        clarification.pop("pending_number", None)
        clarification.pop("awaiting_stock_size", None)
        clarification.pop("choosing_product", None)
        state["products"] = []
        state["last_successful_products"] = []
        state["last_successful_product_ids"] = []
        state["shown_product_ids"] = []
        state["comparison"] = None
        if clarification.get("measurement"):
            clarification["measurement_deferred"] = True
    if reset:
        for key in ("measurement", "explicit_eu", "asked_eu", "asked_length", "measurement_deferred"):
            clarification.pop(key, None)
        state["skip_size"] = False
    if help_requested:
        # Asking for options authorizes browsing, not resetting known constraints.
        state["browse"] = True
        turn["last_question"] = None
        turn["missing"] = []
        turn["intent"] = "recommend"
    if parsed.get("name_query") or parsed.get("sku_query"):
        turn["intent"] = "locate"
    elif task_input and is_recommendation_request(text) and not state.get("comparison"):
        turn["intent"] = "recommend"
    if re.search(r"сравн|порівн", text):
        turn["intent"] = "compare"
    elif re.search(r"цен\w*|ц[іi]н\w*|стоит|стоят|кошту\w*", text):
        turn["intent"] = "price"
    elif re.search(r"налич\w*|наяв\w*|мой размер|мій розмір", text):
        turn["intent"] = "availability"
    elif re.search(r"оформ\w*|заказ\w*|замов\w*", text):
        turn["intent"] = "order"

    def respond(reply, pending=question):
        return {"reply": reply, "_question": pending, "_intent": turn["intent"],
                "products": [], "needs_clarification": bool(pending), "suggestions": []}

    # Selection is a stable bookmark, independent of the currently inspected model.
    if re.search(r"верн\w*|поверн\w*", text) and re.search(r"выбран\w*|обран\w*", text):
        selected = turn.get("selected_task")
        if selected:
            clarification["selected_product_id"] = selected["product_id"]
            state["filters"] = deepcopy(selected["filters"])
            state["hard_constraints"] = deepcopy(selected["filters"])
            if selected.get("measurement"):
                clarification["measurement"] = deepcopy(selected["measurement"])
            else:
                clarification.pop("measurement", None)
            clarification["explicit_eu"] = state["filters"].get("size")
            state["products"] = []
            turn["transition"] = "resume"
            turn["intent"] = "locate"
            return " ".join(str(state["filters"].get(key, "")) for key in ("brand", "name_query")), None, True
        return message, respond("Ще немає обраної пари." if uk else "Вы ещё не выбрали пару.", None), False

    if question == "measurement_unit" and re.fullmatch(r"см|cm|сантиметр\w*", text):
        number = clarification.pop("pending_number")
        return f"{number} см", None, True
    if question == "confirm_size" and text in _YES:
        return f"EU {clarification.pop('pending_number')}", None, True
    if question == "confirm_size" and text in _NO:
        return message, respond("Це число в сантиметрах?" if uk else "Это число в сантиметрах?", "confirm_length"), False
    if question == "confirm_length" and text in _YES:
        return f"{clarification.pop('pending_number')} см", None, True
    if question == "confirm_show_model" and text in _YES:
        state["skip_size"] = True
        clarification["measurement_deferred"] = True
        return "Покажи доступные размеры" if not uk else "Покажи доступні розміри", None, True
    size_answer = re.fullmatch(r"(?:(?:eu|size|размер|розмір)\s*)?\d+(?:[.,]\d+)?(?:\s*(?:eu|size|размер|розмір))?", text)
    if (size_answer and set(parsed) == {"size"} and not _NUMBER.fullmatch(text)
            and (question == "eu_size" or state.get("shopping"))):
        return f"EU {parsed['size']:g}", None, True
    if question and text in _UNKNOWN:
        if question in {"eu_size", "measurement_kind", "measurement_unit", "confirm_size"}:
            state["skip_size"] = True
            clarification["measurement_deferred"] = True
            reply = ("Показати доступні EU-розміри цієї моделі?" if uk else "Показать доступные EU-размеры этой модели?")
            if clarification.get("measurement"):
                reply = ("Не визначатиму EU-розмір за непідтвердженими сантиметрами. " if uk else
                         "Не буду определять EU-размер по неподтверждённым сантиметрам. ") + reply
            if previous.get("name_query"):
                return message, respond(reply, "confirm_show_model"), False
            return message, respond("Можемо поки переглянути каталог без розміру." if uk else
                                    "Можем пока посмотреть каталог без размера.", None), False
        return message, respond("Можна назвати модель або одну важливу для вас умову." if uk else
                                "Можно назвать модель или одно важное для вас условие.", None), False
    if text in _YES | _NO:
        if question == "confirm_show_model" and text in _NO:
            return message, respond("Добре. Обрану модель і ваші умови збережено." if uk else
                                    "Хорошо. Модель и ваши условия сохранены.", None), False
        labels = {
            "eu_size": ("Потрібен EU-розмір числом, якщо він вам відомий.", "Нужен EU-размер числом, если он вам известен."),
            "measurement_kind": ("Потрібно уточнити тип вимірювання: стопа чи устілка.", "Нужно уточнить тип измерения: стопа или стелька."),
            "measurement_unit": ("Потрібна одиниця вимірювання: сантиметри чи EU.", "Нужна единица измерения: сантиметры или EU."),
        }
        pair = labels.get(question)
        reply = pair[0 if uk else 1] if pair else ("Уточніть, будь ласка, до чого стосується відповідь." if uk else
                                                    "Уточните, пожалуйста, к чему относится ответ.")
        return message, respond(reply), False
    if _NUMBER.fullmatch(text):
        number = float(text.replace(",", "."))
        if question == "budget_boundary":
            return message, respond("Це мінімальна чи максимальна ціна?" if uk else
                                    "Это минимальная или максимальная цена?"), False
        minimum, maximum = size_axis_bounds()
        if question == "measurement_kind" and clarification.get("measurement"):
            return f"{text} см", None, True
        if minimum <= number <= maximum and (question == "eu_size" or (not question and state.get("shopping"))):
            return f"EU {number:g}", None, True
        clarification["pending_number"] = text
        if minimum <= number <= maximum:
            return message, respond("Це EU-розмір?" if uk else "Это EU-размер?", "confirm_size"), False
        return message, respond("Це значення в сантиметрах чи EU-розмір?" if uk else
                                "Это значение в сантиметрах или EU-размер?", "measurement_unit"), False
    return message, None, help_requested
