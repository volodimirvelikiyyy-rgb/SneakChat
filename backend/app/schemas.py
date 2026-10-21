import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Границы цены для значений, предложенных моделью. Факты и цены всё равно берутся из БД,
# но любые числа от LLM обязаны пройти эту проверку до построения запроса.
PRICE_MIN = 0.0
PRICE_MAX = 10000.0


def sanitize_price(value: object) -> float | None:
    """Возвращает цену только если это конечное число в допустимых границах, иначе None."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if not math.isfinite(price) or not PRICE_MIN <= price <= PRICE_MAX:
        return None
    return price


# Значение атрибута: одно значение или несколько (многозначные атрибуты вроде набора тегов).
AttributeValue = str | list[str]


class VariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    stock: int
    sku: str = ""
    price: float = 0.0
    # Ось варианта (для кроссовок — размер). У каталога без размеров её просто нет.
    size: float | None = None
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)


class ProductPhotoRead(BaseModel):
    id: int
    url: str
    position: int
    is_primary: bool
    crop_scale: float = 1.0
    crop_x: float = 0.0
    crop_y: float = 0.0


class ProductRead(BaseModel):
    """Карточка товара: универсальные поля плюс совместимые с текущим фронтендом.

    Поля color/gender/use_case/features/price/image_key/image_url/available_sizes заполняются
    совместимым слоем для каталога кроссовок и остаются пустыми для других категорий.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    brand: str
    name: str
    category: str = ""
    description: str = ""
    base_price: float = 0.0
    currency: Literal["USD", "EUR", "UAH"] = "USD"
    base_currency: Literal["USD", "EUR", "UAH"] = "USD"
    image: str = ""
    active: bool = True
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    variants: list[VariantRead]
    total_stock: int
    color: str | None = None
    gender: str | None = None
    use_case: str | None = None
    features: list[str] = Field(default_factory=list)
    price: float = 0.0
    image_key: str = ""
    image_url: str = ""
    photos: list[ProductPhotoRead] = Field(default_factory=list)
    available_sizes: list[float] = Field(default_factory=list)
    # Почему модель подошла: строится только из данных каталога и условий запроса.
    match_reasons: list[str] = Field(default_factory=list)


class AssistantSearchRequest(BaseModel):
    message: str


class AssistantSearchResponse(BaseModel):
    reply: str
    applied_filters: dict[str, object]
    products: list[ProductRead]


class ContactRequestCreate(BaseModel):
    product_id: int
    variant_id: int
    name: str = Field(min_length=2, max_length=100)
    contact: str = Field(min_length=3, max_length=150)
    comment: str | None = Field(default=None, max_length=500)


class ContactRequestResponse(BaseModel):
    id: int
    message: str


class ContactRequestStatusUpdate(BaseModel):
    status: Literal["new", "contacted", "completed", "cancelled"]


class ContactRequestAdminRead(BaseModel):
    id: int
    created_at: str
    name: str
    contact: str
    # Товар и размер пустые, если ссылка заявки не восстановилась после переноса каталога.
    product: str = ""
    size: float | None = None
    price: float
    currency: Literal["USD", "EUR", "UAH"] = "USD"
    comment: str | None
    status: str


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)
    language: Literal["ru", "uk"] = "ru"
    display_currency: Literal["USD", "EUR", "UAH"] = "UAH"


class ChatResponse(BaseModel):
    reply: str
    products: list[ProductRead]
    needs_clarification: bool
    mode: Literal["ollama", "fallback", "groq"]
    suggestions: list[str]
    # Условия, по которым собран ответ. Нужны интерфейсу, чтобы подсветить выбранный размер и
    # честно сказать, если его нет: решение о размере остаётся в backend.
    filters: dict[str, Any] = Field(default_factory=dict)


class ChatLogAdminRead(BaseModel):
    id: int
    created_at: str
    session_id: str
    language: str
    user_message: str
    assistant_reply: str
    product_count: int
    product_names: str
    needs_clarification: bool
    mode: str
    filters: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)


class AIFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brand: str | None = None
    color: str | None = None
    size: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    currency: Literal["USD", "EUR", "UAH"] | None = None
    gender: str | None = None
    use_case: str | None = None
    feature: str | None = None
    # Динамические оси каталога: модель не знает схемы БД и просто возвращает пары код-значение,
    # а ядро само решает, как их отфильтровать.
    attributes: dict[str, str | float] = Field(default_factory=dict)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def _only_sane_price(cls, value: object) -> float | None:
        """Невалидная цена от модели трактуется как отсутствующая, а не как фильтр."""
        return sanitize_price(value)

    @model_validator(mode="after")
    def _drop_inverted_range(self) -> "AIFilters":
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            self.min_price = None
            self.max_price = None
        return self


class AIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["search_products", "check_stock", "product_details", "compare_products", "recommendation", "clarification"]
    filters: AIFilters = Field(default_factory=AIFilters)
    # Подтверждение для каждого категориального фильтра: подстрока из сообщения пользователя.
    # Пустое подтверждение означает, что фильтр не применяется — модель не добавляет невысказанное.
    evidence: dict[str, str] = Field(default_factory=dict)

    @field_validator("evidence", mode="before")
    @classmethod
    def _only_text_evidence(cls, value: object) -> dict[str, str]:
        """В подтверждениях имеет смысл только текст, поэтому числа и мусор отбрасываются."""
        if not isinstance(value, dict):
            return {}
        return {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(item, (str, int, float)) and not isinstance(item, bool)
        }
