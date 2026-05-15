# MA Rent Intelligence Platform — Frontend Implementation

## For: Claude Code
## Version: 1.0
## Date: April 13, 2026
## Location: `ma_poc/frontend/` (create new)
## Service Layer: `ma_poc/services/` (create new)

---

# TABLE OF CONTENTS

1. How to use this file
2. Project overview & goals
3. Directory structure
4. Technology stack
5. Design system & visual language
6. Service layer architecture (`ma_poc/services/`)
7. API layer (`ma_poc/frontend/api/`)
8. Frontend application (`ma_poc/frontend/`)
9. View A — editorial magazine (landing/overview)
10. View B — split-panel data terminal (primary workspace)
11. View C — map-first spatial explorer
12. Property detail page
13. Daily diff dashboard
14. System health dashboard
15. Shared components
16. State management
17. Testing strategy
18. Screenshot tests
19. Documentation requirements
20. Implementation sequence

---

# 1. HOW TO USE THIS FILE

Read this ENTIRE file before writing any code. This is your single source of truth.

**Mandatory workflow for every task:**
1. Read the relevant section of this file
2. Implement fully — no stubs, no TODOs, no placeholder components
3. Add JSDoc comments to every exported function and component
4. Write tests immediately after implementation
5. Run tests: `cd ma_poc/frontend && npm test` (unit/integration)
6. Run lint: `npm run lint && npm run type-check`
7. Run dev server and visually confirm: `npm run dev`
8. Run screenshot tests: `npm run test:screenshots`
9. Run E2E tests: `npm run test:e2e`

**Do not:**
- Skip writing tests for any component
- Use placeholder images or dummy components that say "coming soon"
- Hardcode data — always flow through the service layer
- Use inline styles except for truly dynamic values (widths from data)
- Import from parent directories (`../../../`) — use path aliases (`@/`)

---

# 2. PROJECT OVERVIEW & GOALS

Build a production-grade analytics dashboard for the MA Rent Intelligence Platform. The frontend consumes data produced by the backend scraping pipeline (JSON files on disk) through a modular service layer.

**Three primary property views (all accessible, switchable via segmented control):**
- **View A — Editorial magazine:** Landing page with hero cards, editorial layout, visual hierarchy emphasising the most interesting properties. Uses serif display font for property names, large image areas, concession callouts.
- **View B — Split-panel data terminal:** Left sidebar property list + right detail pane with unit table and inline charts. No page transitions — clicking a property updates the right pane instantly. Primary power-user workspace.
- **View C — Map-first spatial explorer:** Leaflet map with property pins (sized by unit count, colored by tier), floating popup cards on click, and a collapsible right sidebar with market analytics (tier distribution, scrape heatmap, ranked lists).

**Additional pages:**
- Property detail page (drill-down from any view)
- Daily diff dashboard (date-navigable, 6-metric summary, rent change panels, concession tracking)
- System health / admin dashboard (success rate, tier distribution, failure analysis, entity resolution)

**Core principles:**
- All data flows through a service abstraction layer — never read files directly from UI code
- The service layer is extensible: today it reads JSON files, tomorrow it reads from PostgreSQL — only a new implementation folder is needed
- Every component has tests. Every page has screenshot baselines.
- Professional, distinctive visual design — not generic dashboard templates
- Dark mode support from day one
- Responsive down to tablet (1024px minimum)

---

# 3. DIRECTORY STRUCTURE

Create these directories and files exactly as specified.

```
ma_poc/
├── services/                              # Backend service layer (NEW)
│   ├── README.md
│   ├── package.json
│   ├── tsconfig.json
│   ├── src/
│   │   ├── index.ts                       # Public barrel export
│   │   ├── interfaces/
│   │   │   ├── IPropertyService.ts
│   │   │   ├── IUnitService.ts
│   │   │   ├── IRunService.ts
│   │   │   ├── IDiffService.ts
│   │   │   └── IHealthService.ts
│   │   ├── types/
│   │   │   ├── property.ts
│   │   │   ├── unit.ts
│   │   │   ├── run.ts
│   │   │   ├── diff.ts
│   │   │   ├── health.ts
│   │   │   └── common.ts                  # PaginatedResult, filters, sort
│   │   ├── implementations/
│   │   │   ├── json-file/
│   │   │   │   ├── JsonFilePropertyService.ts
│   │   │   │   ├── JsonFileUnitService.ts
│   │   │   │   ├── JsonFileRunService.ts
│   │   │   │   ├── JsonFileDiffService.ts
│   │   │   │   ├── JsonFileHealthService.ts
│   │   │   │   └── dataLoader.ts          # File I/O, caching, path resolution
│   │   │   └── README.md                  # How to add a new implementation
│   │   ├── factory.ts                     # Service factory — picks impl by config
│   │   └── logger.ts                      # Structured pino logger
│   └── tests/
│       ├── factory.test.ts
│       ├── json-file/
│       │   ├── PropertyService.test.ts
│       │   ├── UnitService.test.ts
│       │   ├── RunService.test.ts
│       │   ├── DiffService.test.ts
│       │   └── HealthService.test.ts
│       └── fixtures/
│           ├── properties.json
│           ├── report.json
│           ├── issues.jsonl
│           ├── ledger.jsonl
│           ├── property_index.json
│           └── unit_index.json
│
├── frontend/                              # React frontend (NEW)
│   ├── README.md
│   ├── CLAUDE.md                          # Copy of this file
│   ├── package.json
│   ├── tsconfig.json
│   ├── tsconfig.node.json
│   ├── vite.config.ts
│   ├── tailwind.config.ts
│   ├── postcss.config.js
│   ├── .eslintrc.cjs
│   ├── .prettierrc
│   ├── playwright.config.ts
│   ├── index.html
│   ├── public/
│   │   └── favicon.svg
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx                        # Router + QueryClient + providers
│   │   ├── index.css                      # Tailwind directives + CSS vars
│   │   ├── vite-env.d.ts
│   │   ├── types/
│   │   │   ├── views.ts                   # ViewMode enum, filter types
│   │   │   └── charts.ts                  # Chart config types
│   │   ├── api/
│   │   │   ├── client.ts                  # Axios instance + interceptors
│   │   │   ├── properties.ts
│   │   │   ├── units.ts
│   │   │   ├── runs.ts
│   │   │   ├── diff.ts
│   │   │   └── health.ts
│   │   ├── hooks/
│   │   │   ├── useProperties.ts
│   │   │   ├── usePropertyDetail.ts
│   │   │   ├── useRunHistory.ts
│   │   │   ├── useDailyDiff.ts
│   │   │   ├── useHealthMetrics.ts
│   │   │   ├── useDebounce.ts
│   │   │   └── useLocalStorage.ts
│   │   ├── stores/
│   │   │   ├── viewStore.ts               # Active view mode (A/B/C)
│   │   │   ├── filterStore.ts             # Global filter state
│   │   │   └── selectionStore.ts          # Selected property (terminal view)
│   │   ├── pages/
│   │   │   ├── ExplorePage.tsx            # Three-view switchable explorer
│   │   │   ├── PropertyDetailPage.tsx
│   │   │   ├── DailyDiffPage.tsx
│   │   │   ├── SystemPage.tsx
│   │   │   └── NotFoundPage.tsx
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   │   ├── AppShell.tsx
│   │   │   │   ├── TopNav.tsx
│   │   │   │   ├── Breadcrumb.tsx
│   │   │   │   └── ViewSwitcher.tsx       # A/B/C segmented toggle
│   │   │   ├── views/
│   │   │   │   ├── editorial/
│   │   │   │   │   ├── EditorialView.tsx
│   │   │   │   │   ├── HeroPropertyCard.tsx
│   │   │   │   │   ├── SidebarPropertyCard.tsx
│   │   │   │   │   ├── GridPropertyCard.tsx
│   │   │   │   │   └── EditorialStats.tsx
│   │   │   │   ├── terminal/
│   │   │   │   │   ├── TerminalView.tsx
│   │   │   │   │   ├── PropertyList.tsx   # Virtualised left panel
│   │   │   │   │   ├── PropertyListItem.tsx
│   │   │   │   │   ├── DetailPane.tsx
│   │   │   │   │   ├── UnitTable.tsx      # Sortable columns
│   │   │   │   │   ├── InlineCharts.tsx
│   │   │   │   │   └── RentSparkline.tsx
│   │   │   │   └── spatial/
│   │   │   │       ├── SpatialView.tsx
│   │   │   │       ├── PropertyMap.tsx    # react-leaflet
│   │   │   │       ├── MapPin.tsx
│   │   │   │       ├── MapPopup.tsx
│   │   │   │       ├── MapSidebar.tsx     # Collapsible analytics
│   │   │   │       ├── TierDistribution.tsx
│   │   │   │       ├── ScrapeHeatmap.tsx
│   │   │   │       └── RankedList.tsx
│   │   │   ├── property-detail/
│   │   │   │   ├── PropertyHero.tsx
│   │   │   │   ├── PropertyMetricBar.tsx
│   │   │   │   ├── ScreenshotGallery.tsx
│   │   │   │   ├── FloorPlanSection.tsx
│   │   │   │   ├── UnitCard.tsx
│   │   │   │   ├── UnitDetailDrawer.tsx
│   │   │   │   ├── PropertyCharts.tsx
│   │   │   │   ├── RentDistributionChart.tsx
│   │   │   │   ├── RentByFloorPlanChart.tsx
│   │   │   │   ├── RentPerSqftChart.tsx
│   │   │   │   ├── AvailabilityDonutChart.tsx
│   │   │   │   └── PropertyTimeline.tsx
│   │   │   ├── diff/
│   │   │   │   ├── DiffDashboard.tsx
│   │   │   │   ├── DiffSummaryStrip.tsx
│   │   │   │   ├── RentChangePanel.tsx
│   │   │   │   ├── PropertyChangePanel.tsx
│   │   │   │   ├── ConcessionPanel.tsx
│   │   │   │   └── ChangeTimeline.tsx
│   │   │   ├── system/
│   │   │   │   ├── HealthDashboard.tsx
│   │   │   │   ├── HealthCards.tsx
│   │   │   │   ├── RunHistoryTable.tsx
│   │   │   │   ├── FailureAnalysis.tsx
│   │   │   │   ├── EntityResolution.tsx
│   │   │   │   └── AlertBanner.tsx
│   │   │   ├── filters/
│   │   │   │   ├── SearchBar.tsx
│   │   │   │   ├── FilterChips.tsx
│   │   │   │   ├── FilterPanel.tsx
│   │   │   │   └── SortSelect.tsx
│   │   │   └── shared/
│   │   │       ├── MetricCard.tsx
│   │   │       ├── TierBadge.tsx
│   │   │       ├── StatusDot.tsx
│   │   │       ├── ConcessionTag.tsx
│   │   │       ├── PropertyImage.tsx      # Screenshot or SVG building
│   │   │       ├── EmptyState.tsx
│   │   │       ├── LoadingSkeleton.tsx
│   │   │       ├── ErrorBoundary.tsx
│   │   │       ├── ChartWrapper.tsx
│   │   │       ├── ExportButton.tsx
│   │   │       └── Pagination.tsx
│   │   └── utils/
│   │       ├── formatters.ts              # Currency, date, number, percent
│   │       ├── colors.ts                  # Design tokens + tier map
│   │       ├── sorting.ts
│   │       ├── filtering.ts
│   │       ├── csv.ts
│   │       └── logger.ts                  # Frontend console logger
│   ├── tests/
│   │   ├── setup.ts
│   │   ├── mocks/
│   │   │   ├── handlers.ts               # MSW request handlers
│   │   │   ├── server.ts                  # MSW setup
│   │   │   ├── properties.ts             # Factory functions
│   │   │   └── units.ts
│   │   ├── unit/
│   │   │   ├── components/
│   │   │   │   ├── TierBadge.test.tsx
│   │   │   │   ├── StatusDot.test.tsx
│   │   │   │   ├── MetricCard.test.tsx
│   │   │   │   ├── ConcessionTag.test.tsx
│   │   │   │   ├── PropertyImage.test.tsx
│   │   │   │   ├── SearchBar.test.tsx
│   │   │   │   ├── FilterChips.test.tsx
│   │   │   │   ├── ViewSwitcher.test.tsx
│   │   │   │   ├── HeroPropertyCard.test.tsx
│   │   │   │   ├── PropertyListItem.test.tsx
│   │   │   │   ├── UnitCard.test.tsx
│   │   │   │   ├── MapPopup.test.tsx
│   │   │   │   └── AlertBanner.test.tsx
│   │   │   ├── hooks/
│   │   │   │   ├── useProperties.test.ts
│   │   │   │   ├── useDebounce.test.ts
│   │   │   │   └── useLocalStorage.test.ts
│   │   │   ├── utils/
│   │   │   │   ├── formatters.test.ts
│   │   │   │   ├── sorting.test.ts
│   │   │   │   ├── filtering.test.ts
│   │   │   │   └── csv.test.ts
│   │   │   └── stores/
│   │   │       ├── viewStore.test.ts
│   │   │       ├── filterStore.test.ts
│   │   │       └── selectionStore.test.ts
│   │   ├── integration/
│   │   │   ├── EditorialView.test.tsx
│   │   │   ├── TerminalView.test.tsx
│   │   │   ├── SpatialView.test.tsx
│   │   │   ├── PropertyDetail.test.tsx
│   │   │   ├── DiffDashboard.test.tsx
│   │   │   └── Navigation.test.tsx
│   │   ├── e2e/
│   │   │   ├── explore-editorial.spec.ts
│   │   │   ├── explore-terminal.spec.ts
│   │   │   ├── explore-spatial.spec.ts
│   │   │   ├── property-detail.spec.ts
│   │   │   ├── daily-diff.spec.ts
│   │   │   ├── system-health.spec.ts
│   │   │   ├── view-switching.spec.ts
│   │   │   └── navigation.spec.ts
│   │   └── screenshots/
│   │       ├── visual-regression.spec.ts  # All screenshot tests
│   │       ├── baselines/                 # Git-tracked baseline PNGs
│   │       │   └── .gitkeep
│   │       └── README.md
│   └── api/                               # Express API server
│       ├── package.json
│       ├── tsconfig.json
│       ├── README.md
│       ├── src/
│       │   ├── server.ts
│       │   ├── config.ts
│       │   ├── routes/
│       │   │   ├── properties.ts
│       │   │   ├── runs.ts
│       │   │   ├── diff.ts
│       │   │   └── health.ts
│       │   └── middleware/
│       │       ├── errorHandler.ts
│       │       ├── requestLogger.ts
│       │       └── validation.ts
│       └── tests/
│           └── routes/
│               ├── properties.test.ts
│               ├── runs.test.ts
│               └── diff.test.ts
```

---

# 4. TECHNOLOGY STACK

## Frontend
| Package | Version | Purpose |
|---------|---------|---------|
| react | ^18.3 | UI framework |
| react-dom | ^18.3 | DOM rendering |
| react-router-dom | ^6.22 | Client routing |
| typescript | ^5.4 | Type safety |
| vite | ^5.4 | Build + dev server |
| @tanstack/react-query | ^5.x | Server state, caching, refetch |
| @tanstack/react-virtual | ^3.x | Virtual scrolling (terminal view list) |
| zustand | ^4.5 | Client state (view mode, filters, selection) |
| recharts | ^2.12 | Charts — React-native, composable |
| tailwindcss | ^3.4 | Utility-first styling |
| leaflet | ^1.9 | Map rendering |
| react-leaflet | ^4.2 | React Leaflet bindings |
| axios | ^1.7 | HTTP client with interceptors |
| lucide-react | latest | Tree-shakable icons |
| date-fns | ^3.x | Date formatting |
| framer-motion | ^11.x | View transitions, list animations |
| clsx | ^2.x | Conditional class merging |

## Service layer
| Package | Version | Purpose |
|---------|---------|---------|
| typescript | ^5.4 | Type safety |
| pino | ^9.x | Structured JSON logging |
| pino-pretty | ^11.x | Dev-mode log formatting |
| glob | ^10.x | File pattern matching |
| chokidar | ^3.6 | File watching for live run detection |

## API server
| Package | Version | Purpose |
|---------|---------|---------|
| express | ^4.19 | HTTP server |
| cors | ^2.8 | CORS middleware |
| tsx | ^4.x | TS execution in dev |
| pino-http | ^10.x | Request logging |
| zod | ^3.22 | Query param validation |

## Testing
| Package | Version | Purpose |
|---------|---------|---------|
| vitest | ^1.6 | Unit + integration |
| @testing-library/react | ^15.x | Component testing |
| @testing-library/user-event | ^14.x | User interaction simulation |
| @testing-library/jest-dom | ^6.x | DOM matchers |
| jsdom | ^24.x | DOM environment |
| msw | ^2.x | API mocking |
| @playwright/test | ^1.43 | E2E + screenshot tests |

---

# 5. DESIGN SYSTEM & VISUAL LANGUAGE

## 5.1 Aesthetic direction

**Tone:** Refined industrial — Bloomberg's data density married to Sotheby's visual luxury. Clean surfaces, precise typography, controlled color accent.

**Memorable trait:** The three-view switcher. One click transforms the information architecture — editorial cards dissolve into a split-panel terminal, then into a spatial map. Same data, three lenses. Use `framer-motion` `<AnimatePresence mode="wait">` for smooth crossfade between views.

## 5.2 Typography

Load via Google Fonts in `index.html`:
```html
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500&family=Instrument+Serif&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

```typescript
// tailwind.config.ts
fontFamily: {
  sans: ['"DM Sans"', 'system-ui', 'sans-serif'],     // Body, labels, UI
  mono: ['"JetBrains Mono"', 'monospace'],              // Rent values, data
  display: ['"Instrument Serif"', 'Georgia', 'serif'],  // Hero property names
}
```

**Usage rules:**
- `font-display`: Property names in HeroPropertyCard, page-level headings only
- `font-sans`: All body text, labels, table headers, filter chips, badges
- `font-mono`: Rent values, unit numbers, percentages, metric card values — anywhere numbers need tabular alignment
- Weights: 400 (regular) and 500 (medium) only. Never use 600/700.
- Sizes: page title 22px, section heading 16px, body 13px, labels 11px uppercase tracking-wide, data values 13px mono, large metrics 22px mono

## 5.3 Color palette

```typescript
// tailwind.config.ts extend.colors
colors: {
  rent: {
    50: '#E1F5EE', 100: '#9FE1CB', 200: '#5DCAA5',
    400: '#1D9E75', 600: '#0F6E56', 800: '#085041', 900: '#04342C',
  },
  // Tier colors
  tier: {
    api: '#1D9E75', jsonld: '#378ADD', dom: '#534AB7',
    llm: '#EF9F27', vision: '#D85A30', fail: '#E24B4A',
  },
  // Status
  status: { available: '#1D9E75', leased: '#ADB5BD', unknown: '#EF9F27' },
  // Change direction
  change: { up: '#E24B4A', down: '#1D9E75', new: '#378ADD', gone: '#868E96' },
}
```

**Tier badge styles (define in `src/utils/colors.ts`):**
```typescript
export const TIER_STYLES = {
  TIER_1_API:     { bg: 'bg-emerald-50 dark:bg-emerald-950', text: 'text-emerald-800 dark:text-emerald-200', label: 'API' },
  TIER_2_JSONLD:  { bg: 'bg-blue-50 dark:bg-blue-950', text: 'text-blue-800 dark:text-blue-200', label: 'JSON-LD' },
  TIER_3_DOM:     { bg: 'bg-violet-50 dark:bg-violet-950', text: 'text-violet-800 dark:text-violet-200', label: 'DOM' },
  TIER_4_LLM:     { bg: 'bg-amber-50 dark:bg-amber-950', text: 'text-amber-800 dark:text-amber-200', label: 'LLM' },
  TIER_5_VISION:  { bg: 'bg-orange-50 dark:bg-orange-950', text: 'text-orange-800 dark:text-orange-200', label: 'Vision' },
  FAILED:         { bg: 'bg-red-50 dark:bg-red-950', text: 'text-red-800 dark:text-red-200', label: 'Failed' },
} as const;
```

## 5.4 Layout tokens

- Page max-width: 1440px centered (`max-w-7xl mx-auto`)
- Content padding: `px-6`
- Card: `rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900`
- Metric card: `rounded-lg bg-slate-50 dark:bg-slate-800/50 p-4`
- Section gap: `gap-4`, grid gap: `gap-3`
- Dark mode: `darkMode: 'class'` in Tailwind. Toggle via TopNav button. Persist in localStorage.

## 5.5 Motion (Framer Motion)

```typescript
// Shared animation variants — define in src/utils/motion.ts
export const fadeSlideUp = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -8 },
  transition: { duration: 0.2, ease: 'easeOut' },
};

export const staggerChildren = {
  animate: { transition: { staggerChildren: 0.04 } },
};

export const cardHover = {
  whileHover: { y: -2, transition: { duration: 0.15 } },
};
```

Wrap view switches in `<AnimatePresence mode="wait">` for crossfade.

---

# 6. SERVICE LAYER (`ma_poc/services/`)

The abstraction layer between data storage and the UI. All data access goes through interfaces. Implementations are swappable.

## 6.1 Core interfaces

Define 5 interfaces in `src/interfaces/`. Every method returns a Promise. Every method has full JSDoc.

**IPropertyService:**
- `getProperties(filters?, sort?, page?, pageSize?)` → `PaginatedResult<PropertySummary>`
- `getPropertyById(id)` → `Property | null`
- `getAggregateStats(filters?)` → `PropertyAggregates`
- `searchProperties(query, limit?)` → `PropertySummary[]`
- `getRankedProperties(metric, direction, limit?)` → `PropertySummary[]`

**IUnitService:**
- `getUnitsByProperty(propertyId)` → `Unit[]`
- `getUnitsByFloorPlan(propertyId)` → `FloorPlanGroup[]`
- `getUnitHistory(propertyId, unitId)` → `UnitHistoryEntry[]`

**IRunService:**
- `getRunHistory(limit?)` → `RunSummary[]`
- `getRunByDate(date)` → `RunDetail | null`
- `getLatestRun()` → `RunDetail`

**IDiffService:**
- `getDailyDiff(date)` → `DailyDiff`
- `getLatestDiff()` → `DailyDiff`
- `getPropertyChangelog(propertyId, days?)` → `ChangelogEntry[]`

**IHealthService:**
- `getHealthSummary()` → `HealthSummary`
- `getTierDistribution()` → `TierDistribution`
- `getTopFailures(limit?)` → `FailureRecord[]`
- `getEntityResolutionStats()` → `EntityResolutionStats`

## 6.2 Type definitions (`src/types/`)

Define all types in dedicated files. Key types:

```typescript
// types/property.ts
export interface PropertySummary {
  id: string;
  name: string;
  address: string;
  city: string;
  state: string;
  zip: string;
  latitude: number;
  longitude: number;
  managementCompany: string;
  totalUnits: number;
  avgAskingRent: number;
  medianAskingRent: number;
  availabilityRate: number;
  availableUnits: number;
  extractionTier: ExtractionTier;
  scrapeStatus: ScrapeStatus;
  propertyStatus: PropertyStatus;
  yearBuilt: number;
  stories: number;
  activeConcession: string | null;
  lastScrapeTimestamp: string;
  carryForwardDays: number;
  imageUrl: string | null;
  websiteUrl: string;
}

export interface Property extends PropertySummary {
  units: Unit[];
  floorPlans: FloorPlan[];
  marketMetrics: MarketMetrics;
  scrapeHistory: ScrapeEvent[];
  screenshotPaths: { pricingPage: string | null; banner: string | null };
}

export type ExtractionTier = 'TIER_1_API' | 'TIER_2_JSONLD' | 'TIER_3_DOM' | 'TIER_4_LLM' | 'TIER_5_VISION' | 'FAILED';
export type ScrapeStatus = 'SUCCESS' | 'FAILED' | 'CARRIED_FORWARD' | 'SKIPPED';
export type PropertyStatus = 'ACTIVE' | 'LEASE_UP' | 'STABILISED' | 'OFFLINE';
```

```typescript
// types/common.ts
export interface PaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

export interface PropertyFilters {
  search?: string;
  cities?: string[];
  tiers?: ExtractionTier[];
  statuses?: ScrapeStatus[];
  propertyStatuses?: PropertyStatus[];
  minRent?: number;
  maxRent?: number;
  hasConcession?: boolean;
}

export interface SortOptions {
  field: string;
  direction: 'asc' | 'desc';
}
```

## 6.3 Service factory

```typescript
// src/factory.ts
export type ServiceImplementation = 'json-file' | 'database';

export interface ServiceConfig {
  implementation: ServiceImplementation;
  dataDir?: string;
  connectionString?: string;  // Future DB implementation
}

export function createServices(config: ServiceConfig) {
  switch (config.implementation) {
    case 'json-file':
      if (!config.dataDir) throw new Error('dataDir required for json-file');
      return {
        properties: new JsonFilePropertyService(config.dataDir),
        units: new JsonFileUnitService(config.dataDir),
        runs: new JsonFileRunService(config.dataDir),
        diff: new JsonFileDiffService(config.dataDir),
        health: new JsonFileHealthService(config.dataDir),
      };
    default:
      throw new Error(`Unknown implementation: ${config.implementation}`);
  }
}
```

## 6.4 JSON file implementation rules

- Use `dataLoader.ts` for ALL file I/O — centralised caching (60s TTL), error handling, path resolution
- The "latest run" = most recent YYYY-MM-DD directory in `data/runs/`
- All filtering, sorting, pagination happens in-memory after loading
- Log every file read and cache hit/miss via pino
- Handle missing files gracefully — return empty results, not thrown errors
- Use structured logging: `logger.info({ file, cached, duration_ms }, 'loaded properties')`

## 6.5 How to add new implementations

Document in `implementations/README.md`:
1. Create folder `implementations/my-impl/`
2. Implement all 5 interfaces
3. Add case to `factory.ts`
4. Add config fields to `ServiceConfig` if needed
5. Write tests in `tests/my-impl/`
6. Update this README

---

# 7. API LAYER (`ma_poc/frontend/api/`)

Express server exposing the service layer over HTTP.

## Routes

```
GET  /api/properties                     # ?page=&pageSize=&search=&city=&tier=&sort=&dir=
GET  /api/properties/stats               # Aggregate stats
GET  /api/properties/search?q=           # Text search
GET  /api/properties/ranked?metric=&dir=&limit=
GET  /api/properties/:id                 # Full property + units

GET  /api/runs                           # Run history
GET  /api/runs/latest                    # Latest run
GET  /api/runs/:date                     # Run by date

GET  /api/diff/latest                    # Latest diff
GET  /api/diff/:date                     # Diff for specific date

GET  /api/health                         # Health summary
GET  /api/health/tiers                   # Tier distribution
GET  /api/health/failures                # Top failures
GET  /api/health/identity                # Entity resolution stats
```

## Server setup

- Instantiate services via factory in `server.ts`
- Pass services to route handlers via closure (not global)
- Use `zod` for query param validation in middleware
- Global error handler returns `{ error: string, details?: string }`
- Request logger uses `pino-http`
- CORS enabled for `localhost:5173` (Vite dev)

## Vite proxy

```typescript
// vite.config.ts
server: { proxy: { '/api': 'http://localhost:3005' } }
```

---

# 8–11. THE THREE VIEWS

## View A — Editorial magazine

**Layout:** Top stats bar → hero card (1.4fr) + sidebar stack (1fr) → 4-column grid of smaller cards → pagination.

**HeroPropertyCard:** Largest card. 200px image area (screenshot or generated SVG building). Property name in `font-display` (Instrument Serif). Three metric cards inline (units, avg rent, availability). Tier + status tags.

**SidebarPropertyCard:** Horizontal. 80x80 thumbnail + name/addr/stats right. Stack of 3 cards. Show next-most-interesting properties (highest availability, newest concessions, lease-ups).

**GridPropertyCard:** 4-col grid. Small image header, name, address, 3 data rows. Concession strip if applicable. Failed properties get red warning strip.

**PropertyImage (shared):** If `imageUrl` exists → render image. Otherwise → deterministic SVG building generated from `propertyId` (seed color), `stories` (height), `totalUnits` (windows). Every property gets a unique-looking building.

## View B — Split-panel terminal

**Layout:** Left panel (340px, resizable) + right detail pane (flex-1). No page navigation.

**PropertyList (left):** Virtualised with `@tanstack/react-virtual`. Search at top, count badge. Each item shows name, city, unit count, avg rent, tier badge, availability. Selected item has teal left-border accent. Keyboard nav: arrow keys move selection, Enter opens detail page.

**DetailPane (right):** Header (name, address, tags, large rent number) → 6 inline metrics (units, available, median, DOM, sqft, $/sqft) → sortable unit table → 2x2 mini chart grid (rent by floor plan, 30-day trend, rent/sqft scatter, availability donut).

**UnitTable:** Full-width table. Columns: Unit, Floor plan, Type, Sqft, Asking rent, Effective rent, DOM, Status. Click column header to sort. Alternating row shading in dark mode.

## View C — Map-first spatial

**Layout:** Map area (flex-1) + collapsible right sidebar (280px).

**PropertyMap:** react-leaflet + OpenStreetMap tiles. Dark mode tile layer available. Fit bounds to show all properties on load.

**MapPin:** Circle marker. Size 24–40px scaled by `totalUnits`. Color by `extractionTier`. Failed properties semi-transparent. Unit count as text label inside.

**MapPopup:** On pin click → floating card with name, addr, 3 metrics, concession strip, tier/status badges, "View detail" button.

**MapSidebar:** Market summary (4 metrics), tier distribution bars, 14-day scrape activity heatmap (grid of colored squares), ranked lists (top by rent, most available). Collapse toggle button.

## View switcher

Segmented control in TopNav: `[Magazine] [Terminal] [Map]` with icons. Only visible on ExplorePage (`/`). URL reflects: `/?view=editorial|terminal|spatial`. Persist last-used in localStorage. Wrap in `<AnimatePresence mode="wait">`.

---

# 12. PROPERTY DETAIL PAGE (`/properties/:id`)

Accessible from any view. Full drill-down.

**PropertyHero:** 2-col. Left: screenshot/image (200px). Right: key-value metadata rows (management, year, stories, units, tier badge, status, concession). Name in `font-display`.

**PropertyMetricBar:** 5 metric cards: min rent, max rent, median, avg DOM, avg sqft.

**ScreenshotGallery:** Shows pricing page + banner screenshots if available. Click to enlarge (modal lightbox).

**FloorPlanSection:** Groups units under headers. Header: plan name, bed/bath, count, available count, avg rent. Below: responsive grid of UnitCards.

**UnitCard:** Mini-card showing unit number, rent, effective rent (green if discounted), sqft, $/sqft, status dot, DOM badge. Click → UnitDetailDrawer (slide-out panel).

**PropertyCharts:** 2x2 grid using Recharts. RentDistributionChart (histogram), RentByFloorPlanChart (horizontal bar), RentPerSqftChart (scatter), AvailabilityDonutChart (doughnut). All wrapped in ChartWrapper.

**PropertyTimeline:** Vertical dot-timeline built from change history. Dot color = event type (green=price drop, red=price up, blue=new listing, amber=concession, gray=leased).

---

# 13–14. DIFF & SYSTEM HEALTH

Follow the designs from the previous mockups in this conversation. Reference those designs directly for layout details.

**Daily diff:** Date nav (prev/next arrows + date label). 6-metric strip (rents up, down, new available, became leased, new concessions, disappeared). Two-col panels for increases/decreases with indicator bars. Concession panel. Per-property change timeline.

**System health:** 4 health cards with threshold coloring and bottom accent bars. Alert banner for 3+ day consecutive failures. Run history table (date, duration, count, rate, visual bar). Error code distribution bars. Entity resolution 3-tier funnel cards.

---

# 15. SHARED COMPONENTS

Every shared component must:
1. Accept typed props with JSDoc
2. Support dark mode
3. Have a unit test
4. Use `data-testid` for test targeting
5. Be exported from barrel `index.ts`

**Key components and their contracts:**

- `MetricCard`: props `label`, `value`, `subtitle?`, `accentColor?`, `trend?: 'up'|'down'`
- `TierBadge`: props `tier: ExtractionTier`, uses `TIER_STYLES` map
- `StatusDot`: props `status: 'available'|'leased'|'unknown'|'failed'`, optional `pulse` animation
- `ConcessionTag`: props `text: string`, amber background with star icon
- `PropertyImage`: props `imageUrl?`, `propertyId`, `stories`, `accentColor`, renders screenshot or SVG
- `LoadingSkeleton`: props `variant: 'card'|'table-row'|'metric'|'text-block'`
- `EmptyState`: props `title`, `description`, `action?`
- `ErrorBoundary`: wraps children, shows friendly error + retry button
- `ChartWrapper`: props `title`, `loading?`, responsive height container

---

# 16. STATE MANAGEMENT

**viewStore (Zustand):** `activeView: 'editorial' | 'terminal' | 'spatial'`. Persisted in localStorage.

**filterStore (Zustand):** `search`, `cities[]`, `tiers[]`, `statuses[]`, `sortField`, `sortDirection`, `page`, `pageSize`. Actions: `setSearch()`, `toggleCity()`, `toggleTier()`, `resetAll()`. Sync to URL query params via React Router.

**selectionStore (Zustand):** `selectedPropertyId: string | null`. Terminal view only. Not persisted.

**React Query config:**
```typescript
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, gcTime: 300_000, refetchOnWindowFocus: false, retry: 2 },
  },
});
```

---

# 17. TESTING STRATEGY

## Test pyramid

| Layer | Tool | Target | Tests |
|-------|------|--------|-------|
| Unit | Vitest + RTL | Shared components, hooks, utils, stores | 60+ |
| Integration | Vitest + RTL + MSW | Full view compositions with mocked API | 15+ |
| E2E | Playwright | Complete user flows in browser | 8+ specs |
| Screenshot | Playwright | Visual regression per page + mode | 12+ captures |

## Unit test rules

- Test renders with required props, optional props, edge cases (null, zero, empty)
- Test user interactions (click, type, hover)
- Test dark mode class application
- Test accessibility (roles, aria-labels)

## Integration test rules

- Use MSW to mock all `/api/*` endpoints
- Test full view compositions (EditorialView, TerminalView, SpatialView)
- Test data loading → rendering → interaction flows
- Test filter persistence across view switches

## E2E test rules

- Test complete user journeys: land on page → filter → click property → see detail → go back
- Test view switching preserves filters
- Test keyboard navigation in terminal view
- Test map interaction in spatial view

---

# 18. SCREENSHOT TESTS

## Playwright config for screenshots

```typescript
// playwright.config.ts
export default defineConfig({
  testDir: './tests/screenshots',
  snapshotDir: './tests/screenshots/baselines',
  snapshotPathTemplate: '{snapshotDir}/{testFilePath}/{arg}{ext}',
  fullyParallel: false,
  retries: 0,
  use: { baseURL: 'http://localhost:5173' },
  expect: {
    toHaveScreenshot: {
      maxDiffPixels: 100,
      threshold: 0.2,
      animations: 'disabled',
    },
  },
  projects: [
    { name: 'Desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'Tablet', use: { viewport: { width: 1024, height: 768 } } },
  ],
  webServer: { command: 'npm run dev', port: 5173, reuseExistingServer: true },
});
```

## Screenshot test file (`tests/screenshots/visual-regression.spec.ts`)

Capture baselines for every page in both light and dark mode:

**Light mode tests:**
1. `editorial-view-light.png` — full page, editorial view
2. `terminal-view-light.png` — terminal view with property selected
3. `spatial-view-light.png` — map view with sidebar
4. `property-detail-light.png` — property with units
5. `property-detail-concession-light.png` — property with concession
6. `property-detail-failed-light.png` — failed property
7. `daily-diff-light.png` — diff dashboard
8. `system-health-light.png` — system health

**Dark mode tests:** Same 8 captures with `-dark.png` suffix.

**How to write each test:**
```typescript
test('editorial view — light', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/?view=editorial', { waitUntil: 'networkidle' });
  await page.waitForSelector('[data-testid="hero-card"]');
  await page.waitForFunction(() => document.fonts.ready);
  await expect(page).toHaveScreenshot('editorial-view-light.png', { fullPage: true });
});
```

## NPM scripts

```json
"test:screenshots": "playwright test tests/screenshots/",
"test:screenshots:update": "playwright test tests/screenshots/ --update-snapshots",
"test:all": "npm run test && npm run test:e2e && npm run test:screenshots"
```

## Baseline management

- Baselines live in `tests/screenshots/baselines/` — commit to git
- After intentional changes: `npm run test:screenshots:update` then commit new PNGs
- Review diffs carefully in PRs
- Map tile screenshots may need `waitForTimeout(2000)` for tile loading

---

# 19. DOCUMENTATION REQUIREMENTS

## Code comments

Every file needs a file-level JSDoc:
```typescript
/**
 * @file JsonFilePropertyService.ts
 * @description Reads property data from JSON files in data/runs/.
 * Implements IPropertyService. Caches parsed data with 60s TTL.
 */
```

Every exported function/component needs JSDoc with `@param`, `@returns`, `@example`.

## README files (4 total)

1. **`ma_poc/services/README.md`** — Service layer overview, interfaces, extensibility guide
2. **`ma_poc/frontend/README.md`** — Setup, dev workflow, architecture, testing commands
3. **`ma_poc/frontend/api/README.md`** — Routes, config, middleware
4. **`ma_poc/frontend/tests/screenshots/README.md`** — Screenshot workflow, baseline management

## Logging

**Service layer:** pino with structured JSON. Levels: debug (cache), info (load), warn (missing file), error (parse fail).

**Frontend:** Console logger in `src/utils/logger.ts`. Log: API requests/responses with timing, view switches, filter changes, error boundary catches, image load failures.

**API server:** pino-http for request logging. Include method, path, status, duration.

---

# 20. IMPLEMENTATION SEQUENCE

Build in this exact order. Each step complete with tests before the next.

## Phase 1 — Foundation
1. Service layer types + interfaces
2. Service layer json-file implementation + dataLoader
3. Service layer tests
4. API server (Express routes + middleware)
5. API server tests
6. Frontend scaffold (Vite + React + Router + Tailwind + tokens + dark mode)
7. Shared components (all items in `shared/`) + unit tests

## Phase 2 — Views
8. View A — Editorial (hero, sidebar cards, grid, stats, filters)
9. View B — Terminal (split panel, virtualised list, detail pane, unit table, charts)
10. View C — Spatial (Leaflet map, pins, popups, sidebar)
11. View switcher + AnimatePresence transitions

## Phase 3 — Detail pages
12. Property detail page (hero, metrics, screenshots, floor plans, unit cards, charts, timeline)
13. Daily diff dashboard
14. System health dashboard

## Phase 4 — Polish & validation
15. Integration tests (all 6)
16. E2E tests (all 8 specs)
17. Screenshot tests (16+ baselines: 8 light + 8 dark)
18. README documentation (all 4 files)
19. Performance: virtual scrolling verification, bundle size, Lighthouse

---

# 21. DATA-TESTID CONVENTION

Every significant element gets `data-testid="{component}-{descriptor}"`:

```
hero-card, property-list, property-list-item, detail-pane, unit-table
view-editorial, view-terminal, view-spatial
filter-seattle, filter-failed, search-input, sort-select
health-cards, diff-summary, property-hero, screenshot-gallery
tier-badge-{tier}, metric-card-{metric}, concession-tag
floor-plan-{name}, unit-card-{number}
chart-rent-distribution, chart-rent-by-fp, chart-rent-sqft, chart-availability
map-container, map-sidebar, error-state, empty-state, loading-skeleton
```

---

# END OF INSTRUCTIONS

Implement in the order specified in Section 20. Every section is mandatory. Every test is mandatory. Do not skip documentation. Build it right, build it once.
Ensure that no code other than frontend and services need to be changes for this implementation.