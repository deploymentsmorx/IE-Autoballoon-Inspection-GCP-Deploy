# SmorX Inspection Software

## Folders
- `frontend/` — React + Vite dark UI (Login, Super Admin, Inspection / ballooning)
- `backend/` — FastAPI API (auth, admin, multi-PDF ballooning, report PDF/Excel)

## Default login
- Email: `admin@smorx.ai`
- Password: `admin123`

## Run backend
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9686
```

## Run frontend
```bash
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:9835 (API proxied to backend on **9686**)

## Features
- Multiple PDF upload at once
- Auto ballooning (vector engine)
- Create / Edit (move) / Delete balloons with auto sequence renumber
- Inspection report with Instr. ID + measured columns
- Tolerance colors: green exact, yellow in-tolerance, red out-of-tolerance
- Add/remove extra measured columns
- Report PDF + Excel download
