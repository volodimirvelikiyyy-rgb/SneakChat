/**
 * Действия, которые backend проверенно понимает как команду диалога. Остальные действия
 * приходят из suggestions самого backend: он формирует их по текущему состоянию, поэтому
 * каждая кнопка меняет выдачу, а не повторяет прежний ответ.
 */
export const QUICK_ACTIONS = [
  { id: 'cheaper', phrase: 'покажи дешевле' },
  { id: 'similar', phrase: 'покажи похожие' },
  { id: 'more', phrase: 'покажи ещё' },
]

// Формулировки, которыми сам backend называет то же действие: дублировать их кнопкой не нужно.
const KNOWN_ACTION_PHRASES = {
  cheaper: ['Покажи подешевле', 'Дешевле'],
  similar: ['Похожие'],
  more: ['Покажи ещё', 'Показать ещё'],
}

function comparisons(id, suggestions) {
  const known = KNOWN_ACTION_PHRASES[id] || []
  return known.filter(phrase => suggestions.includes(phrase))
}

/**
 * Набор кнопок: сначала подсказки backend (он формирует их по текущему состоянию, поэтому
 * каждая такая кнопка меняет выдачу), затем проверенные команды диалога. Дубли убираются.
 */
export function buildQuickActions(t, suggestions = []) {
  const buttons = suggestions.map(suggestion => ({ key: `backend-${suggestion}`, label: suggestion, phrase: suggestion }))
  for (const action of QUICK_ACTIONS) {
    const alreadyOffered = comparisons(action.id, suggestions).some(known => buttons.some(button => button.phrase === known))
    if (alreadyOffered) continue
    buttons.push({ key: action.id, label: t.actions[action.id], phrase: action.phrase })
  }
  return buttons
}

export default function QuickActions({ t, onAction, suggestions = [] }) {
  const buttons = buildQuickActions(t, suggestions)
  return (
    <div className="quick-actions" aria-label={t.actionsLabel}>
      {buttons.map(button => (
        <button key={button.key} type="button" onClick={() => onAction(button.phrase)}>{button.label}</button>
      ))}
    </div>
  )
}
