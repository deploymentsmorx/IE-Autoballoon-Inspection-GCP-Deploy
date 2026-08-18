# AutoBalloon — Production deployment (AWS + Docker + EXE + Web)

**Target:** 10–50 users · **Web browser** + **AutoBalloon.exe** at customer sites · **One API** for both.

---

## Architecture

```
Customer PC                    AWS (EC2 + Docker)
┌─────────────────┐           ┌──────────────────────────────┐
│ AutoBalloon.exe │──────────►│ API  :9686  (FastAPI Docker) │
└─────────────────┘           │ Web  :8080  (nginx Docker)   │
                              └───────────┬──────────────────┘
Engineer browser ──► :8080                │
                                          ├── Neon Postgres
                                          └── S3 (PDF files)
```

| Component | Where | Notes |
|-----------|--------|--------|
| **API** | Docker on EC2 | Ballooning, auth, reports |
| **Web UI** | Docker on EC2 | `http://IP:8080` or domain |
| **EXE** | Customer Windows PC | Built separately, points to API URL |
| **Neon** | Cloud | Same DB URL on server |
| **S3** | `autoballoon-storage-smorx-prod` | PDF storage — `https://autoballoon-storage-smorx-prod.s3.us-east-1.amazonaws.com/` — files under `autoballoon/{username}/{job_id}/` |

**Do not install backend on customer PC.** Only the EXE goes to customers.

---

## What NOT to share in chat

Never paste in Slack/email to developers:
- AWS root password
- `AWS_SECRET_ACCESS_KEY`
- Neon password
- JWT secrets

Use **IAM roles** on EC2 or a deployment `.env.production` file on the server only.

---

## AWS resources (deployment team)

| Resource | Recommendation |
|----------|----------------|
| **EC2** | Ubuntu 22.04, **t3.large** (10 users) or **c5.2xlarge** (50 users + heavy PDF) |
| **Disk** | 50 GB+ |
| **Elastic IP** | Fixed public IP for EXE/web |
| **Security group** | Inbound: **22** (SSH), **9686** (API), **8080** (web). Prefer **443** with nginx + SSL later |
| **S3** | `autoballoon-storage-smorx-prod` (us-east-1) — `https://autoballoon-storage-smorx-prod.s3.us-east-1.amazonaws.com/` |
| **Neon** | Existing Postgres URL |
| **IAM role** | EC2 instance role with `s3:PutObject`, `s3:GetObject` on the bucket. Leave `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` blank so boto3 uses this role. |

---

## Step 1 — Deploy API + Web with Docker (EC2)

### On the EC2 server

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
# log out and back in

git clone https://github.com/deploymentsmorx/Auto-Balloon-IE.git
cd Auto-Balloon-IE

cp deploy/.env.production.example .env.production
nano .env.production   # fill Neon, JWT, S3, CORS, VITE_API_URL
```

**`.env.production` example (replace YOUR_EC2_IP):**

```env
DATABASE_URL=postgresql://...
JWT_SECRET=...
PWD_PEPPER=...
AWS_S3_BUCKET=autoballoon-storage-smorx-prod
AWS_REGION=us-east-1
AWS_S3_URL=https://autoballoon-storage-smorx-prod.s3.us-east-1.amazonaws.com
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
CORS_ORIGINS=http://YOUR_EC2_IP:8080
VITE_API_URL=http://YOUR_EC2_IP:9686
UVICORN_WORKERS=2
```

### Start

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
docker compose -f docker-compose.prod.yml ps
```

### Verify

```bash
curl http://localhost:9686/api/health
# Expect: "db":"neon", "storage":"s3", "s3_url":"https://autoballoon-storage-smorx-prod.s3.us-east-1.amazonaws.com"
curl -I http://localhost:8080
```

From your PC:
- Web: `http://YOUR_EC2_IP:8080`
- API: `http://YOUR_EC2_IP:9686/api/health`

---

## Step 2 — Build customer EXE (Windows machine)

EXE is **not** built inside Linux Docker. Build on Windows:

1. Edit `frontend/.env.production`:
   ```env
   VITE_API_URL=http://YOUR_EC2_IP:9686
   ```
   (Use `https://api.yourdomain.com` when SSL is ready.)

2. Build:
   ```powershell
   cd frontend
   powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
   ```

3. Ship to customer:
   ```text
   frontend\dist-desktop\AutoBalloon.exe
   ```

4. Customer: Activate → Login → use app (no backend install).

---

## Step 3 — Create users & licenses

1. Login as super admin (web or local dev against same Neon).
2. Admin → create customer, license, approved device, engineer login.
3. Give customer: **EXE + license key + install password + login**.

---

## Scaling (10 vs 50 users)

| Users | EC2 | `UVICORN_WORKERS` |
|-------|-----|-------------------|
| ~10 | t3.large | 2 |
| ~25 | c5.xlarge | 3 |
| ~50 | c5.2xlarge | 4 |

Ballooning is CPU-heavy; one large drawing can take 1–3 minutes. Workers help when **multiple users** run jobs at the same time.

---

## HTTPS (recommended for production)

Put **nginx** or **ALB** in front with Let's Encrypt / ACM:

- `https://api.yourdomain.com` → port 9686
- `https://app.yourdomain.com` → port 8080

Then update:
- `.env.production`: `CORS_ORIGINS`, `VITE_API_URL`
- Rebuild web Docker + rebuild EXE with new HTTPS API URL.

---

## Quick checklist

```
[ ] EC2 running Ubuntu + Docker
[ ] .env.production on server (Neon + S3 + JWT)
[ ] docker compose up — health shows db:neon, storage:s3
[ ] Web opens at :8080
[ ] EXE built with VITE_API_URL = public API
[ ] Customer license created in Admin
[ ] Test: upload → balloon → report PDF download
```

---

## Git repo

https://github.com/deploymentsmorx/Auto-Balloon-IE

Local dev (unchanged): `backend\start-api.bat` + `npm run dev` on port 9835.
