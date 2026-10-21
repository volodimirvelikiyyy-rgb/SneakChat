const GALLERY_ANGLES = {
  'adidas-campus-00s': ['adidas-campus-00s.jpg'],
  'adidas-gazelle-indoor': ['adidas-gazelle-indoor.jpg'],
  'adidas-samba-og': ['adidas-samba-og.jpg'],
  'adidas-supernova-rise': ['adidas-supernova-rise.jpg'],
  'adidas-terrex-agravic-3': ['adidas-terrex-agravic-3.jpg'],
  'asics-gel-nyc': ['asics-gel-nyc.jpg'],
  'asics-gel-trabuco-13': ['asics-gel-trabuco-13.jpg'],
  'new-balance-2002r': ['new-balance-2002r.jpg'],
  'new-balance-574': ['new-balance-574.jpg'],
  'new-balance-9060': ['new-balance-9060.jpg'],
  'nike-air-max-pulse': ['nike-air-max-pulse.jpg'],
  'nike-dunk-low-retro': ['nike-dunk-low-retro.jpg'],
  'nike-pegasus-trail-5': ['nike-pegasus-trail-5.jpg'],
  'nike-v2k-run': ['nike-v2k-run.jpg'],
}

const BASE = '/images/sneakers/'
const LEGACY_ONLY = new Set(['new-balance-550', 'asics-gel-sonoma-8-gtx'])

function slug(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
}

export function resolveProductPhotos(product) {
  const managedPhotos = (product?.photos || []).filter(photo => photo?.url)
  if (managedPhotos.length) {
    return managedPhotos.map((photo, index) => ({
      src: photo.url,
      original: photo.url,
      preview: photo.url,
      crop_scale: photo.crop_scale,
      crop_x: photo.crop_x,
      crop_y: photo.crop_y,
      alt: (product?.brand || '') + ' ' + (product?.name || '') + (index ? ' — фото ' + (index + 1) : ''),
    }))
  }
  const key = product?.image_key || product?.image || 'fallback'
  const color = slug(product?.color)
  const lowerKey = String(key).toLowerCase()
  const primaryKey = LEGACY_ONLY.has(key)
    ? key
    : color && lowerKey !== color && !lowerKey.endsWith('-' + color) ? key + '-' + color : key
  const candidates = [primaryKey + '.jpg', ...(GALLERY_ANGLES[key] || [])]
  const unique = [...new Set(candidates)]
  return unique.map((filename, index) => ({
    src: BASE + 'framed/' + filename,
    original: BASE + filename,
    preview: BASE + 'optimized/' + filename,
    alt: (product?.brand || '') + ' ' + (product?.name || '') + (index ? ' — фото ' + (index + 1) : ''),
  }))
}

export function getPhotoFrameStyle(photo) {
  const scale = Number(photo?.crop_scale) || 1
  const x = Number(photo?.crop_x) || 0
  const y = Number(photo?.crop_y) || 0
  return scale === 1 && x === 0 && y === 0 ? undefined : { transform: `translate(${x}%, ${y}%) scale(${scale})` }
}

export function getLegacyImagePath(product) {
  const key = product?.image_key || product?.image || 'fallback'
  return BASE + key + '.jpg'
}
