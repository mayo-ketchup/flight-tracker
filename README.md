# SJU Flight Tracker

Every Monday at 7am (Puerto Rico time) this checks Google Flights for next month's
**Wednesday → Sunday (4 nights) nonstop** round trips from SJU to:

- **Washington, DC** (DCA, IAD, BWI)
- **New York** (JFK)

It saves the prices, updates a web page with flight details and price-trend charts,
and emails you the 3 cheapest flights for each trip with a link to the page.

About 8–10 SerpApi searches a week (one per Wednesday per trip).

## Files

| File | What it does |
|---|---|
| `config.json` | Trip settings: airports, weekday, nights, nonstop, top N |
| `tracker/tracker.py` | Searches, saves history, writes the page data, sends the email |
| `site/index.html` | The web page |
| `data/history.json` | Saved prices from every Monday (updated automatically) |
| `.github/workflows/weekly.yml` | Runs everything on GitHub every Monday |

## Setup (one time)

1. **Create a GitHub repo.** On github.com, click **New repository**, name it
   `flight-tracker`, and make it **Public** (free GitHub Pages needs a public repo;
   your keys and email stay hidden in Secrets). Don't add a README.

2. **Upload the code.** In this folder:
   ```
   git init -b main
   git add .
   git commit -m "Flight tracker"
   git remote add origin https://github.com/YOUR-USERNAME/flight-tracker.git
   git push -u origin main
   ```

3. **Add your secrets.** Repo → **Settings → Secrets and variables → Actions →
   New repository secret**:

   | Name | Value |
   |---|---|
   | `SERPAPI_KEY` | Your SerpApi API key |
   | `GMAIL_ADDRESS` | The Gmail address that sends the email |
   | `GMAIL_APP_PASSWORD` | Your 16-character Gmail app password |
   | `EMAIL_TO` | *(optional)* Where to send it, if not the same Gmail |

4. **Turn on the page.** Repo → **Settings → Pages → Build and deployment →
   Source: GitHub Actions**.

5. **Run it once now.** Repo → **Actions → Weekly flight check → Run workflow**.
   After about a minute you'll get the email, and the page will be at
   `https://YOUR-USERNAME.github.io/flight-tracker/`.

After that it runs by itself every Monday.

## Preview the page locally (no searches)

```
python tracker/tracker.py --demo
python -m http.server -d site 8000
```
Then open http://localhost:8000.

## Changing things

Edit `config.json`, for example `"nights": 3`, `"top_n": 5`, or add another trip:
```json
{ "id": "orlando", "name": "Orlando", "destination": "MCO" }
```
Each extra trip adds about 4–5 searches a week.

## Notes

- Prices are **round-trip per person**. The page shows the outbound flight;
  you choose the return flight on Google Flights when booking.
- If a date has no nonstops (sometimes true for Washington), it shows fewer than 3.
- GitHub sometimes starts scheduled runs a few minutes late.
