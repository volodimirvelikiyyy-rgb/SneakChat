import { useEffect, useMemo, useRef, useState } from 'react'
import { getLegacyImagePath, getPhotoFrameStyle, resolveProductPhotos } from './productImages'
import { EXTRA_COPY } from '../lib/uiCopy'

export default function ProductGallery({ product, t = EXTRA_COPY.ru }) {
  const photos = useMemo(() => resolveProductPhotos(product), [product])
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [lightboxOpen, setLightboxOpen] = useState(false)
  const closeRef = useRef(null)
  const openerRef = useRef(null)
  const touchStartX = useRef(null)
  const activePhoto = photos[selectedIndex] || photos[0]
  const hasMultiple = photos.length > 1

  useEffect(() => {
    setSelectedIndex(0)
    setLightboxOpen(false)
  }, [product?.id])

  useEffect(() => {
    if (!lightboxOpen) return undefined
    const previousOverflow = document.body.style.overflow
    const onKeyDown = event => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        setLightboxOpen(false)
      } else if (event.key === 'ArrowRight' && hasMultiple) {
        event.preventDefault()
        setSelectedIndex(index => (index + 1) % photos.length)
      } else if (event.key === 'ArrowLeft' && hasMultiple) {
        event.preventDefault()
        setSelectedIndex(index => (index - 1 + photos.length) % photos.length)
      }
    }
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', onKeyDown)
    closeRef.current?.focus()
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
      openerRef.current?.focus()
    }
  }, [lightboxOpen, hasMultiple, photos.length])

  if (!activePhoto) return null

  const openLightbox = event => {
    openerRef.current = event.currentTarget
    setLightboxOpen(true)
  }
  const stepPhoto = delta => setSelectedIndex(index => (index + delta + photos.length) % photos.length)
  const previewFallback = event => {
    const image = event.currentTarget
    if (image.dataset.fallback === 'raw') {
      image.dataset.fallback = 'generic'
      image.src = '/images/sneakers/fallback.svg'
      return
    }
    if (image.dataset.fallback === 'framed') {
      image.dataset.fallback = 'raw'
      image.src = image.dataset.raw || getLegacyImagePath(product)
      return
    }
    image.dataset.fallback = 'framed'
    image.src = image.dataset.original || getLegacyImagePath(product)
  }
  const originalFallback = event => {
    const image = event.currentTarget
    if (image.dataset.fallback === 'preview') {
      image.src = '/images/sneakers/fallback.svg'
      return
    }
    if (image.dataset.fallback === 'original') {
      image.dataset.fallback = 'preview'
      image.src = activePhoto.preview
      return
    }
    image.dataset.fallback = 'original'
    image.src = activePhoto.original
  }

  return (
    <div className="product-gallery">
      <button type="button" className="gallery-main" onClick={openLightbox} aria-label={t.galleryZoomPhoto(`${product.brand} ${product.name}`)}>
        <img src={activePhoto.preview} style={getPhotoFrameStyle(activePhoto)} data-original={activePhoto.src} data-raw={activePhoto.original} alt={activePhoto.alt} loading="eager" decoding="async" onError={previewFallback} />
        <span className="gallery-zoom-hint" aria-hidden="true">{t.galleryZoom}</span>
      </button>
      {hasMultiple && (
        <div className="gallery-controls">
          <button type="button" className="gallery-arrow" onClick={() => stepPhoto(-1)} aria-label={t.galleryPrevious}>‹</button>
          <span className="gallery-count" aria-live="polite">{selectedIndex + 1} / {photos.length}</span>
          <button type="button" className="gallery-arrow" onClick={() => stepPhoto(1)} aria-label={t.galleryNext}>›</button>
        </div>
      )}
      <div className="gallery-thumbnails" aria-label={t.galleryPhotos}>
        {photos.map((photo, index) => (
          <button
            type="button"
            key={photo.src}
            className={'gallery-thumbnail' + (index === selectedIndex ? ' selected' : '')}
            onClick={() => setSelectedIndex(index)}
            aria-label={t.galleryShowPhoto(index + 1, photos.length)}
            aria-pressed={index === selectedIndex}
          >
            <img src={photo.preview} style={getPhotoFrameStyle(photo)} data-original={photo.src} data-raw={photo.original} alt="" loading="lazy" decoding="async" onError={previewFallback} />
          </button>
        ))}
      </div>
      {lightboxOpen && (
        <div className="gallery-lightbox" role="dialog" aria-modal="true" aria-label={t.galleryLightbox(`${product.brand} ${product.name}`)} onClick={() => setLightboxOpen(false)}>
          <button ref={closeRef} type="button" className="lightbox-close" onClick={() => setLightboxOpen(false)} aria-label={t.galleryClose}>×</button>
          {hasMultiple && <button type="button" className="lightbox-arrow previous" onClick={event => { event.stopPropagation(); stepPhoto(-1) }} aria-label={t.galleryPrevious}>‹</button>}
          <div
            className="lightbox-media"
            onClick={event => event.stopPropagation()}
            onTouchStart={event => { touchStartX.current = event.touches[0].clientX }}
            onTouchEnd={event => {
              if (touchStartX.current == null || !hasMultiple) return
              const delta = event.changedTouches[0].clientX - touchStartX.current
              if (Math.abs(delta) > 45) stepPhoto(delta < 0 ? 1 : -1)
              touchStartX.current = null
            }}
          >
            <img key={activePhoto.src} src={activePhoto.src} style={getPhotoFrameStyle(activePhoto)} alt={activePhoto.alt} decoding="async" onError={originalFallback} />
            {hasMultiple && <span className="lightbox-count">{selectedIndex + 1} / {photos.length}</span>}
          </div>
          {hasMultiple && <button type="button" className="lightbox-arrow next" onClick={event => { event.stopPropagation(); stepPhoto(1) }} aria-label={t.galleryNext}>›</button>}
          {hasMultiple && (
            <div className="lightbox-thumbnails" onClick={event => event.stopPropagation()}>
              {photos.map((photo, index) => (
                <button
                  type="button"
                  key={photo.src}
                  className={'gallery-thumbnail' + (index === selectedIndex ? ' selected' : '')}
                  onClick={() => setSelectedIndex(index)}
                  aria-label={t.galleryShowPhoto(index + 1, photos.length)}
                  aria-pressed={index === selectedIndex}
                >
                  <img src={photo.preview} style={getPhotoFrameStyle(photo)} data-original={photo.src} data-raw={photo.original} alt="" loading="lazy" decoding="async" onError={previewFallback} />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
