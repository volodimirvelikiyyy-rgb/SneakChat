import { formatProductCurrency } from './currency'

const MAX_COMPARE = 3

/** Сравнение 2–3 моделей по данным backend. Отсутствующее свойство помечается как «нет данных». */
export default function ComparePanel({ products, t, labels, onRemove, onClear, displayCurrency = 'UAH', rates }) {
  if (!products.length) return null
  const rows = [
    { key: 'price', label: t.compareRows.price, value: product => formatProductCurrency(product, displayCurrency, rates) },
    { key: 'brand', label: t.compareRows.brand, value: product => product.brand },
    { key: 'sizes', label: t.compareRows.sizes, value: product => (product.available_sizes?.length ? product.available_sizes.join(', ') : null) },
    { key: 'purpose', label: t.compareRows.purpose, value: product => (product.use_case ? labels.useCases[product.use_case] || product.use_case : null) },
    { key: 'color', label: t.compareRows.color, value: product => (product.color ? labels.colors[product.color] || product.color : null) },
    { key: 'gender', label: t.compareRows.gender, value: product => (product.gender ? labels.genders[product.gender] || product.gender : null) },
    { key: 'features', label: t.compareRows.features, value: product => (product.features?.length ? product.features.map(tag => labels.features[tag] || tag).join(', ') : null) },
    { key: 'stock', label: t.compareRows.stock, value: product => (product.total_stock > 0 ? t.inStock(product.total_stock) : t.soldOut) },
  ]
  return (
    <section className="compare-panel" aria-label={t.compareTitle}>
      <header>
        <b>{t.compareTitle}</b>
        <span>{t.compareHint(MAX_COMPARE)}</span>
        <button type="button" className="action ghost" onClick={onClear}>{t.compareClear}</button>
      </header>
      <div className="compare-scroll">
        <table className="compare-table">
          <thead>
            <tr>
              <th scope="col">{t.compareRows.field}</th>
              {products.map(product => (
                <th scope="col" key={product.id}>
                  <span className="compare-name">{product.brand} {product.name}</span>
                  <button type="button" className="compare-remove" onClick={() => onRemove(product.id)} aria-label={`${t.compareRemove}: ${product.name}`}>×</button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={row.key}>
                <th scope="row">{row.label}</th>
                {products.map(product => {
                  const value = row.value(product)
                  return <td key={product.id}>{value == null ? <span className="no-data">{t.noData}</span> : value}</td>
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
