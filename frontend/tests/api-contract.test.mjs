/**
 * E2E: интерфейс проверяется через настоящий backend, а не через mock.
 * Запускается реальный uvicorn и выполняются сценарии Этапа 6 на уровне контракта,
 * который использует UI: поля карточек, состояния, отсутствие технических текстов.
 */
import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const BACKEND = resolve(HERE, '../../backend')
const PORT = 8011
const BASE = `http://127.0.0.1:${PORT}`

let server

async function waitForHealth(attempts = 60) {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(`${BASE}/api/health`)
      if (response.ok) return true
    } catch { /* сервер ещё поднимается */ }
    await new Promise(resolveTimeout => setTimeout(resolveTimeout, 500))
  }
  throw new Error('backend не ответил на /api/health')
}

function startServer(env = {}) {
  // uvicorn в окружении не установлен, поэтому приложение поднимается минимальным
  // ASGI-сервером на стандартной библиотеке: HTTP и backend при этом настоящие.
  return spawn(process.env.SNEAKCHAT_TEST_PYTHON || 'python3', [resolve(HERE, 'asgi_server.py'), String(PORT)], {
    cwd: BACKEND,
    env: { ...process.env, ...env },
    stdio: 'ignore',
  })
}

const post = (session, message) => fetch(`${BASE}/api/chat`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id: session, message, language: 'ru', display_currency: 'USD' }),
}).then(async response => ({ status: response.status, body: await response.json() }))

before(async () => {
  server = startServer()
  await waitForHealth()
})

after(() => {
  server?.kill('SIGTERM')
})

test('A. карточки приходят с полями, которые рисует интерфейс', async () => {
  const { status, body } = await post('e2e-a', 'Nike 43 до 120')
  assert.equal(status, 200)
  assert.equal(body.filters.size, 43)
  assert.ok(body.products.length > 0, 'нет товаров')
  for (const product of body.products) {
    for (const field of ['id', 'brand', 'name', 'price', 'description', 'image_key', 'available_sizes', 'variants', 'match_reasons', 'total_stock']) {
      assert.ok(field in product, `в карточке нет поля ${field}`)
    }
    assert.ok(product.variants.every(variant => 'size' in variant && 'stock' in variant), 'варианты без размера или остатка')
  }
})

test('B. «дешевле» проходит через диалог и снижает бюджет', async () => {
  await post('e2e-b', 'Nike 43 до 150')
  const { status, body } = await post('e2e-b', 'покажи дешевле')
  assert.equal(status, 200)
  assert.ok(body.filters.max_price <= 150, `бюджет не изменился: ${body.filters.max_price}`)
})

test('C. «другой цвет» обрабатывается существующей логикой', async () => {
  await post('e2e-c', 'Nike 43')
  const { status, body } = await post('e2e-c', 'другой цвет')
  assert.equal(status, 200)
  assert.ok(typeof body.reply === 'string' && body.reply.length > 0)
})

test('D. «похожие» не мутируют условия пользователя', async () => {
  for (const message of ['Nike', '43', 'не белые', 'до 150']) await post('e2e-d', message)
  const before = (await post('e2e-d', 'покажи мои условия')).body.filters
  await post('e2e-d', 'покажи похожие')
  const after = (await post('e2e-d', 'и снова')).body.filters
  assert.deepEqual(after, before, 'условия изменились после «похожих»')
})

test('E. подробности товара доступны из API', async () => {
  const list = await fetch(`${BASE}/api/products`).then(response => response.json())
  const id = list[0].id
  const detail = await fetch(`${BASE}/api/products/${id}`).then(response => response.json())
  assert.ok(detail.description)
  assert.ok(detail.variants.length > 0)
  assert.ok('attributes' in detail)
})

test('F. сравнение опирается на поля backend', async () => {
  const list = await fetch(`${BASE}/api/products`).then(response => response.json())
  const [first, second] = list
  for (const product of [first, second]) {
    for (const field of ['price', 'brand', 'available_sizes']) assert.ok(field in product, `нет поля ${field}`)
  }
})

test('G. недоступный размер не выдаётся за доступный', async () => {
  const { status, body } = await post('e2e-g', 'Nike размер 46')
  assert.equal(status, 200)
  const claimed = body.products.flatMap(product => product.available_sizes)
  assert.equal(claimed.includes(46), false, 'интерфейс получил размер 46 как доступный')
})

test('H. отсутствие точного совпадения честно названо', async () => {
  const { status, body } = await post('e2e-h', 'Nike 43 голубой до 80')
  assert.equal(status, 200)
  if (body.products.length > 0) {
    const relaxed = /друг|ближайш|бюджет|без ограничен|не учитываю|тех же условиях/i.test(body.reply)
    const exact = body.products.every(product => product.brand === 'Nike' && product.available_sizes.includes(43) && product.color === 'Blue' && product.price <= 80)
    assert.ok(relaxed || exact, `ослабление не названо: ${body.reply}`)
  }
})

test('I. недоступный провайдер не ломает интерфейс и не показывает технических слов', async () => {
  server?.kill('SIGTERM')
  server = startServer({ AI_PROVIDER: 'ollama', OLLAMA_MODEL: 'none', OLLAMA_BASE_URL: 'http://127.0.0.1:9', OLLAMA_TIMEOUT: '2' })
  await waitForHealth()
  const { status, body } = await post('e2e-i', 'Nike 43')
  assert.equal(status, 200)
  assert.equal(body.mode, 'fallback')
  assert.ok(body.products.length > 0, 'fallback обязан отвечать по каталогу')
  for (const word of ['ollama', 'provider', 'json', 'validation', 'traceback', 'exception']) {
    assert.equal(body.reply.toLowerCase().includes(word), false, `техническое слово ${word} в ответе`)
  }
})

test('K. «подешевле» меняет ответ, а не повторяет его', async () => {
  const session = 'e2e-k'
  const first = await post(session, 'Nike 43 до 130')
  const second = await post(session, 'покажи дешевле')
  assert.equal(second.status, 200)
  assert.notEqual(second.body.reply, first.body.reply, 'кнопка «дешевле» повторила прежний ответ')
  // Условия пользователя не потеряны, а бюджет пересчитан в меньшую сторону.
  assert.equal(second.body.filters.brand, 'Nike')
  assert.equal(second.body.filters.size, 43)
  assert.ok(second.body.filters.max_price < first.body.filters.max_price)
})

test('L. честное «нет» называет рычаг, а подсказки действительно его меняют', async () => {
  const session = 'e2e-l'
  const miss = await post(session, 'Nike размер 46 до 80')
  assert.equal(miss.status, 200)
  assert.equal(miss.body.products.length, 0)
  assert.match(miss.body.reply, /ослабить/)
  for (const axis of ['бренд', 'бюджет', 'размер']) assert.match(miss.body.reply, new RegExp(axis))
  assert.ok(miss.body.suggestions.length > 0, 'нет ни одной кнопки ослабления')
  for (const suggestion of miss.body.suggestions) {
    const changed = await post(`${session}-${suggestion}`, suggestion)
    assert.notEqual(
      JSON.stringify(changed.body.filters),
      JSON.stringify(miss.body.filters),
      `подсказка «${suggestion}» не изменила условия`,
    )
  }
})

test('M. у каждой модели каталога есть файл изображения', async () => {
  const fs = await import('node:fs')
  const directory = resolve(HERE, '../public/images/sneakers')
  const list = await fetch(`${BASE}/api/products`).then(response => response.json())
  assert.ok(list.length > 0, 'каталог пуст')
  const missing = []
  for (const product of list) {
    const key = product.image_key || product.image
    if (!key || !fs.existsSync(resolve(directory, `${key}.jpg`))) missing.push(`${product.name}: ${key}`)
  }
  assert.deepEqual(missing, [], `нет фото для моделей: ${missing.join(', ')}`)
})

test('J. мобильные требования закреплены в стилях', async () => {
  const css = await fetch(`${BASE}/api/health`).then(() => import('node:fs')).then(fs => fs.readFileSync(resolve(HERE, '../src/styles.css'), 'utf8'))
  for (const width of ['360px', '390px', '430px']) {
    assert.ok(css.includes(width), `нет ширины ${width}`)
  }
  assert.match(css, /min-height: 44px/)
})
