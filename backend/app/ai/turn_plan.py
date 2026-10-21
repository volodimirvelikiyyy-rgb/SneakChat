"""Read-only planning contract. A plan is not permission to execute or write data."""

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..catalog_rules import (
    BRAND_CANONICAL, COLOR_CANONICAL, FEATURE_CANONICAL, GENDER_CANONICAL,
    USE_CASE_CANONICAL, product_fact_question, size_axis_bounds,
)
from ..schemas import PRICE_MAX, PRICE_MIN

Intent = Literal["locate", "recommend", "compare", "price", "availability", "order", "clarification"]
Transition = Literal["continue", "new_task", "refine", "resume", "suspend"]
Parameter = Literal[
    "name_query", "brand", "size", "measurement_value", "measurement_kind", "color",
    "min_price", "max_price", "currency", "gender", "use_case", "feature",
]
QuestionKind = Literal[
    "need", "eu_size", "measurement_kind", "measurement_unit", "model", "comparison_models",
    "color", "criterion", "budget_boundary", "condition", "confirm_show_model", "confirm_size", "confirm_length",
]


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ParameterChange(PlanModel):
    field: Parameter
    operation: Literal["set", "remove", "exclude"]
    value: str | float | None
    evidence: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def valid_parameter(self):
        if self.operation == "remove":
            if self.value is not None:
                raise ValueError("Removal must not assign a value")
            return self
        if self.operation == "exclude" and self.field not in {"brand", "color", "size", "gender", "use_case", "feature"}:
            raise ValueError("This parameter cannot be excluded")
        if self.field in {"size", "measurement_value", "min_price", "max_price"}:
            if not isinstance(self.value, float) or not math.isfinite(self.value):
                raise ValueError("Expected a finite number")
            low, high = size_axis_bounds() if self.field == "size" else (
                (PRICE_MIN, PRICE_MAX) if self.field in {"min_price", "max_price"} else (1, 60))
            if not low <= self.value <= high:
                raise ValueError("Number outside supported bounds")
        elif not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 120:
            raise ValueError("Expected a short nonempty string")
        allowed = {
            "brand": set(BRAND_CANONICAL.values()), "color": set(COLOR_CANONICAL.values()),
            "gender": set(GENDER_CANONICAL.values()), "use_case": set(USE_CASE_CANONICAL.values()),
            "feature": set(FEATURE_CANONICAL.values()), "currency": {"USD", "EUR", "UAH"},
            "measurement_kind": {"foot_cm", "insole_cm", "cm_jp"},
        }
        if self.field in allowed and self.value not in allowed[self.field]:
            raise ValueError("Unsupported parameter value")
        return self


class TurnUnderstanding(PlanModel):
    """Public model contract: understanding only, no executable store decisions."""

    intent: Literal["locate", "recommend", "compare", "price", "availability", "order",
                    "clarification", "select", "resume", "more", "cheaper", "other_topic"]
    changes: list[ParameterChange] = Field(max_length=8, description="New parameters only, with exact current-message evidence. Known context is retained by backend.")
    model_queries: list[str] = Field(max_length=2, description="Literal model names quoted from the current message. Empty for brands or previously shown pairs.")
    unresolved_preferences: list[str] = Field(max_length=3, description="Current-message quotes requesting unsupported properties; never assertions about shoes.")


class CatalogAction(PlanModel):
    tool: Literal["search_products", "get_product_details", "check_stock", "recommend_products"]
    product_ids: list[int] = Field(max_length=6)
    model_queries: list[str] = Field(max_length=2)

    @model_validator(mode="after")
    def valid_arguments(self):
        if any(value <= 0 for value in self.product_ids):
            raise ValueError("Invalid product reference")
        if any(not text.strip() or len(text) > 120 for text in self.model_queries):
            raise ValueError("Invalid model query")
        if self.tool in {"get_product_details", "check_stock"} and (not self.product_ids or self.model_queries):
            raise ValueError("This tool needs existing product references")
        return self


class ClarifyingQuestion(PlanModel):
    kind: QuestionKind
    reason: Literal["missing", "ambiguous", "conflict", "unsupported"]


class ReplyPart(PlanModel):
    kind: Literal[
        "acknowledge", "catalog_results", "comparison", "price", "availability", "clarification",
        "request_instructions", "unknown_attribute", "model_lookup", "more_results", "cheaper_results", "select_product",
    ]
    product_ids: list[int] = Field(max_length=6)
    fact_fields: list[Literal[
        "brand", "name", "price", "available_sizes", "total_stock", "color", "use_case", "features", "variants",
    ]] = Field(max_length=9)


class TurnPlan(PlanModel):
    intent: Intent = Field(description="Intent of the latest message, not the preceding turn.")
    transition: Transition
    changes: list[ParameterChange] = Field(max_length=8, description="Only changes supported by the latest message; do not copy saved context.")
    selected_product_id: int | None
    referenced_product_ids: list[int] = Field(max_length=6)
    actions: list[CatalogAction] = Field(max_length=3)
    question: ClarifyingQuestion | None = Field(description="One necessary typed question, or null. With null, never emit a clarification reply part.")
    reply_plan: list[ReplyPart] = Field(min_length=1, max_length=4)
    # Quotes of the shopper, never assertions about product properties.
    unresolved_preferences: list[str] = Field(max_length=3, description="Exact quotes of new unsupported preferences from the latest message only.")

    @model_validator(mode="after")
    def consistent_plan(self):
        slots = [change.field for change in self.changes]
        if len(slots) != len(set(slots)):
            raise ValueError("Conflicting updates to one parameter")
        if self.selected_product_id is not None and self.selected_product_id <= 0:
            raise ValueError("Invalid selection")
        if any(not quote.strip() or len(quote) > 160 for quote in self.unresolved_preferences):
            raise ValueError("Invalid unresolved preference")
        if self.question is None and any(part.kind == "clarification" for part in self.reply_plan):
            raise ValueError("Clarification needs a typed question")
        if self.transition == "suspend" and (self.actions or self.changes or self.selected_product_id is not None):
            raise ValueError("Suspending a task cannot change preferences or run tools")
        # model_lookup requests a lookup; it cannot assert that the model exists/does not exist.
        return self


def _validate_explicit_constraints(plan: TurnPlan, message: str, parsed: dict, context: dict, future: dict) -> None:
    """Require coverage of reliable parser evidence, not just valid proposed updates.

    Never repair a model plan here: rejection delegates to the existing fallback.
    Literal model lookups may carry their own brand, including two-brand comparisons.
    """
    from ..services.assistant import parse_request
    from ..catalog_rules import detect_negations, detect_removals

    queries = [query for action in plan.actions for query in action.model_queries]
    query_brands = [parse_request(query).get('brand') for query in queries]
    removals = detect_removals(message)
    for field in ('brand', 'size', 'min_price', 'max_price', 'currency', 'use_case', 'color', 'gender'):
        if field not in parsed:
            continue
        axis = 'price' if field in {'min_price', 'max_price', 'currency'} else field
        if axis in removals or 'all' in removals:
            continue
        if (field == 'size' and re.fullmatch(r'\d+(?:[.,]\d+)?', message.strip())
                and plan.question and plan.question.reason == 'ambiguous'
                and plan.question.kind in {'confirm_size', 'measurement_unit', 'confirm_length'}):
            # A bare number may be stored as a pending question, not an EU filter.
            continue
        actual = future.get(field)
        if field == 'brand' and queries:
            if len(queries) == 1 and query_brands[0] is not None:
                actual = query_brands[0]
            elif len(queries) > 1 and parsed[field] in query_brands:
                # These are separate model lookups, not one global brand filter.
                continue
        if field == 'brand' and plan.intent in {'compare', 'locate'}:
            ids = {identifier for action in plan.actions for identifier in action.product_ids}
            brands = {item.get('brand') for item in context.get('shown_products', []) if item['id'] in ids}
            if parsed[field] in brands:
                continue
        if actual != parsed[field]:
            raise ValueError('Plan loses or contradicts an explicit constraint: ' + field)

    excluded = {'brand': set(context.get('exclude_brand', []))}
    for field, values in context.get('exclude_attributes', {}).items():
        excluded[field] = set(values if isinstance(values, list) else [values])
    for change in plan.changes:
        if change.operation == 'exclude':
            excluded.setdefault(change.field, set()).add(change.value)
    for field, values in detect_negations(message).items():
        positive = future.get(field)
        positive = positive if isinstance(positive, list) else [positive]
        if field == 'brand':
            positive = [*positive, *query_brands]
        if not set(values) <= excluded.get(field, set()) or any(value in values for value in positive):
            raise ValueError('Plan loses or contradicts an explicit exclusion: ' + field)


def validate_plan_context(plan: TurnPlan, message: str, context: dict) -> None:
    """Reject invented references and unproven updates before a future executor sees them."""
    # Reuse parsing for unambiguous numeric/currency evidence, not a second catalog engine.
    from ..services.assistant import parse_request
    from ..catalog_rules import detect_negations, detect_removals
    from ..services.request_safety import measurement_kind

    known_ids = set(context["shown_product_ids"])
    selected = context["selected_product_id"]
    if selected is not None:
        known_ids.add(selected)
    refs = [*plan.referenced_product_ids]
    if plan.selected_product_id is not None:
        refs.append(plan.selected_product_id)
    for item in [*plan.actions, *plan.reply_plan]:
        refs.extend(item.product_ids)
    if not set(refs) <= known_ids:
        raise ValueError("Unknown product reference")
    if plan.transition == "resume" and (selected is None or plan.selected_product_id != selected):
        raise ValueError("No matching selection to resume")
    text = message.casefold()
    parsed = parse_request(message)
    if any(part.kind == "more_results" for part in plan.reply_plan):
        from ..catalog_rules import _is_more_request_phrase
        if (any(change.operation == "set" and context["filters"].get(change.field) != change.value
                for change in plan.changes) and not _is_more_request_phrase(message)):
            raise ValueError("New constraints require fresh results, not unrequested pagination")
    for change in plan.changes:
        if change.evidence.casefold() not in text:
            raise ValueError("Evidence is not in the current message")
        if change.operation == "remove":
            axis = "price" if change.field in {"min_price", "max_price", "currency"} else change.field
            removed = detect_removals(message)
            axes = {"price": r"цен\w*|ц[іi]н\w*|бюджет\w*|потолок|стеля",
                    "brand": r"бренд\w*|марк\w*", "color": r"цвет\w*|кол[іi]р\w*",
                    "size": r"размер\w*|розмір\w*", "name_query": r"модел\w*"}
            explicit_removal = re.search(r"не\s+важ\w*|не\s+нуж\w*|не\s+потріб\w*|не\s+учитыв\w*|не\s+врахов\w*|убер\w*|прибери|скасу\w*", change.evidence, re.I)
            if (axis not in removed and "all" not in removed
                    and not (explicit_removal and re.search(axes.get(axis, r"(?!)"), change.evidence, re.I))):
                raise ValueError("Removal lacks explicit consent")
        elif change.operation == "exclude":
            negations = detect_negations(message)
            values = negations.get(change.field, [])
            explicit_negative = re.search(r"\b(?:не|ні|без|кроме|окрім|except|not)\b", change.evidence, re.I)
            confirmed = parse_request(change.evidence).get(change.field)
            if change.field == "brand" and confirmed != change.value and str(change.value).casefold() not in change.evidence.casefold():
                raise ValueError("Brand lacks explicit evidence")
            if change.value not in values and not (explicit_negative and confirmed == change.value):
                raise ValueError("Exclusion lacks explicit evidence")
        elif change.field in {"size", "min_price", "max_price", "currency"}:
            proof = parse_request(change.evidence)
            if change.field in {"min_price", "max_price"}:
                opposite = "min_price" if change.field == "max_price" else "max_price"
                if proof.get(opposite) == change.value and proof.get(change.field) != change.value:
                    raise ValueError("Budget direction contradicts the evidence")
            bare_size = (change.field == "size" and context["pending_question"] == "eu_size"
                         and re.fullmatch(r"\d+(?:[.,]\d+)?", message.strip()))
            expected = float(message.strip().replace(",", ".")) if bare_size else parsed.get(change.field)
            if (change.field == "size" and context["pending_question"] == "confirm_size"
                    and message.strip().casefold().strip('!.') in {"да", "так", "ага"}):
                expected = context.get("pending_number")
            # The planner may infer a budget role from free wording, but cannot invent
            # its amount or contradict an explicit EU/CM/minimum/maximum in the quote.
            if expected is None and change.field in {"min_price", "max_price", "size"}:
                roles = {"size": r"\b(?:eu|размер\w*|розмір\w*|ношу|взуваю)\b",
                         "max_price": r"\b(?:до|бюджет\w*|потолок|стеля|верхн\w*)\b|не\s+(?:больше|более|вище|дорожче)",
                         "min_price": r"\b(?:от|від|нижн\w*|миним\w*|мінім\w*)\b"}
                numbers = [float(n.replace(',', '.')) for n in re.findall(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)", change.evidence)]
                if re.search(roles[change.field], change.evidence, re.I) and numbers == [change.value]:
                    expected = change.value
                if expected is None and change.field in {"min_price", "max_price"} and numbers == [change.value]:
                    proof = parse_request(change.evidence)
                    opposite = "min_price" if change.field == "max_price" else "max_price"
                    if (proof.get("size") != change.value and proof.get(opposite) != change.value
                            and not re.search(r"\b(?:см|cm|jp|eu)\b", change.evidence, re.I)
                            and not re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", change.evidence)):
                        expected = change.value
            if expected != change.value:
                raise ValueError("Unconfirmed numeric or currency update")
        elif change.field == "measurement_value":
            if not re.search(r"(?<!\d)" + re.escape(f"{change.value:g}").replace(r"\.", "[.,]") + r"(?!\d)", change.evidence):
                raise ValueError("Unconfirmed measurement")
            if not re.search(r"см|cm|jp", message, re.I) and context["pending_question"] != "measurement_kind":
                raise ValueError("Unknown measurement unit")
        elif change.field == "measurement_kind" and measurement_kind(change.evidence) != change.value:
            raise ValueError("Unconfirmed measurement type")
        elif change.field == "name_query":
            if str(change.value).casefold() in BRAND_CANONICAL:
                raise ValueError("A brand is a filter, not a model name")
            if str(change.value).casefold() not in change.evidence.casefold():
                raise ValueError("Model name lacks explicit evidence")
            if product_fact_question(str(change.value)):
                raise ValueError("A catalog field question is not a model name")
        elif change.field in {"brand", "color", "gender", "use_case", "feature"}:
            confirmed = parse_request(change.evidence).get(change.field)
            if change.field == "brand" and confirmed != change.value and str(change.value).casefold() not in change.evidence.casefold():
                raise ValueError("Brand lacks explicit evidence")
            if (change.field == "feature" and confirmed != change.value
                    and not (isinstance(confirmed, list) and change.value in confirmed)):
                raise ValueError("Unconfirmed categorical preference")
            if re.search(r"\b(?:не|ні|без|кроме|окрім|not|except)\b", change.evidence, re.I):
                raise ValueError("Negative evidence cannot set a positive preference")
    for action in plan.actions:
        for query in action.model_queries:
            if query.casefold().strip() in BRAND_CANONICAL:
                raise ValueError("A brand is a filter, not a model query")
            if product_fact_question(query):
                raise ValueError("A catalog field question is not a model name")
            if query.casefold() not in text:
                raise ValueError("Model query must quote the current request; use IDs for previous products")
    if any(quote.casefold() not in text for quote in plan.unresolved_preferences):
        raise ValueError("Unresolved preference is not a user quote")
    future = dict(context["selected_filters"] if plan.transition == "resume" else context["filters"])
    if plan.transition == "new_task":
        future.pop("name_query", None)
    for change in plan.changes:
        if change.operation == "remove":
            future.pop(change.field, None)
        elif change.operation == "exclude":
            if future.get(change.field) == change.value:
                future.pop(change.field, None)
            if change.field == "brand":
                future.pop("name_query", None)
        elif change.operation == "set":
            if change.field == "brand" and future.get("brand") != change.value:
                future.pop("name_query", None)
            future[change.field] = change.value
    _validate_explicit_constraints(plan, message, parsed, context, future)
    if plan.question and plan.question.kind == "need" and any(
            future.get(field) is not None for field in
            ("brand", "name_query", "size", "min_price", "max_price", "color", "use_case", "gender")):
        raise ValueError("Use the known preferences instead of restarting discovery")
    if plan.question and plan.question.reason == "missing":
        slot = {"eu_size": "size", "color": "color", "model": "name_query"}.get(plan.question.kind)
        if slot and future.get(slot) is not None:
            raise ValueError("This answer is already known")
        new_kind = future.get("measurement_kind") or context["measurement"]["kind"]
        if plan.question.kind == "measurement_kind" and new_kind is not None:
            raise ValueError("Measurement type is already known")
    if future.get("min_price") is not None and future.get("max_price") is not None and future["min_price"] > future["max_price"]:
        if not plan.question or plan.question.kind != "budget_boundary" or plan.question.reason != "conflict":
            raise ValueError("Budget conflict needs clarification")
    if any(item.tool == "check_stock" for item in plan.actions) and future.get("size") is None:
        raise ValueError("Stock check needs a confirmed size")
