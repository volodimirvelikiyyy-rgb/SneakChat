import { useEffect, useRef, useState } from 'react'
import { BrowserTTS } from '../lib/tts'
import { cleanText, defaultCleaningOptions } from '../lib/textCleaner'

const SETTINGS_KEY = 'listenflow:settings'

function getSavedPreferences() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {} } catch { return {} }
}

export default function ReadAloudButton({ text, className = '', label = '🔊', title = 'Озвучить сообщение' }) {
  const provider = useRef(null)
  const [reading, setReading] = useState(false)

  useEffect(() => {
    provider.current = new BrowserTTS()
    return () => provider.current?.stop()
  }, [])

  function read() {
    const tts = provider.current
    if (!tts?.supported) return
    const preferences = getSavedPreferences()
    const content = cleanText(text, preferences.cleaning || defaultCleaningOptions)
    if (!content) return
    tts.speak({
      text: content,
      rate: preferences.rate || 1.15,
      voiceURI: preferences.voiceURI || '',
      onStart: () => setReading(true),
      onEnd: () => setReading(false),
      onError: () => setReading(false),
    })
  }

  return (
    <button className={`read-aloud-button ${className}`} type="button" onClick={read} disabled={!text || reading} aria-label={title} title={title}>
      {reading ? '…' : label}
    </button>
  )
}
