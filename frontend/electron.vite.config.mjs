import { defineConfig } from 'electron-vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  main: {
    build: {
      rollupOptions: {
        input: 'src/main/index.js'
      }
    }
  },
  preload: {
    build: {
      rollupOptions: {
        input: 'src/preload/index.js',
        output: {
          format: 'cjs',
          entryFileNames: '[name].cjs'
        }
      }
    }
  },
  renderer: {
    root: 'src/renderer',
    plugins: [vue()]
  }
})
