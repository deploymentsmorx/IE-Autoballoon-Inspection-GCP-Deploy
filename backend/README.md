# SmorX AutoBalloon API

Standard FastAPI layout:

```
app/
  api/routes/     # auth, users, autoballoon
  core/           # config, security, database
  models/         # customer, user, license, processing_job
  schemas/        # pydantic request/response
  services/       # business logic + AutoBalloon engine
  utils/          # S3/file handler, logger
```

## Run locally

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9686
```

Health: `GET http://127.0.0.1:9686/api/health`

## Architecture

```
AutoBalloon.exe → FastAPI → Neon Postgres (metadata)
                         → AWS S3 (PDF files; local fallback)
```

Configure `backend/.env` from `.env.example`.

**Production S3:** `autoballoon-storage-smorx-prod` (us-east-1) — [bucket URL](https://autoballoon-storage-smorx-prod.s3.us-east-1.amazonaws.com/). PDFs are stored per user under `{prefix}/{username}/{job_id}/`. Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` on the server. Health shows `"storage": "s3"`, `"s3_bucket"`, and `"s3_url"` when enabled.
