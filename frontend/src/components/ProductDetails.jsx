import { useEffect, useState } from 'react'
import ProductGallery from './ProductGallery'
import SizeChips from './SizeChips'
import { formatProductCurrency } from './currency'

const REQUEST_URL = '/api/requests'

/** Подробности товара: только реальные данные backend, без внутренних полей базы. */
export default function ProductDetails({ product, loading, error, onClose, t, labels, onToggleCompare, inCompare, displayCurrency = 'UAH', rates }) {
  const [selectedSize, setSelectedSize] = useState(null)
  const [requestOpen, setRequestOpen] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [requestError, setRequestError] = useState('')
  const [requestLoading, setRequestLoading] = useState(false)

  useEffect(() => {
    setSelectedSize(null)
    setRequestOpen(false)
    setSubmitted(false)
    setRequestError('')
  }, [product?.id])

  const submitRequest = async event => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const variant = product.variants.find(item => item.size === selectedSize)
    if (!variant) return
    setRequestLoading(true)
    setRequestError('')
    try {
      const response = await fetch(REQUEST_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          product_id: product.id,
          variant_id: variant.id,
          name: form.get('name'),
          contact: form.get('contact'),
          comment: form.get('comment') || null,
        }),
      })
      if (!response.ok) throw new Error()
      setSubmitted(true)
    } catch {
      setRequestError(t.requestError)
    } finally {
      setRequestLoading(false)
    }
  }

  // product может быть null, пока подробности загружаются.
  const tags = product?.features || []

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section
        className="product-details"
        role="dialog"
        aria-modal="true"
        aria-label={product ? `${product.brand} ${product.name}` : t.loadingProduct}
        onMouseDown={event => event.stopPropagation()}
      >
        <header className="details-head">
          <button type="button" className="details-back" onClick={onClose}>← {t.backToChat}</button>
        </header>
        {loading && <p className="details-status" role="status">{t.loadingProduct}</p>}
        {error && <p className="details-status error" role="alert">{t.errorText}</p>}
        {product && (
          <div className="details-body">
            <ProductGallery product={product} t={t} />
            <div className="details-info">
              <p className="product-brand">{product.brand}</p>
              <h2>{product.name}</h2>
              <p className="product-price">{formatProductCurrency(product, displayCurrency, rates, selectedSize)}</p>
              {product.description && <p className="details-description">{
                product.description === `${product.brand} ${product.name} в цвете ${product.color}. Демонстрационная модель каталога.`
                  ? t.demoDescription(product, labels.colors[product.color] || product.color)
                  : product.description
              }</p>}
              <SizeChips product={product} selectedSize={selectedSize} t={t} />
              <dl className="details-facts">
                {product.color && <div><dt>{t.color}</dt><dd>{labels.colors[product.color] || product.color}</dd></div>}
                {product.gender && <div><dt>{t.gender}</dt><dd>{labels.genders[product.gender] || product.gender}</dd></div>}
                {product.use_case && <div><dt>{t.purpose}</dt><dd>{labels.useCases[product.use_case] || product.use_case}</dd></div>}
                {tags.length > 0 && (
                  <div>
                    <dt>{t.features}</dt>
                    <dd>{tags.map(tag => labels.features[tag] || tag).join(', ')}</dd>
                  </div>
                )}
                <div><dt>{t.availability}</dt><dd>{product.total_stock > 0 ? t.inStock(product.total_stock) : t.soldOut}</dd></div>
              </dl>
              {product.match_reasons?.length > 0 && (
                <div className="details-reasons">
                  <p>{t.whyFits}</p>
                  <ul>{product.match_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
                </div>
              )}
              <div className="details-actions">
                {onToggleCompare && (
                  <button type="button" className={`action ghost ${inCompare ? 'active' : ''}`} onClick={() => onToggleCompare(product.id)}>
                    {inCompare ? t.inCompare : t.compare}
                  </button>
                )}
                <button
                  type="button"
                  className="action primary"
                  onClick={() => setRequestOpen(true)}
                  disabled={!product.variants.some(variant => variant.stock > 0)}
                >
                  {t.leaveRequest}
                </button>
              </div>
              {requestOpen && !submitted && (
                <form className="request-form" onSubmit={submitRequest}>
                  <p className="request-title">{selectedSize ? t.chooseSize : t.chooseFirst}</p>
                  <div className="request-sizes">
                    {product.variants.map(variant => {
                      const classes = ['size-chip']
                      classes.push(variant.stock > 0 ? 'available' : 'sold-out')
                      if (selectedSize === variant.size) classes.push('selected')
                      return (
                        <button
                          key={variant.id}
                          type="button"
                          className={classes.join(' ')}
                          disabled={variant.stock === 0}
                          onClick={() => setSelectedSize(variant.size)}
                        >
                          {variant.size}
                        </button>
                      )
                    })}
                  </div>
                  <p className="request-hint">{t.unavailableSizes}</p>
                  <label>{t.name}<input name="name" required placeholder={t.namePlaceholder} /></label>
                  <label>{t.contact}<input name="contact" required placeholder={t.contactPlaceholder} /></label>
                  <label>{t.comment} <small>{t.optional}</small><input name="comment" placeholder={t.commentPlaceholder} /></label>
                  {requestError && <p className="request-error" role="alert">{requestError}</p>}
                  <button type="submit" className="action primary" disabled={!selectedSize || requestLoading}>
                    {requestLoading ? t.sending : t.send}
                  </button>
                  <p className="request-privacy">{t.privacy}</p>
                </form>
              )}
              {submitted && (
                <div className="request-done" role="status">
                  <b>{t.success}</b>
                  <p>{t.successText(product.name, selectedSize, formatProductCurrency(product, displayCurrency, rates, selectedSize))}</p>
                </div>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  )
}
