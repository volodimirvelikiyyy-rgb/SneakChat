/** Состояние загрузки: видно, что консультант ищет варианты, а не пустой экран. */
export function Searching({ t }) {
  return (
    <div className="searching" role="status" aria-live="polite">
      <span className="dots" aria-hidden="true"><i /><i /><i /></span>
      {t.searching}
    </div>
  )
}

/** Ошибка: короткое понятное сообщение и возможность повторить. Без технических деталей. */
export function ErrorState({ t, onRetry }) {
  return (
    <div className="state-block error" role="alert">
      <b>{t.errorTitle}</b>
      <p>{t.errorText}</p>
      <button type="button" onClick={onRetry}>{t.retry}</button>
    </div>
  )
}

/**
 * Точного совпадения нет: показывается то, что backend уже сказал словами, отдельным блоком,
 * чтобы ослабление условий не выглядело как точное совпадение.
 */
export function FallbackNotice({ text }) {
  return (
    <div className="state-block notice" role="status">
      {text}
    </div>
  )
}

/** Пустой результат по условиям. */
export function EmptyState({ t, onReset }) {
  return (
    <div className="state-block">
      <b>{t.nothing}</b>
      <p>{t.nothingText}</p>
      <button type="button" onClick={onReset}>{t.resetFilters}</button>
    </div>
  )
}
