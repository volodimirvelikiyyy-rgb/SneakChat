import { renderToStaticMarkup } from 'react-dom/server'
import ProductCard from '../src/components/ProductCard'
import ProductImage from '../src/components/ProductImage'
import ProductGallery from '../src/components/ProductGallery'
import { resolveProductPhotos } from '../src/components/productImages'
import SizeChips from '../src/components/SizeChips'
import ProductDetails from '../src/components/ProductDetails'
import ComparePanel from '../src/components/ComparePanel'
import QuickActions, { QUICK_ACTIONS, buildQuickActions } from '../src/components/QuickActions'
import { EmptyState, ErrorState, FallbackNotice, Searching } from '../src/components/StateBlocks'
import { EXTRA_COPY } from '../src/lib/uiCopy'
import { buildConditions } from '../src/lib/conditions'

const COMPONENTS = {
  ProductCard, ProductImage, ProductGallery, SizeChips, ProductDetails, ComparePanel, QuickActions,
  EmptyState, ErrorState, FallbackNotice, Searching,
}

/**
 * Рендер компонента в HTML самим бандлом: тесты не работают с React напрямую, поэтому
 * в процессе гарантированно один экземпляр React и хуки ведут себя как в браузере.
 */
export function renderComponent(name, props = {}) {
  const Component = COMPONENTS[name]
  if (!Component) throw new Error(`неизвестный компонент: ${name}`)
  return renderToStaticMarkup(<Component {...props} />)
}

export { COMPONENTS, QUICK_ACTIONS, buildQuickActions, EXTRA_COPY, buildConditions, resolveProductPhotos }
