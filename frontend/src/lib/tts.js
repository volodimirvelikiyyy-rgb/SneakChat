export class BrowserTTS {
  constructor() {
    this.supported = typeof window !== 'undefined' && 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window
    this.synthesis = this.supported ? window.speechSynthesis : null
    this.runId = 0
  }

  getVoices() {
    return this.synthesis?.getVoices() ?? []
  }

  speak({ text, rate = 1, voiceURI = '', onStart, onEnd, onError }) {
    if (!this.supported) {
      onError?.(new Error('Web Speech API is unavailable'))
      return
    }

    this.stop()
    const runId = ++this.runId
    const utterance = new SpeechSynthesisUtterance(text)
    const voice = this.getVoices().find(item => item.voiceURI === voiceURI)
    utterance.lang = voice?.lang || 'ru-RU'
    utterance.rate = rate
    if (voice) utterance.voice = voice

    utterance.onstart = () => { if (runId === this.runId) onStart?.() }
    utterance.onend = () => { if (runId === this.runId) onEnd?.() }
    utterance.onerror = event => {
      if (runId === this.runId && event.error !== 'canceled') onError?.(event)
    }
    this.synthesis.speak(utterance)
  }

  stop() {
    this.runId += 1
    this.synthesis?.cancel()
  }
}
