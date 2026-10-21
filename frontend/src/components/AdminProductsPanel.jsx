import { useEffect, useMemo, useState } from 'react'
import ProductImage from './ProductImage'
import { resolveProductPhotos } from './productImages'
import { CURRENCIES, formatCurrency, currencySymbol } from './currency'

const API = '/api/admin'
const STATUS_LABELS = { published: 'Опубликован', hidden: 'Скрыт', draft: 'Черновик', deleted: 'В архиве' }
const EMPTY_FORM = {
  brand: '', name: '', color: '', price: '', currency: 'USD', description: '', gender: '', purpose: '',
  features: '', characteristics: [], variants: [{ size: 38, stock: 0 }], status: 'draft',
}

function toForm(product) {
  return {
    brand: product.brand || '', name: product.name || '', color: product.color || '',
    price: String(product.price ?? product.base_price ?? ''), currency: product.currency || 'USD', description: product.description || '',
    gender: product.gender || '', purpose: product.purpose || '',
    features: (product.features || []).join(', '),
    characteristics: (product.characteristics || []).map(item => ({ name: item.name, value: item.value })),
    variants: (product.variants || []).map(item => ({ size: item.size, stock: item.stock })),
    status: product.status || (product.active ? 'published' : 'hidden'),
  }
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) {
    let detail = 'Не удалось выполнить действие.'
    try { detail = (await response.json()).detail || detail } catch { /* empty response */ }
    throw new Error(detail)
  }
  if (response.status === 204) return null
  return response.json()
}

export default function AdminProductsPanel() {
  const [products, setProducts] = useState([])
  const [brand, setBrand] = useState('')
  const [model, setModel] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [stockFilter, setStockFilter] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [frames, setFrames] = useState({})

  const selected = useMemo(() => products.find(item => item.id === selectedId) || null, [products, selectedId])

  const loadProducts = async (preserveSelection = true) => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      if (brand.trim()) params.set('brand', brand.trim())
      if (model.trim()) params.set('model', model.trim())
      if (statusFilter) params.set('status', statusFilter)
      if (stockFilter) params.set('stock', stockFilter)
      const data = await api(`/products?${params}`)
      setProducts(data)
      if (preserveSelection && selectedId) {
        const fresh = data.find(item => item.id === selectedId)
        if (fresh) setForm(toForm(fresh))
      }
    } catch (reason) {
      setError(reason.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadProducts(false) }, [])

  const edit = product => {
    setSelectedId(product.id)
    setForm(toForm(product))
    setFrames(Object.fromEntries((product.photos || []).map(photo => [photo.id, {
      crop_scale: photo.crop_scale || 1, crop_x: photo.crop_x || 0, crop_y: photo.crop_y || 0,
    }])))
    setError('')
    setNotice('')
  }

  const startNew = () => {
    setSelectedId(null)
    setForm({ ...EMPTY_FORM, variants: [{ size: 38, stock: 0 }] })
    setFrames({})
    setError('')
    setNotice('Новый товар будет сохранён как черновик.')
  }

  const save = async event => {
    event.preventDefault()
    setSaving(true)
    setError('')
    setNotice('')
    const payload = {
      ...form,
      price: Number(form.price),
      currency: form.currency,
      features: form.features.split(',').map(value => value.trim()).filter(Boolean),
      characteristics: form.characteristics.filter(item => item.name.trim() && item.value.trim()),
      variants: form.variants.filter(item => item.size !== '' && item.size != null).map(item => ({ size: Number(item.size), stock: Number(item.stock || 0) })),
    }
    try {
      const saved = selectedId
        ? await api(`/products/${selectedId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        : await api('/products', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      setSelectedId(saved.id)
      await loadProducts(false)
      const fresh = await api(`/products/${saved.id}`)
      setProducts(current => current.map(item => item.id === fresh.id ? fresh : item))
      setForm(toForm(fresh))
      setNotice('Изменения сохранены.')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setSaving(false)
    }
  }

  const setField = (field, value) => setForm(current => ({ ...current, [field]: value }))
  const updateVariant = (index, field, value) => setForm(current => ({
    ...current, variants: current.variants.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item),
  }))
  const updateCharacteristic = (index, field, value) => setForm(current => ({
    ...current, characteristics: current.characteristics.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item),
  }))

  const uploadPhotos = async event => {
    const files = [...(event.target.files || [])]
    event.target.value = ''
    if (!selectedId || files.length === 0) {
      setError('Сначала сохраните товар, затем добавьте фотографии.')
      return
    }
    setSaving(true)
    setError('')
    try {
      for (const file of files) {
        await api(`/products/${selectedId}/photos`, {
          method: 'POST', headers: { 'Content-Type': file.type }, body: await file.arrayBuffer(),
        })
      }
      await loadProducts()
      setNotice(`Добавлено фотографий: ${files.length}.`)
    } catch (reason) {
      setError(reason.message)
    } finally {
      setSaving(false)
    }
  }

  const setPrimary = async photoId => {
    try {
      await api(`/products/${selectedId}/photos/${photoId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_primary: true }) })
      await loadProducts()
      setNotice('Главная фотография изменена.')
    } catch (reason) { setError(reason.message) }
  }

  const saveFrame = async photoId => {
    try {
      await api(`/products/${selectedId}/photos/${photoId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(frames[photoId]) })
      await loadProducts()
      setNotice('Кадрирование сохранено. Оригинал не менялся.')
    } catch (reason) { setError(reason.message) }
  }

  const movePhoto = async (photo, direction) => {
    const photos = [...(selected?.photos || [])].sort((a, b) => a.position - b.position)
    const index = photos.findIndex(item => item.id === photo.id)
    const target = index + direction
    if (target < 0 || target >= photos.length) return
    ;[photos[index], photos[target]] = [photos[target], photos[index]]
    try {
      await api(`/products/${selectedId}/photos/order`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ photo_ids: photos.map(item => item.id) }) })
      await loadProducts()
    } catch (reason) { setError(reason.message) }
  }

  const removePhoto = async photo => {
    if (!window.confirm('Убрать это фото из галереи? Исходный файл останется сохранён.')) return
    try {
      await api(`/products/${selectedId}/photos/${photo.id}`, { method: 'DELETE' })
      await loadProducts()
      setNotice('Фото убрано из галереи; исходник сохранён.')
    } catch (reason) { setError(reason.message) }
  }

  const archive = async product => {
    if (!window.confirm(`Переместить «${product.brand} ${product.name}» в архив? Товар можно восстановить.`)) return
    try {
      await api(`/products/${product.id}`, { method: 'DELETE' })
      if (selectedId === product.id) startNew()
      await loadProducts(false)
      setNotice('Товар перемещён в архив и скрыт из магазина.')
    } catch (reason) { setError(reason.message) }
  }

  const setPhotoFrame = (photo, key, value) => setFrames(current => ({
    ...current, [photo.id]: { ...(current[photo.id] || { crop_scale: 1, crop_x: 0, crop_y: 0 }), [key]: Number(value) },
  }))

  const importCurrentPhotos = async () => {
    if (!selected) return
    const filenames = resolveProductPhotos(selected).map(photo => {
      try { return decodeURIComponent(new URL(photo.original, window.location.origin).pathname.split('/').pop()) } catch { return '' }
    }).filter(filename => filename.toLowerCase().endsWith('.jpg'))
    try {
      await api(`/products/${selectedId}/photos/import-current`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filenames }) })
      await loadProducts()
      setNotice('Текущие фото скопированы в управляемую галерею. Исходники сохранены.')
    } catch (reason) { setError(reason.message) }
  }

  return (
    <section className="admin-products" aria-label="Управление товарами">
      <div className="admin-products-toolbar">
        <label>Бренд<input value={brand} onChange={event => setBrand(event.target.value)} placeholder="Например, Nike" /></label>
        <label>Модель<input value={model} onChange={event => setModel(event.target.value)} placeholder="Название модели" /></label>
        <label>Статус<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="">Все статусы</option>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>Наличие<select value={stockFilter} onChange={event => setStockFilter(event.target.value)}><option value="">Любое</option><option value="available">Есть размеры</option><option value="sold-out">Нет в наличии</option></select></label>
        <button type="button" className="action primary" onClick={() => loadProducts(false)}>Найти</button>
        <button type="button" className="action ghost" onClick={startNew}>＋ Новый товар</button>
      </div>
      {error && <p className="catalog-message error" role="alert">{error}</p>}
      {notice && <p className="admin-notice" role="status">{notice}</p>}
      <div className="admin-products-layout">
        <div className="admin-product-list">
          {loading && <p>Загрузка каталога…</p>}
          {!loading && products.length === 0 && <p className="catalog-message">По фильтрам товары не найдены.</p>}
          {!loading && products.map(product => (
            <article key={product.id} className={'admin-product-row' + (product.id === selectedId ? ' selected' : '')}>
              <ProductImage product={product} size="sm" />
              <div className="admin-product-row-info"><b>{product.brand} {product.name}</b><span>{product.color || 'Цвет не указан'} · {formatCurrency(product.price, product.currency)}</span><span>{product.total_stock} пар · {STATUS_LABELS[product.status]}</span></div>
              <div className="admin-product-row-actions"><button type="button" onClick={() => edit(product)}>Изменить</button><button type="button" onClick={() => archive(product)}>В архив</button></div>
            </article>
          ))}
        </div>

        <form className="admin-product-editor" onSubmit={save}>
          <div className="admin-editor-heading"><h2>{selectedId ? 'Карточка товара' : 'Новый товар'}</h2>{selected && <span>#{selected.id}</span>}</div>
          <div className="admin-form-grid">
            <label>Бренд<input required maxLength="50" value={form.brand} onChange={event => setField('brand', event.target.value)} /></label>
            <label>Модель<input required maxLength="120" value={form.name} onChange={event => setField('name', event.target.value)} /></label>
            <label>Цвет<input required maxLength="120" value={form.color} onChange={event => setField('color', event.target.value)} /></label>
            <label>Цена, {currencySymbol(form.currency)}<input required type="number" min="0" max="10000" step="0.01" value={form.price} onChange={event => setField('price', event.target.value)} /></label>
            <label>Валюта<select value={form.currency} onChange={event => setField('currency', event.target.value)}>{CURRENCIES.map(item => <option key={item.code} value={item.code}>{item.label}</option>)}</select></label>
            <label>Статус<select value={form.status} onChange={event => setField('status', event.target.value)}>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label>Пол<input value={form.gender} onChange={event => setField('gender', event.target.value)} placeholder="Например, unisex" /></label>
            <label>Назначение<input value={form.purpose} onChange={event => setField('purpose', event.target.value)} placeholder="Например, повседневные" /></label>
          </div>
          <label className="admin-form-wide">Описание<textarea rows="4" maxLength="4000" value={form.description} onChange={event => setField('description', event.target.value)} /></label>
          <label className="admin-form-wide">Особенности, через запятую<input value={form.features} onChange={event => setField('features', event.target.value)} placeholder="Амортизация, кожа" /></label>

          <div className="admin-editor-section"><div className="admin-section-heading"><h3>Характеристики</h3><button type="button" onClick={() => setField('characteristics', [...form.characteristics, { name: '', value: '' }])}>＋ Добавить</button></div>
            {form.characteristics.map((item, index) => <div className="admin-characteristic-row" key={`${item.code || 'new'}-${index}`}><input aria-label="Название характеристики" placeholder="Например, Материал" value={item.name} onChange={event => updateCharacteristic(index, 'name', event.target.value)} /><input aria-label="Значение характеристики" placeholder="Значение" value={item.value} onChange={event => updateCharacteristic(index, 'value', event.target.value)} /><button type="button" aria-label="Удалить характеристику" onClick={() => setField('characteristics', form.characteristics.filter((_, row) => row !== index))}>×</button></div>)}
          </div>

          <div className="admin-editor-section"><div className="admin-section-heading"><h3>Размеры и остатки</h3><button type="button" onClick={() => setField('variants', [...form.variants, { size: '', stock: 0 }])}>＋ Добавить размер</button></div>
            {form.variants.map((variant, index) => <div className="admin-size-row" key={index}><label>Размер<input type="number" min="34" max="50" step="0.5" value={variant.size} onChange={event => updateVariant(index, 'size', event.target.value)} /></label><label>Количество<input type="number" min="0" max="999" value={variant.stock} onChange={event => updateVariant(index, 'stock', event.target.value)} /></label><button type="button" aria-label="Убрать размер" onClick={() => setField('variants', form.variants.filter((_, row) => row !== index))}>Убрать</button></div>)}
          </div>

          <div className="admin-editor-section"><div className="admin-section-heading"><h3>Фотографии товара</h3>{selectedId && <label className="admin-upload-button">Добавить JPG, PNG или WebP<input type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={uploadPhotos} disabled={saving} /></label>}</div>
            {!selectedId && <p>Сохраните карточку, чтобы загрузить фотографии.</p>}
            {selectedId && !selected?.has_managed_photos && <div className="admin-legacy-gallery"><p>Сейчас витрина использует эти изображения. Нажмите кнопку, чтобы добавить их копии в редактируемую галерею.</p><div>{resolveProductPhotos(selected || {}).map(photo => <figure key={photo.original}><img src={photo.preview} alt={photo.alt} /><figcaption>{photo.alt}</figcaption></figure>)}</div><button type="button" className="action ghost" onClick={importCurrentPhotos}>Добавить текущие фото в галерею</button></div>}
            {selectedId && selected?.has_managed_photos && (selected?.photos || []).length === 0 && <p>В галерее нет активных фотографий. Сохранённые исходники остаются на диске.</p>}
            <div className="admin-photo-grid">{(selected?.photos || []).map((photo, index) => {
              const frame = frames[photo.id] || { crop_scale: photo.crop_scale || 1, crop_x: photo.crop_x || 0, crop_y: photo.crop_y || 0 }
              const style = { transform: `translate(${frame.crop_x}%, ${frame.crop_y}%) scale(${frame.crop_scale})` }
              return <article className="admin-photo-card" key={photo.id}>
                <div className="admin-photo-preview"><img src={photo.url} alt={`Фото ${index + 1} ${form.brand} ${form.name}`} style={style} />{photo.is_primary && <span>Главное</span>}</div>
                <div className="admin-photo-controls"><button type="button" onClick={() => setPrimary(photo.id)} disabled={photo.is_primary}>Сделать главным</button><div><button type="button" onClick={() => movePhoto(photo, -1)} disabled={index === 0} aria-label="Переместить фото влево">←</button><button type="button" onClick={() => movePhoto(photo, 1)} disabled={index === (selected.photos.length - 1)} aria-label="Переместить фото вправо">→</button><button type="button" onClick={() => removePhoto(photo)}>Убрать</button></div></div>
                <label>Масштаб <input type="range" min="1" max="2.5" step="0.05" value={frame.crop_scale} onChange={event => setPhotoFrame(photo, 'crop_scale', event.target.value)} /></label>
                <label>Сдвиг по горизонтали <input type="range" min="-50" max="50" step="1" value={frame.crop_x} onChange={event => setPhotoFrame(photo, 'crop_x', event.target.value)} /></label>
                <label>Сдвиг по вертикали <input type="range" min="-50" max="50" step="1" value={frame.crop_y} onChange={event => setPhotoFrame(photo, 'crop_y', event.target.value)} /></label>
                <button type="button" className="action ghost" onClick={() => saveFrame(photo.id)}>Сохранить кадрирование</button>
              </article>
            })}</div>
          </div>

          <div className="admin-preview-block"><h3>Предпросмотр</h3><div className="admin-product-preview"><ProductImage size="sm" product={selected ? { ...selected, photos: selected.photos } : { ...form, image_key: 'fallback' }} /><div><b>{form.brand || 'Бренд'} {form.name || 'Модель'}</b><span>{form.color || 'Цвет'} · {formatCurrency(form.price || 0, form.currency)}</span><p>{form.description || 'Описание товара'}</p></div></div></div>
          <div className="admin-editor-actions"><button type="submit" className="action primary" disabled={saving}>{saving ? 'Сохраняем…' : 'Сохранить изменения'}</button>{selected && <button type="button" className="action ghost" onClick={() => { const status = selected.status === 'published' ? 'hidden' : 'published'; api(`/products/${selected.id}/status`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) }).then(() => loadProducts()).then(() => setNotice(status === 'published' ? 'Товар опубликован.' : 'Товар скрыт.')).catch(reason => setError(reason.message)) }}>{selected.status === 'published' ? 'Скрыть товар' : 'Опубликовать'}</button>}</div>
        </form>
      </div>
    </section>
  )
}
