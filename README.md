# RGUHS Result Helper

A small, unofficial web app that requests a result from the RGUHS EMS result portal and displays the returned marks in a mobile-friendly format.

## Important scope and privacy

- Unofficial community tool; not affiliated with RGUHS.
- Currently hard-coded to the `MBBS7A` / `PHASE - I MBBS (RS7)` examination option seen in the October 2026 portal flow. The official portal may change or offer other exam codes. Do not assume it supports every course or exam.
- No database, result history, analytics, or registration-number storage.
- Requires the user to confirm they have permission to check the result.
- Basic in-memory rate limiting is included. It resets when the service restarts and is not a substitute for stronger abuse prevention.
- Public hosting means other people can access the app. Before promoting it broadly, test with consent, confirm the portal's terms permit this use, and consider keeping the app private or adding stronger access controls.
- Do not use it for bulk lookups or scraping. If the official portal is down, the helper cannot create a result that the university has not made available.

## Run locally

Requires Python 3.10+.

```powershell
cd rguhs_result_helper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

## Deploy to the Web

The project includes `render.yaml` and `Procfile` configured for cloud web services.

### Option 1: Deploy on Render (Recommended Free/Low-cost Option)

1. **Commit and Push to GitHub**:
   ```powershell
   git config user.name "Your Name"
   git config user.email "your.email@example.com"
   git add .
   git commit -m "Initial commit for RGUHS Result Helper"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo-name>.git
   git push -u origin main
   ```

2. **Deploy on [Render.com](https://render.com/)**:
   - Sign up / Log in to Render.
   - Click **New +** &rarr; **Web Service**.
   - Connect your GitHub repository.
   - Settings:
     - **Name:** `rguhs-result-helper`
     - **Runtime:** `Python`
     - **Build Command:** `pip install -r requirements.txt`
     - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 300`
     - **Plan:** Free
   - Click **Deploy Web Service**.

> **Note on Free Tiers:** On Render's free tier, the service spins down after 15 minutes of inactivity. The first request after spin-down may take ~40–50 seconds to warm up.

### Option 2: Deploy on Railway or Koyeb
- Push to GitHub as described above.
- In **Railway** or **Koyeb**, click **New Service from GitHub**.
- It will automatically detect `Procfile` or `requirements.txt` and launch the app.

## How lookup works

1. Opens a fresh `requests.Session` to the official RGUHS EMS result page.
2. Sends the registration-number ASP.NET AJAX postback.
3. Selects the `MBBS7A` examination via the dropdown postback.
4. Submits the View postback.
5. Parses the returned result HTML fragment and returns only result fields needed by the UI.
6. Closes the session; response headers prevent caching.

## Troubleshooting

- `503`: official portal is unavailable; retry later.
- `404`: the portal did not return a marksheet for that registration number/exam.
- `502`: upstream response changed or could not be parsed.
- If RGUHS changes its form fields, hidden state, exam code, or response format, this app may need updating.
