export const defaultCleaningOptions = {
  skipCode: true,
  skipUrls: true,
  stripMarkdown: true,
}

export function cleanText(source = '', options = defaultCleaningOptions) {
  let text = source

  if (options.skipCode) text = text.replace(/```[\s\S]*?```|`[^`]*`/g, ' ')
  if (options.skipUrls) text = text.replace(/(?:https?:\/\/|www\.)\S+/gi, ' ')
  if (options.stripMarkdown) {
    text = text
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')
      .replace(/^\s*[-+*]\s+/gm, '')
      .replace(/^\s*\d+[.)]\s+/gm, '')
      .replace(/[>*_~]/g, '')
  }

  return text.replace(/[•◆◇▪▫※]/g, ' ').replace(/\s+/g, ' ').trim()
}
