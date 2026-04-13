import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
        display: ['"Instrument Serif"', 'Georgia', 'serif'],
      },
      colors: {
        rent: {
          50: '#E1F5EE', 100: '#9FE1CB', 200: '#5DCAA5',
          400: '#1D9E75', 600: '#0F6E56', 800: '#085041', 900: '#04342C',
        },
        tier: { api: '#1D9E75', jsonld: '#378ADD', dom: '#534AB7', llm: '#EF9F27', vision: '#D85A30', fail: '#E24B4A' },
        status: { available: '#1D9E75', leased: '#ADB5BD', unknown: '#EF9F27' },
        change: { up: '#E24B4A', down: '#1D9E75', new: '#378ADD', gone: '#868E96' },
      },
    },
  },
  plugins: [],
} satisfies Config;
