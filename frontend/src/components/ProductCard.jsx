import ProductImage from './ProductImage'
import SizeChips from './SizeChips'
import { formatProductCurrency, getProductPriceInCurrency } from './currency'

/**
 * Карточка товара. Один компонент для каталога и для чата: данные всегда приходят из backend.
 *
 * `budgetMax` — бюджет из условий ответа. Если модель дороже, карточка говорит об этом прямо:
 * показать товар выше бюджета без пометки значит умолчать о нарушенном условии запроса.
 */
export default function ProductCard({ product, t, selectedSize, onOpen, onToggleCompare, inCompare, variant = 'grid', budgetMax, budgetCurrency, displayCurrency = 'UAH', rates }) {
  const compact = variant === 'chat'
  const reason = product.match_reasons?.[0]
  const targetBudgetCurrency = budgetCurrency || displayCurrency
  const comparablePrice = getProductPriceInCurrency(product, targetBudgetCurrency, rates, selectedSize)
  const overBudget = budgetMax != null && budgetMax !== '' && comparablePrice.currency === targetBudgetCurrency
    && Number.isFinite(Number(budgetMax)) && comparablePrice.amount > Number(budgetMax)
    ? comparablePrice.amount - Number(budgetMax)
    : null
  return (
    <article className={`product-card product-card-${variant}`}>
      <button type="button" className="product-open" onClick={() => onOpen(product.id)} aria-label={`${t.details}: ${product.brand} ${product.name}`}>
        <ProductImage product={product} size={compact ? 'sm' : 'md'} />
      </button>
      <div className="product-body">
        <p className="product-brand">{product.brand}</p>
        <h3 className="product-name">{product.name}</h3>
        <p className="product-price">{formatProductCurrency(product, displayCurrency, rates, selectedSize)}</p>
        {overBudget !== null && (
          <p className="product-over-budget" role="status">{t.overBudget(overBudget.toFixed(0))}</p>
        )}
        <SizeChips product={product} selectedSize={selectedSize} t={t} limit={compact ? 5 : undefined} />
        {reason && (
          <p className="product-reason"><span>{t.reasonLabel}</span> {reason}</p>
        )}
        {product.total_stock === 0 && <p className="product-sold-out">{t.soldOut}</p>}
        <div className="product-actions">
          <button type="button" className="action primary" onClick={() => onOpen(product.id)}>{t.details}</button>
          {onToggleCompare && (
            <button
              type="button"
              className={`action ghost ${inCompare ? 'active' : ''}`}
              onClick={() => onToggleCompare(product.id)}
              aria-pressed={inCompare ? 'true' : 'false'}
            >
              {inCompare ? t.inCompare : t.compare}
            </button>
          )}
        </div>
      </div>
    </article>
  )
}
