/** Данные для тестов повторяют контракт backend (см. ProductRead в app/schemas.py). */
export const PRODUCT = {
  id: 1,
  brand: 'Nike',
  name: 'Air Max Pulse',
  description: 'Nike Air Max Pulse в цвете White. Демонстрационная модель каталога.',
  price: 119,
  base_price: 119,
  currency: 'USD',
  color: 'White',
  gender: 'Men',
  use_case: 'Walking',
  features: ['school', 'autumn'],
  image_key: 'white',
  image: 'white',
  category: 'Sneakers',
  active: true,
  total_stock: 12,
  available_sizes: [42, 43],
  match_reasons: ['бренд Nike', 'размер 43 в наличии'],
  variants: [
    { id: 1, sku: 'nike-air-max-pulse-41', size: 41, stock: 0, attributes: { size: '41' } },
    { id: 2, sku: 'nike-air-max-pulse-42', size: 42, stock: 5, attributes: { size: '42' } },
    { id: 3, sku: 'nike-air-max-pulse-43', size: 43, stock: 7, attributes: { size: '43' } },
  ],
  attributes: { color: 'White', gender: 'Men', purpose: 'Walking', tags: ['school', 'autumn'] },
}

export const SECOND = {
  ...PRODUCT,
  id: 2,
  brand: 'Adidas',
  name: 'Ultraboost Light',
  price: 149,
  base_price: 149,
  currency: 'EUR',
  base_currency: 'EUR',
  color: 'Black',
  gender: null,
  use_case: null,
  features: [],
  image_key: 'black',
  available_sizes: [44],
  match_reasons: ['укладывается в бюджет до $150'],
  variants: [{ id: 9, sku: 'adidas-ub-44', size: 44, stock: 3, attributes: { size: '44' } }],
  attributes: { color: 'Black' },
}

import { EXTRA_COPY } from './.build/entry.mjs'

const BASE = {
  sizes: 'Размеры', inStock: stock => `${stock} пар в наличии`, loadingProduct: 'Загружаем карточку…', color: 'Цвет', purpose: 'Назначение',
  features: 'Подходят', chooseSize: 'Выберите размер', unavailableSizes: 'Недоступные размеры отмечены и не выбираются.',
  leaveRequest: 'Оставить заявку', chooseFirst: 'Сначала выберите размер', name: 'Имя', namePlaceholder: 'Как к вам обращаться',
  contact: 'Телефон или Telegram', contactPlaceholder: '@username', comment: 'Комментарий', optional: '(необязательно)',
  commentPlaceholder: 'Например, удобное время', sending: 'Отправляем…', send: 'Отправить заявку',
  privacy: 'Мы используем данные только для ответа на эту заявку.', requestError: 'Не удалось отправить заявку.',
  success: 'Заявка принята', successText: (name, size, price) => `Спасибо! Сохранили ${name}, размер ${size}, за $${price}.`,
  nothing: 'Ничего не найдено', nothingText: 'Попробуйте изменить условия.', resetFilters: 'Сбросить фильтры', retry: 'Повторить',
}

export const T = { ...BASE, ...EXTRA_COPY.ru }
export const LABELS = {
  colors: { White: 'Белый', Black: 'Чёрный' },
  genders: { Men: 'Мужские', Women: 'Женские' },
  useCases: { Walking: 'Для прогулок', Running: 'Для бега' },
  features: { school: 'Для школы', autumn: 'На осень' },
}
