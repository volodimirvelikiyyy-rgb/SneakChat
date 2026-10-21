/**
 * Условия поиска в виде подписей для покупателя.
 *
 * Логику подбора определяет backend: он возвращает в ответе `filters` — те условия, по которым
 * действительно шёл поиск. Здесь они только переводятся в слова, ни одно условие не выводится
 * заново и не досчитывается. Если значение неизвестно словарю, оно показывается как есть:
 * скрыть настоящее условие хуже, чем показать его кодом.
 */
export function buildConditions(filters, t, labels) {
  if (!filters) return []
  const colors = labels?.colors || {}
  const genders = labels?.genders || {}
  const useCases = labels?.useCases || {}
  const features = labels?.features || {}
  const lower = value => String(value).toLowerCase()
  const items = []

  if (filters.brand) items.push(`${t.condBrand} ${filters.brand}`)
  if (filters.color) items.push(`${t.condColor} ${lower(colors[filters.color] || filters.color)}`)
  if (filters.size != null && filters.size !== '') items.push(`${t.condSize} ${filters.size}`)
  if (filters.gender) items.push(`${t.condGender} ${lower(genders[filters.gender] || filters.gender)}`)
  if (filters.use_case) items.push(`${t.condPurpose} ${lower(useCases[filters.use_case] || filters.use_case)}`)
  if (filters.feature) {
    const codes = Array.isArray(filters.feature) ? filters.feature : [filters.feature]
    items.push(`${t.condFeature} ${codes.map(code => lower(features[code] || code)).join(', ')}`)
  }
  if (filters.min_price != null && filters.min_price !== '') items.push(`${t.condFrom} $${filters.min_price}`)
  if (filters.max_price != null && filters.max_price !== '') items.push(`${t.condUpTo} $${filters.max_price}`)

  for (const brand of filters.exclude_brand || []) items.push(`${t.condExcept} ${brand}`)
  for (const [code, value] of Object.entries(filters.exclude_attributes || {})) {
    const values = Array.isArray(value) ? value : [value]
    const known = {
      color: item => colors[item] || item,
      purpose: item => useCases[item] || item,
      tags: item => features[item] || item,
      size: item => item,
    }[code]
    items.push(`${t.condExcept} ${values.map(item => lower(known ? known(item) : item)).join(', ')}`)
  }
  return items
}
