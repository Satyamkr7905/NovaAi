# MananAI

A friendly AI coach that teaches **Data Structures and Algorithms** at your pace.

It picks the next problem based on what you already know, gives hints only when you need them, and remembers the mistakes you tend to make — so you actually get better, instead of just grinding questions.
  

---

## Live deployment

- Frontend (Vercel): https://mananai01.vercel.app/
- Backend (Render): https://novaai-2wyy.onrender.com/

---

## What's inside

This repo is two apps that work together:

| Folder | What it is | Tech |
|---|---|---|
| `adaptive_dsa_agent/` | The brain + API | Python, FastAPI, PostgreSQL, Gemini (optional) |
| `adaptive_dsa_frontend/` | The website you use | Next.js, React, Tailwind |

They talk to each other over HTTP. For local development, the API runs on `https://novaai-2wyy.onrender.com/`, and the site runs on `https://mananai01.vercel.app/`.

---

## How it works

1. **You sign up** with your email and a password. We email you a 6-digit code to prove the address is yours.
2. **The tutor picks a question** that's just hard enough — not so easy you're bored, not so hard you give up.
3. **You type an answer.** A scorer checks whether it's right.
4. **If you're stuck**, ask for a hint. Hints start subtle and get more specific. The full solution never shows up on hint 1.
5. **The system learns about you** — which topics are weak, which mistakes you repeat (off-by-one, bad base case, wrong complexity, etc.), and what you've already mastered.
6. **Next question is chosen** with all of that in mind. Over time you stop seeing easy stuff and start seeing problems that target your weak spots.

---

## Get it running

You only need to do this once. Pick **one** of the two paths below.

### Path A — Docker (easiest)

Needs: Docker Desktop.

```bash
# from the repo root
cp adaptive_dsa_agent/.env.example adaptive_dsa_agent/.env   # then edit it (see "Secrets" below)
docker compose up --build
```

Open http://localhost:3000 — done.

### Path B — Run both apps yourself

Needs: Python 3.11+, Node 18+, and either PostgreSQL or you can use SQLite for local dev.

**1. Backend (API)**

```bash
cd adaptive_dsa_agent
python -m venv .venv
.venv\Scripts\activate          # on Windows
# source .venv/bin/activate     # on macOS / Linux
pip install -r requirements.txt

cp .env.example .env            # then edit — see "Secrets" below
uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
```

**2. Frontend (website)**

Open a **second terminal**:

```bash
cd adaptive_dsa_frontend
cp .env.local.example .env.local     # then open it and set NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000
npm install
npm run dev
```

Open http://localhost:3000.

---

## Secrets — the stuff you edit in `.env`

The backend reads `adaptive_dsa_agent/.env`. Only **`JWT_SECRET`** is strictly required to start. Everything else is optional.

```dotenv
# Required
APP_ENV=dev                                 # 'dev' or 'prod'. Use 'prod' for real deployments.
JWT_SECRET=a-long-random-string-32-chars+   # generate: python -c "import secrets; print(secrets.token_hex(32))"
DATABASE_URL=sqlite:///./data/tutor_api.db  # or a postgres:// URL

# Optional — turn on if you want real email OTPs
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx          # 16-char Google App Password, NOT your real Gmail password

# Optional — smarter hints via Google Gemini
GEMINI_API_KEY=

# Optional — Google Sign-In
GOOGLE_CLIENT_ID=your-web-client-id.apps.googleusercontent.com
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

### If you leave Gmail blank

No problem. In `APP_ENV=dev` the API will **show the 6-digit code in the HTTP response** and the website will pop it on screen. Great for local development — you never wait for an email.

### If you want real Gmail email

A regular Gmail password will **not** work. Google needs an "App Password":

1. Turn on 2-Step Verification on your Google Account.
2. Go to https://myaccount.google.com/apppasswords.
3. Create a password, copy the 16-character string, paste it as `GMAIL_APP_PASSWORD`.

---

## The auth flow

- **Sign up**: email + password + name → server sends a 6-digit code → you enter it → account is active, you're logged in.
- **Log in**: email + password → you're in.
- **Forgot your password?** Use "Email me a one-time code instead" on the login page.
- **Google Sign-In**: works if `GOOGLE_CLIENT_ID` is set in `.env` and `NEXT_PUBLIC_GOOGLE_CLIENT_ID` in `.env.local`.

Passwords are stored as **bcrypt hashes** — we never see your plaintext password, and it's never logged.

---

## The main API endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/signup` | Create account (email + password + name), sends OTP |
| `POST` | `/signup/verify` | Finish signup with the 6-digit code — returns JWT |
| `POST` | `/login` | Password sign-in — returns JWT |
| `POST` | `/send-otp` | Magic-link-style OTP (no password) |
| `POST` | `/verify-otp` | Complete magic-link sign-in |
| `POST` | `/auth/google` | Exchange a Google credential for a JWT |
| `GET` | `/questions/next` | Pick the right next question for this user |
| `POST` | `/submit-answer` | Score an answer, update the user's model |
| `GET` | `/questions/{id}/hint?level=N` | Multi-level hints (1 = nudge, 3 = strong) |
| `GET` | `/user/stats` | Streak, accuracy, topic mastery |
| `GET` | `/analytics` | Longer-form progress report |

Full OpenAPI docs are at `http://localhost:8000/docs` once the API is running.

---

## Tests

```bash
cd adaptive_dsa_agent
python -m pytest tests/ -q
```

Should show `33 passed`.

---

## Common troubleshooting

| Symptom | What's wrong | Fix |
|---|---|---|
| Frontend says **"Can't reach the API at http://127.0.0.1:8000"** | API isn't running, or `NEXT_PUBLIC_API_BASE` is wrong | Start `uvicorn`, check the address, restart `npm run dev` |
| Signup responds **"Could not send the sign-in code"** | Gmail rejected SMTP login | Use a real Google **App Password**, or blank out `GMAIL_USER` / `GMAIL_APP_PASSWORD` to use the dev inline code |
| Server refuses to start with **"JWT_SECRET is unset or insecure"** | `APP_ENV=prod` with a weak secret | Generate one: `python -c "import secrets; print(secrets.token_hex(32))"` |
| Login says **"Please verify your email"** | You signed up but never used the 6-digit code | Go to `/verify-otp?mode=signup` or sign up again — the OTP will be re-sent |
| First page load is slow | Next.js dev mode compiles on first hit | Normal. Run `npm run build && npm start` for an instant boot |

---

## Security — what we did

- Passwords: **bcrypt**, 12 rounds.
- OTPs: hashed with a server pepper, compared in **constant time**, invalidated after a few wrong tries.
- Rate limiting: per-email cooldown + daily cap on OTP sends.
- JWTs: HS256, short lifetime, refuses to start in `prod` with a weak secret.
- Headers: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `COOP`, `Permissions-Policy`.
- CORS: explicit method + header allow-list when `allow_credentials=True`.
- Google tokens: verifies `iss` and `email_verified` before trusting them.

---

## Project layout

```
MananAI/
├── docker-compose.yml          # one-shot "full stack" for Docker users
├── README.md                   # this file
│
├── adaptive_dsa_agent/         # the Python/FastAPI backend
│   ├── server/                 # HTTP API (auth, tutor routes)
│   ├── app/                    # learning brain (decision engine, hint generator, selector)
│   ├── data/                   # questions.json, user progress
│   ├── prompts/                # Gemini prompt templates
│   ├── tests/                  # pytest suite
│   └── .env.example
│
└── adaptive_dsa_frontend/      # the Next.js website
    ├── src/pages/              # login, signup, verify-otp, dashboard, practice, …
    ├── src/services/           # api.js, auth.js, userProgress.js
    ├── src/context/            # AuthContext, ThemeContext
    └── .env.local.example
```

---

## License

For learning and evaluation. Not audited for production use.
