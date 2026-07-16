# Hapzea Brand & Design System

> **Purpose:** Drop this `brand-kit/` folder into any new Hapzea application project.
> When prompting Copilot to build UI, tell it: *"Follow brand-kit/BRAND-DESIGN-SYSTEM.md for all
> colors, fonts, logo, and component styling."* This keeps every app visually consistent.

---

## 1. Brand Identity

- **Product name:** Hapzea
- **Tagline:** *AI Photo Delivery for Photographers* / "AI-powered real-time photo delivery platform for photographers and event teams."
- **Logo mark:** A blue feather/quill (gradient sky-blue → royal-blue) next to the white "Hapzea" wordmark.
- **Logo file:** [`assets/logo/logo.png`](assets/logo/logo.png) — transparent PNG, white wordmark, designed for **dark backgrounds**.
- **Domain:** https://hapzea.com

### Logo usage rules
- Always render the logo on a **dark/black background** (the wordmark is white).
- Preferred height: `h-12` (48px) on mobile, `h-14` (56px) on desktop; keep `w-auto object-contain`.
- Hover interaction used in-app: `hover:scale-105 transition-transform duration-300 cursor-pointer`.
- `alt="Hapzea Logo"`. Import in React as: `import logo from '<path>/assets/logo/logo.png';`

---

## 2. Color Palette

The theme is a **dark, premium tech aesthetic**: black/deep-navy backgrounds with cyan→blue gradient accents.

### Core brand colors
| Token | Hex | Usage |
|-------|-----|-------|
| `custom-blue` | `#091e4c` | Deep navy — gradient overlays, backgrounds |
| Brand navy (dark) | `#000062` | Theme color, deep gradient stops, badges |
| Gradient navy alt | `#0f0e38` | Feature card gradient end |
| Deep space | `#0a0f1c` | Page background gradient middle stop |
| Primary black | `#000000` | Base page background |

### Accent gradient (primary CTA / highlights)
The signature accent is a **cyan-to-blue** gradient (matches the feather logo):
- Tailwind: `bg-gradient-to-r from-sky-400 to-blue-600` (hover: `from-sky-500 to-blue-700`)
- Text highlight: `bg-gradient-to-r from-cyan-400 via-blue-400 to-blue-500 bg-clip-text text-transparent`
- Approx hex: sky-400 `#38bdf8` → blue-600 `#2563eb`

### Ambient / decorative gradients
- Indigo glow: `rgba(99, 102, 241, 0.8)` (`#6366f1`)
- Purple glow: `rgba(168, 85, 247, 0.4)` (`#a855f7`)
- Blue glow: `rgba(59, 130, 246, 0.6)` (`#3b82f6`)
- Heading gradient: `from-white via-blue-100 to-purple-200`

### Semantic / status colors (react-hot-toast + UI)
| Purpose | Hex / Tailwind |
|---------|----------------|
| Success | `#10b981` (emerald-500) |
| Error | `#ef4444` (red-500) |
| Toast background | `#1f2937` (gray-800) |
| Toast text | `#fff` |
| Neutral text (body) | `text-gray-300` |
| Muted text | `#888` |

### Google sign-in brand colors (leave as-is)
`#4285F4` `#34A853` `#FBBC05` `#EA4335`

---

## 3. Typography

- **Font family:** `Inter`, with fallback `system-ui, -apple-system, sans-serif`.
- **Import (CSS):**
  ```css
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
  ```
- **Weights available:** 300, 400, 500, 600, 700, 800, 900.
- **Font smoothing:** `-webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;`
- **Tailwind config:** `fontFamily: { sans: ["Inter", "system-ui", "sans-serif"] }`

### Type scale (from Hero / headings)
| Element | Classes |
|---------|---------|
| H1 / Hero | `text-4xl sm:text-5xl md:text-6xl lg:text-7xl font-bold leading-tight` |
| Section stat | `text-3xl lg:text-4xl font-bold` |
| Body large | `text-lg sm:text-xl md:text-2xl text-gray-300 leading-relaxed` |
| Body / base | `text-base` |
| Small / badge | `text-sm font-medium` |

---

## 4. Component Styles

### Buttons (canonical `Button` component)
Base: `inline-flex items-center justify-center font-semibold rounded-full transition-all duration-300 transform hover:scale-105 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-transparent`

**Variants**
- `primary`: `bg-gradient-to-r from-sky-400 to-blue-600 hover:from-sky-500 hover:to-blue-700 text-white shadow-lg hover:shadow-xl focus:ring-sky-500`
- `secondary`: `bg-transparent border-2 border-white/20 text-white hover:border-white/40 hover:bg-white/10 backdrop-blur-sm focus:ring-white/50`

**Sizes**
- `sm`: `px-4 py-2 text-sm`
- `md`: `px-6 py-3 text-base`
- `lg`: `px-8 py-4 text-lg`

```jsx
const Button = ({ children, variant = 'primary', size = 'md', className = '', ...props }) => {
  const baseClasses = 'inline-flex items-center justify-center font-semibold rounded-full transition-all duration-300 transform hover:scale-105 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-transparent';
  const variants = {
    primary: 'bg-gradient-to-r from-sky-400 to-blue-600 hover:from-sky-500 hover:to-blue-700 text-white shadow-lg hover:shadow-xl focus:ring-sky-500',
    secondary: 'bg-transparent border-2 border-white/20 text-white hover:border-white/40 hover:bg-white/10 backdrop-blur-sm focus:ring-white/50',
  };
  const sizes = { sm: 'px-4 py-2 text-sm', md: 'px-6 py-3 text-base', lg: 'px-8 py-4 text-lg' };
  return <button className={`${baseClasses} ${variants[variant]} ${sizes[size]} ${className}`} {...props}>{children}</button>;
};
export default Button;
```

### Header / Navbar
- Sticky, transparent over dark bg: `sticky top-0 relative z-50 w-full`
- Container: `max-w-7xl mx-auto px-4 sm:px-6 lg:px-8`, row height `h-16 md:h-20`
- Logo left, `secondary` Login button right; hamburger (`lucide-react` `Menu`/`X`) on `sm:hidden`.
- Mobile menu panel: `bg-black/95 backdrop-blur-lg border-t border-white/10`.

### Cards / badges
- Glass badge: `bg-white/10 backdrop-blur-sm border border-white/20 rounded-full px-4 py-2`
- Number badge: `bg-gradient-to-r from-[#002a77] to-[#120f0f] text-white rounded-full w-8 h-8 flex items-center justify-center`
- Grid item hover: lift + shadow (`gallery-grid-item` — see animations).

### Backgrounds & overlays
- Page base: `bg-gradient-to-br from-black via-[#091e4c] to-black` (or via `#0a0f1c`).
- Grid overlay pattern:
  ```
  bg-[linear-gradient(to_right,#0a0a0a_1px,transparent_1px),linear-gradient(to_bottom,#0a0a0a_1px,transparent_1px)] bg-[size:4rem_4rem]
  ```
- Ambient blurred blobs: large `rounded-full blur-3xl animate-pulse` radial gradients (indigo/purple/blue).

### Rounding & effects conventions
- Buttons & badges: `rounded-full`.
- Cards/inputs: `rounded-lg` / `rounded-xl`.
- Frequent use of `backdrop-blur-sm` / `backdrop-blur-lg` for glassmorphism.
- Standard transition: `transition-all duration-300`.

---

## 5. Animations (add to global CSS)

```css
@keyframes gradient-x { 0%,100% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } }
@keyframes pulse-slow { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
@keyframes fadeIn { from { opacity: 0; transform: scale(0.95); } to { opacity: 1; transform: scale(1); } }
@keyframes shimmer { 0% { background-position: -1000px 0; } 100% { background-position: 1000px 0; } }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(59,130,246,0.5); } 70% { box-shadow: 0 0 0 10px rgba(59,130,246,0); } 100% { box-shadow: 0 0 0 0 rgba(59,130,246,0); } }

.animate-gradient-x { background-size: 200% 200%; animation: gradient-x 3s ease infinite; }
.animate-pulse-slow { animation: pulse-slow 2s ease-in-out infinite; }
.image-fade-in { animation: fadeIn 0.3s ease-out; }
.shimmer { background: linear-gradient(90deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.1) 50%, rgba(255,255,255,0) 100%); background-size: 1000px 100%; animation: shimmer 2s infinite; }
.gallery-grid-item { transition: all 0.3s cubic-bezier(0.4,0,0.2,1); }
.gallery-grid-item:hover { transform: translateY(-2px); box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 10px 10px -5px rgba(0,0,0,0.04); }
```

Custom scrollbar (webkit): 8px width, `border-radius: 4px`, thumb `rgb(75 85 99)` on `rgb(31 41 55)` track.

---

## 6. Tech Stack & Libraries (for consistency)

- **Framework:** React 18 + Vite
- **Styling:** Tailwind CSS 3 + PostCSS + Autoprefixer
- **Icons:** `lucide-react`
- **Animation:** `framer-motion`
- **Notifications:** `react-hot-toast` (dark toast: bg `#1f2937`, text `#fff`, success `#10b981`, error `#ef4444`)
- **Routing:** `react-router-dom`
- **State:** `@reduxjs/toolkit` + `react-redux`
- **Data fetching:** `@tanstack/react-query`
- **QR codes:** `qrcode` (dark `#000000`, light `#FFFFFF`)

---

## 7. Ready-to-paste Tailwind config extension

```js
// tailwind.config.js — theme.extend
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        "custom-blue": "#091e4c",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      animation: {
        pulse: 'pulse 4s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        bounce: "bounce 2s infinite",
      },
    },
  },
  plugins: [],
};
```

## 8. Ready-to-paste global CSS header

```css
@import 'tailwindcss/base';
@import 'tailwindcss/components';
@import 'tailwindcss/utilities';
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
html { scroll-behavior: smooth; }
```

## 9. HTML `<head>` meta (theme color)

```html
<meta name="theme-color" content="#000062" />
<link rel="icon" type="image/svg+xml" href="/favicon.ico" />
```

---

### Quick prompt to give Copilot in the new project
> "Build the [feature] following `brand-kit/BRAND-DESIGN-SYSTEM.md`: dark black/navy background,
> Inter font, cyan→blue gradient primary buttons (`rounded-full`), glassmorphism cards, the
> Hapzea feather logo from `brand-kit/assets/logo/logo.png`, and `lucide-react` icons."
