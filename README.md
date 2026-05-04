# Cian Moran — Demo Investment Portfolio

A live investment management portfolio dashboard tracking global equities, fixed income, infrastructure, commodities and crypto.

## Stack
- **Backend:** Python + Flask + yfinance + gspread
- **Frontend:** HTML/CSS/JS + Chart.js
- **Data:** Google Sheets (trade log) + Yahoo Finance (live prices)
- **Hosting:** Render (backend) + GitHub Pages (frontend)

## Local Development

```bash
# Activate virtual environment
source venv/bin/activate

# Run backend
python app.py
```

Then open `index.html` in your browser.

## Deployment
- Backend → Render (set SHEET_ID and GOOGLE_CREDENTIALS_JSON env vars)
- Frontend → GitHub Pages
