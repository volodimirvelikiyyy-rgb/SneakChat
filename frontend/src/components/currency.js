export const CURRENCIES = [
  { code: 'USD', label: 'USD — доллар США ($)', symbol: '$' },
  { code: 'EUR', label: 'EUR — евро (€)', symbol: '€' },
  { code: 'UAH', label: 'UAH — гривна (₴)', symbol: '₴' },
]

export function currencySymbol(currency = 'USD') {
  return CURRENCIES.find(item => item.code === currency)?.symbol || '$'
}

export function formatCurrency(amount, currency = 'USD') {
  const price = Number(amount)
  return `${currencySymbol(currency)}${Number.isFinite(price) ? price.toFixed(2) : '0.00'}`
}

function formatStorefrontCurrency(amount, currency) {
  const price = Number(amount)
  const value = Number.isFinite(price) ? price : 0
  const formatted = new Intl.NumberFormat('ru-RU', {
    maximumFractionDigits: currency === 'UAH' ? 0 : 2,
  }).format(value)
  return `${currencySymbol(currency)}${formatted}`
}

export function getProductPriceInCurrency(product, targetCurrency = 'UAH', rates = null, selectedSize = null) {
  const sourceCurrency = product.base_currency || product.currency || 'USD'
  const selectedVariant = selectedSize == null || selectedSize === ''
    ? null
    : (product.variants || []).find(variant => Number(variant.size) === Number(selectedSize))
  const variantPrice = selectedVariant?.price
  const hasVariantPrice = variantPrice !== null && variantPrice !== undefined && variantPrice !== ''
    && Number.isFinite(Number(variantPrice))
  const sourcePrice = Number(hasVariantPrice ? variantPrice : product.base_price ?? product.price)
  if (sourceCurrency === targetCurrency) return { amount: sourcePrice, currency: sourceCurrency }
  if (!rates || !Number.isFinite(sourcePrice)) return { amount: sourcePrice, currency: sourceCurrency }
  const sourceRate = Number(rates[sourceCurrency])
  const targetRate = Number(rates[targetCurrency])
  if (!(sourceRate > 0) || !(targetRate > 0)) return { amount: sourcePrice, currency: sourceCurrency }
  return { amount: (sourcePrice / sourceRate) * targetRate, currency: targetCurrency }
}

export function formatProductCurrency(product, targetCurrency = 'UAH', rates = null, selectedSize = null) {
  const price = getProductPriceInCurrency(product, targetCurrency, rates, selectedSize)
  return formatStorefrontCurrency(price.amount, price.currency)
}
