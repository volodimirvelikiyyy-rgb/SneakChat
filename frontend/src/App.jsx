import { useEffect, useRef, useState } from 'react'
import ReadAloudButton from './components/ReadAloudButton'
import ProductCard from './components/ProductCard'
import ProductDetails from './components/ProductDetails'
import ComparePanel from './components/ComparePanel'
import { CURRENCIES, formatCurrency } from './components/currency'
import AdminProductsPanel from './components/AdminProductsPanel'
import { EmptyState, ErrorState, Searching } from './components/StateBlocks'
import { EXTRA_COPY } from './lib/uiCopy'
import { buildConditions } from './lib/conditions'

const API_BASE = '/api'
const API_URL = `${API_BASE}/products`
const CHAT_URL = `${API_BASE}/chat`
const CHAT_LOGS_URL = `${API_BASE}/chat/logs`
const REQUEST_URL = `${API_BASE}/requests`
const filtersInitial = { brand: '', color: '', size: '', min_price: '', max_price: '', currency: '', gender: '', use_case: '', feature: '' }
const statusLabels = { new: 'Новая', contacted: 'Связались', completed: 'Завершена', cancelled: 'Отменена' }
const UI = {
  ru: {
    sizes: 'Размеры', available: (sizes, stock) => `Доступно размеров: ${sizes} · ${stock} пар`, soldOut: 'Нет в наличии', loadingCatalog: 'Загружаем каталог', loadingProduct: 'Загружаем карточку…', productError: 'Не удалось открыть товар. Попробуйте ещё раз.', color: 'Цвет', purpose: 'Назначение', features: 'Подходят', chooseSize: 'Выберите размер', unavailableSizes: 'Недоступные размеры отмечены и не выбираются.', none: 'Нет', total: 'Итого за', leaveRequest: 'Оставить заявку', chooseFirst: 'Сначала выберите размер', formTitle: 'Контакт для связи', name: 'Имя', namePlaceholder: 'Как к вам обращаться', contact: 'Телефон или Telegram', contactPlaceholder: '@username или номер телефона', comment: 'Комментарий', optional: '(необязательно)', commentPlaceholder: 'Например, удобное время для связи', sending: 'Отправляем…', send: 'Отправить заявку', privacy: 'Мы используем данные только для ответа на эту заявку.', requestError: 'Не удалось отправить заявку. Проверьте соединение и повторите попытку.', success: 'Заявка принята', successText: (name, size, price) => `Спасибо! Сохранили ${name}, размер ${size}, за ${price}. Свяжемся по указанному контакту.`, back: 'Вернуться в каталог', assistantLabel: 'AI-консультант', closeAssistant: 'Закрыть консультанта', personal: 'ПЕРСОНАЛЬНЫЙ ПОДБОР', findPair: 'Найдём вашу пару', trust: 'Товары, цены, размеры и наличие всегда проверяются по каталогу.', aiMode: 'AI-подбор', fallbackMode: 'Подбор по каталогу', inStock: stock => `${stock} пар в наличии`, selectSize: 'Выбрать размер', query: 'Ваш запрос', queryPlaceholder: 'Ваше сообщение', selecting: 'Отвечаем…', select: 'Отправить', unavailable: 'Помощник сейчас недоступен. Запрос сохранён — попробуйте ещё раз или воспользуйтесь фильтрами каталога.', initial: 'Здравствуйте! Чем могу вам помочь?', catalog: 'Каталог', selectWithAi: 'Подобрать с AI', requests: 'Заявки', stockTag: 'ТОВАРЫ В НАЛИЧИИ', heroTag: 'SNEAKER SELECTION SERVICE', heroTitle: <>Найдите пару,<br /><i>которая подходит вам.</i></>, heroText: 'Подбирайте кроссовки по бренду, цвету, размеру и бюджету — с актуальным наличием в каталоге.', viewCatalog: 'Смотреть каталог', collection: 'КОЛЛЕКЦИЯ', updating: 'Обновляем…', models: count => `${count} моделей`, brand: 'Бренд: Nike', filterColor: 'Цвет: White', size: 'Размер', anyGender: 'Любой пол', anyPurpose: 'Любое назначение', anyFeature: 'Любой сценарий', minPrice: 'Цена от', maxPrice: 'Цена до', currency: 'Валюта цены', allCurrencies: 'Любая валюта', search: 'Найти', reset: 'Сбросить', catalogUnavailable: 'Каталог временно недоступен', catalogError: 'Не удалось загрузить каталог. Убедитесь, что backend запущен на порту 8001.', retry: 'Повторить', nothing: 'Ничего не найдено', nothingText: 'Попробуйте изменить бренд, размер, цену или сбросить фильтры.', resetFilters: 'Сбросить фильтры', close: 'Закрыть', productDescription: product => `${product.brand} ${product.name} — модель из демонстрационного каталога.`
  },
  uk: {
    sizes: 'Розміри', available: (sizes, stock) => `Доступно розмірів: ${sizes} · ${stock} пар`, soldOut: 'Немає в наявності', loadingCatalog: 'Завантажуємо каталог', loadingProduct: 'Завантажуємо картку…', productError: 'Не вдалося відкрити товар. Спробуйте ще раз.', color: 'Колір', purpose: 'Призначення', features: 'Підходять', chooseSize: 'Оберіть розмір', unavailableSizes: 'Недоступні розміри позначені та не обираються.', none: 'Немає', total: 'Разом за', leaveRequest: 'Залишити заявку', chooseFirst: 'Спочатку оберіть розмір', formTitle: 'Контакт для зв’язку', name: 'Ім’я', namePlaceholder: 'Як до вас звертатися', contact: 'Телефон або Telegram', contactPlaceholder: '@username або номер телефону', comment: 'Коментар', optional: '(необов’язково)', commentPlaceholder: 'Наприклад, зручний час для зв’язку', sending: 'Надсилаємо…', send: 'Надіслати заявку', privacy: 'Ми використовуємо дані лише для відповіді на цю заявку.', requestError: 'Не вдалося надіслати заявку. Перевірте з’єднання та спробуйте ще раз.', success: 'Заявку прийнято', successText: (name, size, price) => `Дякуємо! Зберегли ${name}, розмір ${size}, за ${price}. Зв’яжемося за вказаним контактом.`, back: 'Повернутися до каталогу', assistantLabel: 'AI-консультант', closeAssistant: 'Закрити консультанта', personal: 'ПЕРСОНАЛЬНИЙ ПІДБІР', findPair: 'Знайдемо вашу пару', trust: 'Товари, ціни, розміри та наявність завжди перевіряються за каталогом.', aiMode: 'AI-підбір', fallbackMode: 'Підбір за каталогом', inStock: stock => `${stock} пар у наявності`, selectSize: 'Обрати розмір', query: 'Ваш запит', queryPlaceholder: 'Ваше повідомлення', selecting: 'Відповідаємо…', select: 'Надіслати', unavailable: 'Помічник зараз недоступний. Запит збережено — спробуйте ще раз або скористайтеся фільтрами каталогу.', initial: 'Вітаю! Чим можу вам допомогти?', catalog: 'Каталог', selectWithAi: 'Підібрати з AI', requests: 'Заявки', stockTag: 'ТОВАРИ В НАЯВНОСТІ', heroTag: 'SNEAKER SELECTION SERVICE', heroTitle: <>Знайдіть пару,<br /><i>яка пасує саме вам.</i></>, heroText: 'Підбирайте кросівки за брендом, кольором, розміром і бюджетом — з актуальною наявністю в каталозі.', viewCatalog: 'Переглянути каталог', collection: 'КОЛЕКЦІЯ', updating: 'Оновлюємо…', models: count => `${count} моделей`, brand: 'Бренд: Nike', filterColor: 'Колір: White', size: 'Розмір', anyGender: 'Будь-яка категорія', anyPurpose: 'Будь-яке призначення', anyFeature: 'Будь-який сценарій', minPrice: 'Ціна від', maxPrice: 'Ціна до', currency: 'Валюта ціни', allCurrencies: 'Будь-яка валюта', search: 'Знайти', reset: 'Скинути', catalogUnavailable: 'Каталог тимчасово недоступний', catalogError: 'Не вдалося завантажити каталог. Переконайтеся, що backend запущено на порту 8001.', retry: 'Повторити', nothing: 'Нічого не знайдено', nothingText: 'Спробуйте змінити бренд, розмір, ціну або скинути фільтри.', resetFilters: 'Скинути фільтри', close: 'Закрити', productDescription: product => `${product.brand} ${product.name} — модель із демонстраційного каталогу.`
  }
}
const PRODUCT_LABELS = {
  ru: { genders: { Men: 'Мужские', Women: 'Женские', Unisex: 'Унисекс' }, colors: { White: 'Белый', Black: 'Чёрный', Blue: 'Синий', Grey: 'Серый', Green: 'Зелёный', Red: 'Красный', Beige: 'Бежевый', Cream: 'Кремовый', Silver: 'Серебристый', Brown: 'Коричневый', Orange: 'Оранжевый', Olive: 'Оливковый' }, useCases: { Running: 'Для бега', Walking: 'Для прогулок', Training: 'Для зала', Trail: 'Для трекинга', Lifestyle: 'На каждый день' }, features: { school: 'Для школы', autumn: 'На осень', water_resistant: 'Защита от влаги' } },
  uk: { genders: { Men: 'Чоловічі', Women: 'Жіночі', Unisex: 'Унісекс' }, colors: { White: 'Білий', Black: 'Чорний', Blue: 'Синій', Grey: 'Сірий', Green: 'Зелений', Red: 'Червоний', Beige: 'Бежевий', Cream: 'Кремовий', Silver: 'Сріблястий', Brown: 'Коричневий', Orange: 'Помаранчевий', Olive: 'Оливковий' }, useCases: { Running: 'Для бігу', Walking: 'Для прогулянок', Training: 'Для залу', Trail: 'Для трекінгу', Lifestyle: 'На щодень' }, features: { school: 'Для школи', autumn: 'На осінь', water_resistant: 'Захист від вологи' } }
}
const createChatSessionId = () => window.crypto?.randomUUID?.() || `session-${Date.now()}`
const getStoredChatSessionId = () => {
  const stored = window.sessionStorage.getItem('stride-chat-session')
  if (stored) return stored
  const created = createChatSessionId()
  window.sessionStorage.setItem('stride-chat-session', created)
  return created
}
const initialLanguage = window.localStorage.getItem('stride-language') === 'uk' ? 'uk' : 'ru'

function CatalogSkeleton({ t }) {
  return <div className="grid skeleton-grid" aria-label={t.loadingCatalog}>{Array.from({ length: 8 }, (_, index) => <div className="skeleton-card" key={index}><div /><span /><span /><span /></div>)}</div>
}

function AiAssistant({ chatMessages, message, setMessage, loading, error, onSubmit, onOpenProduct, onClose, t, labels, displayCurrency, rates }) {
  return <aside className="chat" aria-label={t.assistantLabel}><button className="close" onClick={onClose} aria-label={t.closeAssistant}>×</button><div className="chat-history" aria-live="polite">{chatMessages.map((item, index) => <div className={`chat-message ${item.role}`} key={index}><div className="chat-message-text">{item.needsClarification ? <p className="notice-text">{item.text}</p> : <p>{item.text}</p>}{item.role === 'assistant' && index > 0 && <ReadAloudButton text={item.text} className="chat-read-button" />}</div>{index === chatMessages.length - 1 && item.products?.length > 0 && <div className="chat-cards"><ChatConditions filters={item.filters} t={t} labels={labels} />{item.products.map(product => <ProductCard key={product.id} product={product} t={t} variant="chat" selectedSize={item.filters?.size} budgetMax={item.filters?.max_price} budgetCurrency={item.filters?.currency || displayCurrency} displayCurrency={displayCurrency} rates={rates} onOpen={onOpenProduct} />)}</div>}</div>)}</div>{loading && <Searching t={t} />}<form onSubmit={onSubmit} className="chat-form"><input value={message} onChange={event => setMessage(event.target.value)} placeholder={t.queryPlaceholder} aria-label={t.query} /><button disabled={loading}>{loading ? t.selecting : t.select}</button></form>{error && <p className="chat-error" role="alert">{t.unavailable}</p>}</aside>
}

/**
 * Условия, по которым backend действительно искал. Показываются один раз над карточками:
 * причина в карточке отвечает на вопрос «почему эта модель», а не «почему эти модели».
 */
function ChatConditions({ filters, t, labels }) {
  const conditions = buildConditions(filters, t, labels)
  if (!conditions.length) return null
  return <p className="chat-conditions"><b>{t.conditionsLabel}</b> {conditions.join(' · ')}</p>
}

function AdminGate() {
  const [ready, setReady] = useState(false)
  const [configured, setConfigured] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [section, setSection] = useState('products')

  const checkSession = async () => {
    try {
      const response = await fetch('/api/admin/me')
      const state = await response.json()
      setConfigured(state.configured)
      setAuthenticated(state.authenticated)
      setError('')
    } catch {
      setError('Не удалось подключиться к локальному backend.')
    } finally {
      setReady(true)
    }
  }

  useEffect(() => { checkSession() }, [])

  const signIn = async event => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const response = await fetch('/api/admin/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail || 'Не удалось войти.')
      setPassword('')
      setAuthenticated(true)
    } catch (reason) {
      setError(reason.message)
    } finally {
      setBusy(false)
    }
  }

  const signOut = async () => {
    try { await fetch('/api/admin/logout', { method: 'POST' }) } finally { setAuthenticated(false); setSection('products') }
  }

  if (!ready) return <main className="admin-login"><p>Проверяем локальный доступ…</p></main>
  if (!configured) return <main className="admin-login"><section><p className="eyebrow">SNEAKCHAT · ЛОКАЛЬНО</p><h1>Настройка входа владельца</h1><p>Пароль ещё не настроен. Установите его в терминале командой из папки backend:</p><code>python -m app.admin_setup</code><p>Пароль хранится только в профиле Windows, в браузер он не сохраняется.</p>{error && <p role="alert">{error}</p>}</section></main>
  if (!authenticated) return <main className="admin-login"><form onSubmit={signIn}><p className="eyebrow">SNEAKCHAT · ЛОКАЛЬНО</p><h1>Вход владельца</h1><p>Админка доступна только на этом компьютере.</p><label>Пароль<input autoComplete="current-password" type="password" maxLength="256" value={password} onChange={event => setPassword(event.target.value)} required /></label>{error && <p className="admin-login-error" role="alert">{error}</p>}<button className="action primary" disabled={busy}>{busy ? 'Проверяем…' : 'Войти'}</button></form></main>

  if (section === 'operations') return <AdminView onBackToProducts={() => setSection('products')} />
  return <div className="admin"><header className="admin-head"><div><p className="eyebrow">STRIDE ADMIN</p><h1>Управление каталогом</h1></div><div className="admin-header-actions"><button type="button" onClick={() => setSection('operations')}>Заявки и диалоги</button><a href="/">← В магазин</a><button type="button" onClick={signOut}>Выйти</button></div></header><AdminProductsPanel /></div>
}

function AdminView({ onBackToProducts }) {
  const [requests, setRequests] = useState([])
  const [chatLogs, setChatLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [logsLoading, setLogsLoading] = useState(true)
  const [error, setError] = useState('')
  const [logsError, setLogsError] = useState('')
  const [adminTab, setAdminTab] = useState('requests')

  const loadRequests = async () => {
    setLoading(true)
    setError('')
    try {
      const response = await fetch(REQUEST_URL)
      if (!response.ok) throw new Error()
      setRequests(await response.json())
    } catch {
      setError('Не удалось загрузить заявки. Проверьте backend и повторите попытку.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadRequests() }, [])

  const loadChatLogs = async () => {
    setLogsLoading(true)
    setLogsError('')
    try {
      const response = await fetch(CHAT_LOGS_URL)
      if (!response.ok) throw new Error()
      setChatLogs(await response.json())
    } catch {
      setLogsError('Не удалось загрузить диалоги. Проверьте backend и повторите попытку.')
    } finally {
      setLogsLoading(false)
    }
  }

  useEffect(() => { loadChatLogs() }, [])

  const updateStatus = async (id, status) => {
    const previous = requests
    setRequests(current => current.map(item => item.id === id ? { ...item, status } : item))
    try {
      const response = await fetch(`${REQUEST_URL}/${id}/status`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
      if (!response.ok) throw new Error()
    } catch {
      setRequests(previous)
      setError('Не удалось обновить статус заявки.')
    }
  }

  const deleteRequest = async id => {
    const previous = requests
    setRequests(current => current.filter(item => item.id !== id))
    try {
      const response = await fetch(`${REQUEST_URL}/${id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error()
    } catch {
      setRequests(previous)
      setError('Не удалось удалить заявку.')
    }
  }

  return <div className="admin"><header className="admin-head"><div><p className="eyebrow">STRIDE ADMIN</p><h1>{adminTab === 'requests' ? 'Заявки' : 'Диалоги'}</h1></div><div className="admin-header-actions">{onBackToProducts && <button type="button" onClick={onBackToProducts}>← Товары</button>}<a href="/">В магазин</a></div></header><div className="admin-tabs" role="tablist" aria-label="Разделы админки"><button type="button" className={adminTab === 'requests' ? 'active' : ''} onClick={() => setAdminTab('requests')}>Заявки</button><button type="button" className={adminTab === 'chats' ? 'active' : ''} onClick={() => setAdminTab('chats')}>Диалоги тестеров</button></div>{adminTab === 'requests' && <>{loading && <p>Загружаем заявки…</p>}{error && <p className="catalog-message error" role="alert">{error}</p>}{!loading && !error && requests.length === 0 && <p className="catalog-message">Заявок пока нет.</p>}{!loading && requests.length > 0 && <div className="admin-table-wrap"><table><thead><tr><th>Дата</th><th>Клиент</th><th>Контакт</th><th>Товар</th><th>Размер</th><th>Цена</th><th>Комментарий</th><th>Статус</th><th>Действия</th></tr></thead><tbody>{requests.map(request => <tr key={request.id}><td>{request.created_at}</td><td>{request.name}</td><td className="admin-long">{request.contact}</td><td>{request.product || 'Товар не найден'}</td><td>{request.size || '—'}</td><td>{formatCurrency(request.price, request.currency)}</td><td className="admin-long">{request.comment || '—'}</td><td><span className={`request-status ${request.status}`}>{statusLabels[request.status] || request.status}</span></td><td><div className="admin-actions"><span className={`status-control ${request.status}`}><select value={request.status} aria-label={`Статус заявки ${request.id}`} onChange={event => updateStatus(request.id, event.target.value)}>{Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></span><button type="button" className="delete-request" aria-label={`Удалить заявку ${request.id}`} onClick={() => deleteRequest(request.id)}>Удалить</button></div></td></tr>)}</tbody></table></div>}</>}{adminTab === 'chats' && <>{logsLoading && <p>Загружаем диалоги…</p>}{logsError && <p className="catalog-message error" role="alert">{logsError}</p>}{!logsLoading && !logsError && chatLogs.length === 0 && <p className="catalog-message">Диалогов пока нет. Попросите тестера открыть AI-консультанта и написать пару обычных запросов.</p>}{!logsLoading && !logsError && chatLogs.length > 0 && <div className="admin-table-wrap"><table className="chat-log-table"><thead><tr><th>Дата</th><th>Язык</th><th>Запрос покупателя</th><th>Ответ бота</th><th>Товары</th><th>Сигнал</th><th>Подсказки</th></tr></thead><tbody>{chatLogs.map(log => <tr key={log.id}><td>{log.created_at}</td><td>{log.language.toUpperCase()}</td><td className="admin-long">{log.user_message}</td><td className="admin-long">{log.assistant_reply}</td><td className="admin-long">{log.product_count ? `${log.product_count}: ${log.product_names}` : 'Не показал'}</td><td><span className={`chat-signal ${log.needs_clarification ? 'clarify' : log.product_count ? 'found' : 'empty'}`}>{log.needs_clarification ? 'Уточнение' : log.product_count ? 'Показал товары' : 'Пусто'}</span></td><td className="admin-long">{log.suggestions?.length ? log.suggestions.join(', ') : '—'}</td></tr>)}</tbody></table></div>}</>}</div>
}

export default function App() {
  const [language, setLanguage] = useState(initialLanguage)
  const [displayCurrency, setDisplayCurrency] = useState(() => {
    const saved = window.localStorage.getItem('stride-currency')
    return CURRENCIES.some(item => item.code === saved) ? saved : 'UAH'
  })
  const [currencyRates, setCurrencyRates] = useState(null)
  const [rateDate, setRateDate] = useState('')
  const [ratesUnavailable, setRatesUnavailable] = useState(false)
  const t = { ...UI[language], ...EXTRA_COPY[language] }
  const labels = PRODUCT_LABELS[language]
  const [products, setProducts] = useState([])
  const [filters, setFilters] = useState(filtersInitial)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [chatOpen, setChatOpen] = useState(false)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [message, setMessage] = useState('')
  const [chatMessages, setChatMessages] = useState([{ role: 'assistant', text: UI[initialLanguage].initial }])
  const [chatSessionId, setChatSessionId] = useState(getStoredChatSessionId)
  const [assistantError, setAssistantError] = useState('')
  const [compare, setCompare] = useState([])
  const compareRef = useRef(null)
  const [assistantLoading, setAssistantLoading] = useState(false)
  const chatGeneration = useRef(0)
  const [selectedProduct, setSelectedProduct] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const loadProducts = async (active = filtersInitial, targetCurrency = displayCurrency) => { setLoading(true); setError(''); const params = new URLSearchParams(Object.entries({ ...active, currency: active.min_price || active.max_price ? targetCurrency : '', display_currency: targetCurrency }).filter(([, value]) => value !== '')); try { const response = await fetch(`${API_URL}/search?${params}`); if (!response.ok) throw new Error(); setProducts(await response.json()) } catch { setError(t.catalogError) } finally { setLoading(false) } }
  useEffect(() => {
    let cancelled = false
    fetch('/api/currency/rates').then(response => response.ok ? response.json() : Promise.reject()).then(result => {
      if (cancelled) return
      setCurrencyRates(result.available ? result.rates : null)
      setRateDate(result.rate_date || '')
      setRatesUnavailable(!result.available)
    }).catch(() => { if (!cancelled) { setCurrencyRates(null); setRatesUnavailable(true) } })
    return () => { cancelled = true }
  }, [])
  useEffect(() => { loadProducts(filters) }, [displayCurrency, currencyRates])
  useEffect(() => { document.documentElement.lang = language }, [language])
  const update = event => { const { name, value } = event.target; setFilters(current => ({ ...current, [name]: value })) }
  const submit = event => { event.preventDefault(); loadProducts(filters) }
  const reset = () => { setFilters(filtersInitial); loadProducts(filtersInitial) }
  const openProduct = async productId => {
    // Причина подбора строится поиском, а карточка товара её не содержит: берём её из уже
    // полученных данных списка или чата, чтобы показать покупателю, почему модель подошла.
    const known = products.find(item => item.id === productId)
      || chatMessages.flatMap(item => item.products || []).find(item => item.id === productId)
    setSelectedProduct(null)
    setDetailError('')
    setDetailLoading(true)
    try {
      const response = await fetch(`${API_URL}/${productId}?display_currency=${displayCurrency}`)
      if (!response.ok) throw new Error()
      const detail = await response.json()
      setSelectedProduct(detail.match_reasons?.length ? detail : { ...detail, match_reasons: known?.match_reasons || [] })
    } catch { setDetailError(t.productError) } finally { setDetailLoading(false) }
  }
  const resetChat = () => {
    const nextSessionId = createChatSessionId()
    chatGeneration.current += 1
    window.sessionStorage.setItem('stride-chat-session', nextSessionId)
    setChatSessionId(nextSessionId)
    setChatMessages([{ role: 'assistant', text: UI[language].initial }])
    setMessage('')
    setAssistantError('')
    setAssistantLoading(false)
  }
  const closeChat = () => { resetChat(); setChatOpen(false) }
  const chooseFromAssistant = productId => { setChatOpen(false); openProduct(productId) }
  const askAssistant = async (event, quickPrompt = '') => { event?.preventDefault(); const userMessage = (quickPrompt || message).trim(); if (!userMessage || assistantLoading) return; const requestGeneration = chatGeneration.current; setAssistantLoading(true); setAssistantError(''); setMessage(''); setChatMessages(current => [...current, { role: 'user', text: userMessage }]); try { const response = await fetch(CHAT_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: `${chatSessionId}-${language}`, message: userMessage, language, display_currency: displayCurrency }) }); if (!response.ok) throw new Error(); const result = await response.json(); if (requestGeneration !== chatGeneration.current) return; setChatMessages(current => [...current, { role: 'assistant', text: result.reply, products: result.products, mode: result.mode, suggestions: result.suggestions, needsClarification: result.needs_clarification, filters: result.filters }]) } catch { if (requestGeneration !== chatGeneration.current) return; setMessage(userMessage); setAssistantError('unavailable') } finally { if (requestGeneration === chatGeneration.current) setAssistantLoading(false) } }
  const changeCurrency = event => { const next = event.target.value; setDisplayCurrency(next); window.localStorage.setItem('stride-currency', next) }
  const changeLanguage = nextLanguage => { chatGeneration.current += 1; setAssistantLoading(false); setLanguage(nextLanguage); window.localStorage.setItem('stride-language', nextLanguage); setChatMessages([{ role: 'assistant', text: UI[nextLanguage].initial }]); setMessage(''); setAssistantError(''); document.documentElement.lang = nextLanguage }
  // Сравнение: только объекты, уже полученные из backend. Логика выбора — в backend.
  useEffect(() => {
    if (compare.length === 1) compareRef.current?.scrollIntoView({ block: 'nearest' })
  }, [compare.length])
  const toggleCompare = id => setCompare(current => {
    if (current.some(item => item.id === id)) return current.filter(item => item.id !== id)
    const source = products.find(item => item.id === id) || (selectedProduct?.id === id ? selectedProduct : null)
    if (!source || current.length >= 3) return current
    return [...current, source]
  })
  if (window.location.pathname === '/admin') return <AdminGate />
  return <div className="store"><header><a className="logo" href="#top">STRIDE<span>.</span></a><nav className="desktop-navigation"><a href="#catalog">{t.catalog}</a><button type="button" onClick={() => setChatOpen(true)}>{t.selectWithAi}</button><a href="/admin">{t.ownerAccess}</a></nav><div className="header-tools"><div className="language-switch" aria-label={t.languageSwitch}><button className={language === 'ru' ? 'active' : ''} onClick={() => changeLanguage('ru')}>RU</button><button className={language === 'uk' ? 'active' : ''} onClick={() => changeLanguage('uk')}>UA</button></div><label className="currency-switch"><span className="visually-hidden">{t.currencySwitch}</span><select aria-label={t.currencySwitch} value={displayCurrency} onChange={changeCurrency}>{CURRENCIES.map(item => <option value={item.code} key={item.code}>{item.code} ({item.symbol})</option>)}</select></label><button type="button" className="mobile-menu-toggle" aria-label={mobileMenuOpen ? t.closeMenu : t.openMenu} aria-expanded={mobileMenuOpen} aria-controls="mobile-navigation" onClick={() => setMobileMenuOpen(open => !open)}><span aria-hidden="true">{mobileMenuOpen ? "×" : "☰"}</span></button><div className="tag">{t.stockTag}</div></div><nav id="mobile-navigation" className={`mobile-navigation ${mobileMenuOpen ? "open" : ""}`} aria-label={t.mobileNavigation}><a href="#catalog" onClick={() => setMobileMenuOpen(false)}>{t.catalog}</a><button type="button" onClick={() => { setMobileMenuOpen(false); setChatOpen(true) }}>{t.selectWithAi}</button><a href="/admin">{t.ownerAccess}</a></nav></header>{(rateDate || ratesUnavailable) && <p className="rate-note" role="status">{rateDate ? `${t.rateDate(rateDate)}${ratesUnavailable ? ` · ${t.savedRate}` : ""}` : t.ratesUnavailable}</p>}<main id="top"><section className="hero"><p className="eyebrow">{t.heroTag}</p><h1>{t.heroTitle}</h1><p>{t.heroText}</p><div className="hero-actions"><button type="button" onClick={() => setChatOpen(true)}>{t.selectWithAi} <span>↗</span></button><a href="#catalog">{t.viewCatalog}</a></div></section><section className="catalog" id="catalog"><div className="catalog-title"><div><p className="eyebrow">{t.collection}</p><h2>{t.catalog}</h2></div><p>{loading ? t.updating : t.models(products.length)}</p></div><form className="filters" onSubmit={submit}><input name="brand" value={filters.brand} onChange={update} placeholder={t.brand} aria-label={t.brand} /><input name="color" value={filters.color} onChange={update} placeholder={t.filterColor} aria-label={t.filterColor} /><input name="size" type="number" value={filters.size} onChange={update} placeholder={t.size} aria-label={t.size} /><select name="gender" value={filters.gender} onChange={update} aria-label={t.anyGender}><option value="">{t.anyGender}</option><option value="Men">{labels.genders.Men}</option><option value="Women">{labels.genders.Women}</option><option value="Unisex">{labels.genders.Unisex}</option></select><select name="use_case" value={filters.use_case} onChange={update} aria-label={t.anyPurpose}><option value="">{t.anyPurpose}</option><option value="Running">{labels.useCases.Running}</option><option value="Walking">{labels.useCases.Walking}</option><option value="Training">{labels.useCases.Training}</option><option value="Trail">{labels.useCases.Trail}</option><option value="Lifestyle">{labels.useCases.Lifestyle}</option></select><input name="min_price" type="number" value={filters.min_price} onChange={update} placeholder={t.minPrice} aria-label={t.minPrice} /><input name="max_price" type="number" value={filters.max_price} onChange={update} placeholder={t.maxPrice} aria-label={t.maxPrice} /><button>{t.search}</button><button className="reset" type="button" onClick={reset}>{t.reset}</button></form>{loading && <CatalogSkeleton t={t} />}{error && <ErrorState t={t} onRetry={() => loadProducts(filters)} />}{!loading && !error && products.length > 0 && <div className="grid">{products.map(product => <ProductCard product={product} t={t} displayCurrency={displayCurrency} rates={currencyRates} selectedSize={Number(filters.size) || null} onOpen={openProduct} onToggleCompare={toggleCompare} inCompare={compare.some(item => item.id === product.id)} key={product.id} />)}</div>}{!loading && !error && products.length === 0 && <EmptyState t={t} onReset={reset} />}</section></main><button className="assistant-button" onClick={() => chatOpen ? closeChat() : setChatOpen(true)} aria-expanded={chatOpen}><span>✦</span> {t.selectWithAi}</button>{chatOpen && <AiAssistant chatMessages={chatMessages} message={message} setMessage={setMessage} loading={assistantLoading} error={assistantError} onSubmit={askAssistant} onOpenProduct={chooseFromAssistant} onClose={closeChat} t={t} labels={labels} displayCurrency={displayCurrency} rates={currencyRates} />}{(detailLoading || selectedProduct || detailError) && <ProductDetails product={selectedProduct} loading={detailLoading} error={detailError} onClose={() => { setSelectedProduct(null); setDetailLoading(false); setDetailError('') }} t={t} labels={labels} displayCurrency={displayCurrency} rates={currencyRates} onToggleCompare={toggleCompare} inCompare={!!selectedProduct && compare.some(item => item.id === selectedProduct.id)} />}{compare.length > 0 && <div ref={compareRef}><ComparePanel products={compare} t={t} labels={labels} displayCurrency={displayCurrency} rates={currencyRates} onRemove={id => setCompare(current => current.filter(item => item.id !== id))} onClear={() => setCompare([])} /></div>}</div>
}
