# Frontend

React 19 + Vite 7 app scaffold with Tailwind 4, shadcn/ui (radix-vega style), strict TypeScript, ESLint, Prettier, and Vitest 5.

## Commands

Run from `frontend/`:

- `npm ci`: install dependencies
- `npm run dev`: start dev server with hot reload (proxies `/api` to port 8765)
- `npm test`: run unit tests with Vitest
- `npm run build`: typecheck and build into `backend/src/mlops_agents/web`
- `npm run lint`: run ESLint
- `npm run format:check`: check formatting with Prettier
- `npm run format`: auto-format with Prettier
- `npm run gen:api`: regenerate API types from `openapi.json`
