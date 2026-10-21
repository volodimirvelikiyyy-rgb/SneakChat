/**
 * Доступные размеры: показываются только реальные варианты, отсутствующие не предлагаются.
 *
 * Наличие по размеру видно в самой плашке, а не только в подсказке при наведении: на телефоне
 * hover недоступен, а «есть ли мой размер» — главный вопрос покупателя.
 */
export default function SizeChips({ product, selectedSize, t, limit }) {
  const variants = [...(product.variants || [])].sort((left, right) => left.size - right.size)
  const shown = typeof limit === 'number' ? variants.slice(0, limit) : variants
  const available = variants.filter(variant => variant.stock > 0)
  const requestedMissing = selectedSize != null && !available.some(variant => variant.size === selectedSize)
  return (
    <div className="size-area">
      {requestedMissing && <p className="size-warn" role="status">{t.sizeMissing(selectedSize)}</p>}
      <div className="size-chips">
        {shown.map(variant => {
          const classes = ['size-chip']
          classes.push(variant.stock > 0 ? 'available' : 'sold-out')
          if (selectedSize === variant.size) classes.push('selected')
          const label = variant.stock > 0 ? t.inStock(variant.stock) : t.soldOut
          return (
            <span
              key={variant.id}
              className={classes.join(' ')}
              aria-disabled={variant.stock > 0 ? undefined : 'true'}
              aria-label={`${variant.size}: ${label}`}
              title={label}
            >
              {variant.size}
              {variant.stock > 0 && <sup className="size-count" aria-hidden="true">{variant.stock}</sup>}
            </span>
          )
        })}
        {typeof limit === 'number' && variants.length > shown.length && (
          <span className="size-chip more">+{variants.length - shown.length}</span>
        )}
      </div>
    </div>
  )
}
