import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/** Сборка компонентов в один самодостаточный ESM-файл, чтобы тесты запускались обычным node --test. */
export default defineConfig({
  plugins: [react()],
  // React остаётся внешним (по умолчанию для ssr): иначе в бандл попадает вторая копия React
  // и хуки в тестах не работают.
  build: {
    ssr: 'tests/entry.jsx',
    outDir: 'tests/.build',
    emptyOutDir: true,
    minify: false,
    rollupOptions: { output: { format: 'es', entryFileNames: 'entry.mjs' } },
  },
})
