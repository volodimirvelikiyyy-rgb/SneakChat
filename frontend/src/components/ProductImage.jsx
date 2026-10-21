import { getLegacyImagePath, getPhotoFrameStyle, resolveProductPhotos } from './productImages'

const FALLBACK = '/images/sneakers/fallback.svg'

/**
 * Карточка использует лёгкий кадрированный файл, а галерея открывает исходник.
 * Для старых записей каталога сохраняется резервный путь по ключу модели.
 */
export default function ProductImage({ product, size = 'md' }) {
  const photo = resolveProductPhotos(product)[0]
  const legacyPath = getLegacyImagePath(product)
  return (
    <img
      className={'product-image product-image-' + size}
      style={getPhotoFrameStyle(photo)}
      src={photo.preview}
      alt={photo.alt}
      loading="lazy"
      decoding="async"
      onError={event => {
        const image = event.currentTarget
        if (image.dataset.fallback === 'generic') return
        if (image.dataset.fallback === 'legacy') {
          image.dataset.fallback = 'generic'
          image.src = FALLBACK
          return
        }
        if (image.dataset.fallback === 'original') {
          image.dataset.fallback = 'legacy'
          image.src = legacyPath
          return
        }
        if (image.dataset.fallback === 'framed') {
          image.dataset.fallback = 'original'
          image.src = photo.original || legacyPath
          return
        }
        image.dataset.fallback = 'framed'
        image.src = photo.src || photo.original || legacyPath
      }}
    />
  )
}
