# Quick how to use Nitter scraper 

1. Random HMAC key in `nitter.conf`: 
   - Replace `CHANGE_ME_TO_A_LONG_RANDOM_SECRET`.

2. Fill account credentials in `sessions.jsonl` (JSONL format).

3. Start docker:
```powershell
docker compose up -d
```

4. Check if Nitter is running:
```powershell
docker logs --tail 100 nitter
```

5. Install dependencies (Python):
```powershell
python -m pip install -r requirements.txt
```

6. Run scraper:
```powershell
python scrape_local.py --term github --mode hashtag --number 10
```