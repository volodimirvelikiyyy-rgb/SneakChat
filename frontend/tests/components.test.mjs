import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { renderComponent, QUICK_ACTIONS, buildQuickActions, buildConditions, resolveProductPhotos, EXTRA_COPY } from './.build/entry.mjs'
import { PRODUCT, SECOND, T, LABELS } from './fixtures.mjs'

test('чат начинается с нейтрального приветствия без автоматических сценариев', () => {
  const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
  assert.ok(app.includes('Здравствуйте! Чем могу вам помочь?'))
  assert.ok(app.includes('Вітаю! Чим можу вам допомогти?'))
  assert.ok(!app.includes('QuickActions'))
  assert.ok(!app.includes('onQuickPrompt'))
  assert.ok(!app.includes('prompts:'))
  assert.ok(app.includes("queryPlaceholder: 'Ваше сообщение'"))
  assert.ok(app.includes("queryPlaceholder: 'Ваше повідомлення'"))
  assert.ok(app.includes('selectedSize={item.filters?.size}'))
})

test('ProductCard показывает фото, название, бренд, цену и причину', () => {
  const markup = renderComponent('ProductCard', { product: PRODUCT, t: T, selectedSize: 43, onOpen() {}, variant: 'grid' })
  assert.ok(markup.includes('src="/images/sneakers/optimized/white.jpg"'), 'карточка не подключила оптимизированное фото товара')
  // Если фото по ключу нет, показывается общая картинка — не пустое место. Обработчик в
  // статичную разметку не попадает, поэтому наличие резерва проверяем по исходнику компонента.
  const component = readFileSync(new URL('../src/components/ProductImage.jsx', import.meta.url), 'utf8')
  assert.ok(component.includes("const FALLBACK = '/images/sneakers/fallback.svg'"), 'не задана резервная картинка')
  assert.ok(component.includes('onError'), 'нет перехода на резервную картинку при отсутствии фото')
  assert.ok(component.includes('loading="lazy"'), 'каталог должен загружать изображения лениво')
  assert.match(markup, /alt="Nike Air Max Pulse"/)
  assert.match(markup, /Air Max Pulse/)
  assert.match(markup, /Nike/)
  assert.match(markup, /\$119/)
  assert.match(markup, /бренд Nike/)
  assert.match(markup, new RegExp(T.reasonLabel))
  assert.match(markup, new RegExp(T.details))
})

test('цены карточек сохраняют символ выбранной валюты без конвертации', () => {
  for (const [currency, expected] of [['USD', '$123,45'], ['EUR', '€123,45'], ['UAH', '₴123']]) {
    const markup = renderComponent('ProductCard', {
      product: { ...PRODUCT, price: 123.45, base_price: 123.45, currency, base_currency: currency }, t: T, onOpen() {}, variant: 'grid',
    })
    assert.ok(markup.includes(expected), `неверно отображается ${currency}`)
  }
})

test('карточка показывает цену только выбранного варианта и использует цену товара при её отсутствии', () => {
  const product = {
    ...PRODUCT,
    price: 119,
    base_price: 119,
    base_currency: 'USD',
    variants: [
      { ...PRODUCT.variants[1], price: 97.25 },
      { ...PRODUCT.variants[2], price: 113.5 },
      { id: 4, size: 44, stock: 2 },
    ],
  }
  const renderPrice = selectedSize => renderComponent('ProductCard', {
    product, selectedSize, t: T, onOpen() {}, variant: 'grid', displayCurrency: 'USD',
  }).match(/<p class="product-price">([^<]+)<\/p>/)?.[1]

  assert.equal(renderPrice(42), '$97,25')
  assert.equal(renderPrice(43), '$113,5')
  assert.equal(renderPrice(44), '$119', 'для варианта без цены используется цена товара')
  assert.equal(renderPrice(45), '$119', 'цена другого размера не переносится на выбранный')
})

test('ProductDetails передаёт выбранный размер в общий форматтер цены при смене размера', () => {
  const component = readFileSync(new URL('../src/components/ProductDetails.jsx', import.meta.url), 'utf8')
  assert.match(component, /formatProductCurrency\(product, displayCurrency, rates, selectedSize\)/)
  assert.match(component, /onClick=\{\(\) => setSelectedSize\(variant\.size\)\}/)
  assert.match(component, /formatProductCurrency\(product, displayCurrency, rates, selectedSize\)\)\}/)
})

test('карточка конвертирует из исходной цены только по переданным курсам и округляет гривны только для показа', () => {
  const markup = renderComponent('ProductCard', {
    product: { ...PRODUCT, price: 100, base_price: 100, currency: 'USD', base_currency: 'USD' },
    displayCurrency: 'UAH', rates: { EUR: 1, USD: 1.1, UAH: 44 }, t: T, onOpen() {}, variant: 'grid',
  })
  assert.match(markup, /₴4(?:\u00a0|\u202f| )000/)
  const fallback = renderComponent('ProductCard', {
    product: { ...PRODUCT, price: 100, base_price: 100, currency: 'USD', base_currency: 'USD' },
    displayCurrency: 'UAH', t: T, onOpen() {}, variant: 'grid',
  })
  assert.ok(fallback.includes('$100'), 'без курса отображается исходная цена, а не придуманная сумма')
})

test('галерея показывает ракурсы подтверждённой модели и работает с одним фото', () => {
  const single = renderComponent('ProductGallery', { product: PRODUCT })
  assert.match(single, /gallery-main/)
  assert.match(single, /Увеличить фото Nike Air Max Pulse/)
  assert.equal((single.match(/class="gallery-thumbnail(?: selected)?"/g) || []).length, 1)

  const pair = { ...PRODUCT, image_key: 'adidas-samba-og', brand: 'Adidas', name: 'Samba OG', color: 'White' }
  const photos = resolveProductPhotos(pair)
  assert.deepEqual(photos.map(photo => photo.src), [
    '/images/sneakers/framed/adidas-samba-og-white.jpg',
    '/images/sneakers/framed/adidas-samba-og.jpg',
  ])
  const markup = renderComponent('ProductGallery', { product: pair })
  assert.equal((markup.match(/class="gallery-thumbnail(?: selected)?"/g) || []).length, 2)
  assert.match(markup, /Следующее фото/)
  assert.match(markup, /Предыдущее фото/)

  const ukrainian = renderComponent('ProductGallery', { product: pair, t: EXTRA_COPY.uk })
  assert.match(ukrainian, /Наступне фото/)
  assert.doesNotMatch(ukrainian, /Следующее фото/)

  for (const imageKey of ['new-balance-550', 'asics-gel-sonoma-8-gtx']) {
    const fallbackPhotos = resolveProductPhotos({ ...PRODUCT, image_key: imageKey, color: 'Black' })
    assert.equal(fallbackPhotos.length, 1)
    assert.equal(fallbackPhotos[0].original, '/images/sneakers/' + imageKey + '.jpg')
  }
})

test('ProductCard не показывает служебные поля backend', () => {
  const markup = renderComponent('ProductCard', { product: PRODUCT, t: T, onOpen() {}, variant: 'grid' })
  for (const field of ['image_key', 'base_price', 'available_sizes', 'match_reasons', 'sku', 'total_stock', 'attributes']) {
    assert.equal(markup.includes(field), false, `служебное поле ${field} попало в карточку`)
  }
})

test('размеры: показаны только реальные варианты, отсутствующие помечены', () => {
  const markup = renderComponent('SizeChips', { product: PRODUCT, selectedSize: 43, t: T })
  assert.match(markup, /class="size-chip available selected"/)
  assert.match(markup, /class="size-chip sold-out"/)
  assert.match(markup, /aria-disabled="true"/)
  assert.match(markup, />43</)
})

test('карточка честно сообщает, что выбранного размера нет', () => {
  const markup = renderComponent('SizeChips', { product: PRODUCT, selectedSize: 46, t: T })
  assert.match(markup, /size-warn/)
  assert.match(markup, /Размера 46 в этой модели нет/)
})

test('компактная карточка ограничивает число размеров и не теряет данные', () => {
  const many = { ...PRODUCT, variants: [38, 39, 40, 41, 42, 43, 44].map((size, index) => ({ id: index + 1, size, stock: 1 })) }
  const markup = renderComponent('SizeChips', { product: many, t: T, limit: 5 })
  assert.match(markup, /\+2/)
  assert.match(markup, /class="size-chip available"/)
})

test('быстрые действия: подсказки backend и только проверенные команды', () => {
  const withSuggestions = buildQuickActions(T, ['Покажи подешевле', 'Покажи все'])
  const phrases = withSuggestions.map(button => button.phrase)
  // Подсказки backend идут первыми и остаются как есть.
  assert.equal(phrases[0], 'Покажи подешевле')
  assert.ok(phrases.includes('Покажи все'))
  // Команды, которые backend понимает как действие.
  assert.ok(phrases.includes('покажи похожие'))
  assert.ok(phrases.includes('покажи ещё'))
  // Дубль не добавляется: «Покажи подешевле» уже предложено backend.
  assert.equal(phrases.includes('покажи дешевле'), false)
  // Никаких выдуманных действий, которых backend не умеет менять.
  for (const phrase of phrases) {
    const allowed = ['покажи дешевле', 'покажи похожие', 'покажи ещё']
    assert.ok(allowed.includes(phrase) || phrase.startsWith('Покажи'), `неожиданное действие: ${phrase}`)
  }
  const markup = renderComponent('QuickActions', { t: T, onAction() {}, suggestions: ['Покажи все'] })
  assert.match(markup, /Покажи все/)
  assert.match(markup, new RegExp(T.actions.similar))
})

test('подробности товара показывают реальные данные backend и кнопку возврата', () => {
  const markup = renderComponent('ProductDetails', { product: PRODUCT, loading: false, error: '', onClose() {}, t: T, labels: LABELS })
  assert.match(markup, /Nike Air Max Pulse/)
  assert.match(markup, /Демонстрационная модель каталога/)
  assert.match(markup, /Белый/)
  assert.match(markup, /Мужские/)
  assert.match(markup, /Для прогулок/)
  assert.match(markup, /Для школы/)
  assert.match(markup, new RegExp(T.backToChat))
  assert.match(markup, /Почему подходит/)
  assert.match(markup, /бренд Nike/)
})

test('украинская карточка локализует шаблонное описание без изменения данных товара', () => {
  const ukrainianLabels = {
    ...LABELS,
    colors: { White: 'Білий' },
    genders: { Men: 'Чоловічі' },
    useCases: { Walking: 'Для прогулянок' },
    features: { school: 'Для школи', autumn: 'На осінь' },
  }
  const markup = renderComponent('ProductDetails', {
    product: PRODUCT, loading: false, error: '', onClose() {},
    t: { ...T, ...EXTRA_COPY.uk }, labels: ukrainianLabels,
  })
  assert.match(markup, /Демонстраційна модель каталогу/)
  assert.match(markup, /у кольорі Білий/)
  assert.doesNotMatch(markup, /Демонстрационная модель каталога/)
  assert.equal(PRODUCT.description, 'Nike Air Max Pulse в цвете White. Демонстрационная модель каталога.')
})

test('подробности не показывают внутренние поля базы', () => {
  const markup = renderComponent('ProductDetails', { product: PRODUCT, loading: false, error: '', onClose() {}, t: T, labels: LABELS })
  for (const field of ['sku', 'image_key', 'base_price', 'nike-air-max-pulse-42', 'attributes']) {
    assert.equal(markup.includes(field), false, `внутреннее поле ${field} попало в подробности`)
  }
})

test('сравнение двух моделей идёт по данным backend, отсутствующее — «нет данных»', () => {
  const markup = renderComponent('ComparePanel', { products: [PRODUCT, SECOND], t: T, labels: LABELS, onRemove() {}, onClear() {} })
  assert.match(markup, new RegExp(T.compareTitle))
  assert.match(markup, /\$119/)
  assert.match(markup, /€149/)
  assert.match(markup, /Adidas Ultraboost Light/)
  assert.match(markup, new RegExp(T.noData))
  assert.match(markup, new RegExp(T.compareRows.purpose))
  assert.match(markup, /compare-scroll/)
})

test('загрузка сообщает, что консультант ищет варианты', () => {
  const markup = renderComponent('Searching', { t: T })
  assert.match(markup, new RegExp(T.searching))
  assert.match(markup, /aria-live="polite"/)
})

test('ошибка показана понятно и без технических деталей', () => {
  const markup = renderComponent('ErrorState', { t: T, onRetry() {} })
  assert.match(markup, new RegExp(T.errorTitle))
  assert.match(markup, new RegExp(T.retry))
  for (const technical of ['provider', 'Ollama', 'JSON', 'validation', 'Traceback', '500', 'http']) {
    assert.equal(markup.toLowerCase().includes(technical.toLowerCase()), false, `технический текст ${technical}`)
  }
})

test('уточнение при отсутствии точного совпадения выделено отдельно', () => {
  const markup = renderComponent('FallbackNotice', { text: 'Точных совпадений нет. Ниже — варианты в других цветах.' })
  assert.match(markup, /state-block notice/)
  assert.match(markup, /других цветах/)
})

test('пустой результат предлагает сбросить фильтры', () => {
  const markup = renderComponent('EmptyState', { t: T, onReset() {} })
  assert.match(markup, new RegExp(T.nothing))
  assert.match(markup, new RegExp(T.resetFilters))
})

test('мобильная вёрстка: есть медиазапросы и перенос длинных названий', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  for (const width of ['430px', '360px', '600px']) {
    assert.ok(css.includes(`max-width: ${width}`) || css.includes(`min-width: ${width}`), `нет медиазапроса ${width}`)
  }
  assert.ok(css.includes('overflow-wrap: anywhere'), 'нет переноса длинных названий')
  assert.ok(css.includes('.compare-scroll { overflow-x: auto; }'), 'таблица сравнения не прокручивается')
  assert.ok(css.includes('min-height: 44px'), 'кнопки меньше 44px')
})

test('карточка сравнивает бюджет в одной валюте и скрывает сравнение при неизвестном курсе', () => {
  const over = renderComponent('ProductCard', { product: PRODUCT, t: T, onOpen() {}, budgetMax: 80, budgetCurrency: 'USD' })
  assert.match(over, /product-over-budget/)
  assert.match(over, /выше бюджета на \$39/)

  const selectedVariant = {
    ...PRODUCT,
    variants: PRODUCT.variants.map(variant => ({ ...variant, size: 43, price: 70 })),
  }
  const fitting = renderComponent('ProductCard', {
    product: selectedVariant, t: T, onOpen() {}, budgetMax: 80, budgetCurrency: 'USD', selectedSize: 43,
  })
  assert.equal(fitting.includes('product-over-budget'), false)

  const unknownRate = renderComponent('ProductCard', {
    product: PRODUCT, t: T, onOpen() {}, budgetMax: 80, budgetCurrency: 'EUR', displayCurrency: 'UAH', rates: null,
  })
  assert.equal(unknownRate.includes('product-over-budget'), false)

  const noBudget = renderComponent('ProductCard', { product: PRODUCT, t: T, onOpen() {} })
  assert.equal(noBudget.includes('product-over-budget'), false)
})

test('наличие по размеру видно в плашке, а не только при наведении', () => {
  const markup = renderComponent('SizeChips', { product: PRODUCT, selectedSize: 43, t: T })
  assert.match(markup, /<sup class="size-count" aria-hidden="true">7<\/sup>/)
  assert.match(markup, /aria-label="43: 7 пар в наличии"/)
  // У распроданного размера количества нет: показывать «0» бессмысленно.
  assert.match(markup, /aria-label="41: Нет в наличии"/)
})

test('условия подбора показаны словами по данным backend', () => {
  const conditions = buildConditions(
    { brand: 'Nike', color: 'White', size: 43, max_price: 120, use_case: 'Running', exclude_attributes: { color: ['Black'] } },
    T,
    LABELS,
  )
  assert.deepEqual(conditions, ['бренд Nike', 'цвет белый', 'размер 43', 'назначение для бега', 'до $120', 'кроме чёрный'])
  // Условий нет — подписывать нечего.
  assert.deepEqual(buildConditions({}, T, LABELS), [])
  assert.deepEqual(buildConditions(null, T, LABELS), [])
  // Незнакомое значение не прячется: лучше код, чем потерянное условие.
  assert.deepEqual(buildConditions({ brand: 'Saucony' }, T, LABELS), ['бренд Saucony'])
})

test('сравнение честно помечает отсутствующие данные у любой строки', () => {
  const sparse = { ...PRODUCT, available_sizes: [], features: [], color: null, gender: null, use_case: null }
  const markup = renderComponent('ComparePanel', { products: [sparse, SECOND], t: T, labels: LABELS, onRemove() {}, onClear() {} })
  assert.match(markup, /class="no-data"/)
  assert.match(markup, new RegExp(T.noData))
  assert.equal(markup.includes('undefined'), false, 'пустое значение не должно протекать как undefined')
})

test('admin route has a defined view component', () => {
  const source = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
  assert.match(source, /function AdminGate\(\)/)
  assert.match(source, /function AdminView\(\{ onBackToProducts \}\)/)
  assert.match(source, /window\.location\.pathname === '\/admin'\) return <AdminGate \/>/)
  assert.match(source, /<AdminProductsPanel \/>/)
})
