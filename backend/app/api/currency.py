from fastapi import APIRouter

from ..services.currency import get_rates

router = APIRouter(prefix="/api/currency", tags=["currency"])


@router.get("/rates")
def read_currency_rates():
    """Current Frankfurter snapshot or an explicit unavailable response."""
    return get_rates()
