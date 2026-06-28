# Team Vindland Saga — Frontend

Short description
- Purpose: Frontend web application for the Team Vindland Saga platform — dashboards, exams, alerts, analytics, and admin panels.

Key features
- Role-based dashboard (admin, instructor, student)
- Live monitoring and alerts
- Exam interfaces and proctoring UIs
- Analytics and reporting views

Quick start (development)

Prerequisites
- Node.js 18+ and npm (or pnpm/yarn)

Install and run

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000 in your browser. The dev server supports hot reload for files under `app/` and `components/`.

Backend (API)
- Location: `../backend`
- The backend is a FastAPI service. To run locally:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Environment variables
- Copy or create an env file for local development (examples):

- `DATABASE_URL` — Postgres connection string
- `SECRET_KEY` — app secret
- `FRONTEND_URL` — http://localhost:3000
- `OPENAI_API_KEY` — if AI integrations are used

Database
- Schema: `../database/schema.sql` — run in your database to create tables.

Testing
- Backend tests live in `../tests/` and are run with `pytest`.
- Frontend: run linters and test suite as configured in `package.json` (if present).

Project layout
- `app/` — Next.js App Router pages and layouts
- `components/` — shared UI components (Sidebar, TopNav, etc.)
- `public/` — static assets
- `styles/` or `globals.css` — global styles

Deployment
- Build the frontend for production:

```bash
cd frontend
npm run build
npm run start
```

- Recommended: deploy the backend and frontend to platforms that support Python and Node.js respectively (Vercel for frontend, Render/Heroku/AWS/GCP for API), and configure environment variables securely.

Contributing
- Fork the repo, create a feature branch, add tests for new behavior, and open a pull request.
- Keep frontend changes focused to UI/UX and component tests.

Troubleshooting
- If `npm run dev` fails, ensure Node.js version matches the project's `engines` (if set) and run `npm ci` to install exact dependency versions.
- For backend errors, activate the virtualenv before installing requirements.

License
- MIT (update as appropriate)

Maintainers / Contact
- Add project maintainer names and contact information here.

---

If you want, I can also:
- add a repository-level `README.md` at the project root
- include a short CONTRIBUTING.md or environment example file

