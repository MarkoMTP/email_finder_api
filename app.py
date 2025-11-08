from flask import Flask, request, jsonify
import re, asyncio, httpx, dns.resolver, logging, sys, os, random, string, smtplib, time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from functools import lru_cache
from typing import List, Dict, Set
from dotenv import load_dotenv

# --- Load environment ---
load_dotenv()

# --- Fix asyncio reuse issue ---
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

app = Flask(__name__)

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)
logger = logging.getLogger(__name__)

# --- Constants ---
EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')

DISPOSABLE_DOMAINS = {'tempmail.com','guerrillamail.com','mailinator.com','10minutemail.com'}
GENERIC_DOMAINS = {
    'godaddy.com','wix.com','wordpress.com','squarespace.com','gmail.com','yahoo.com','hotmail.com',
    'outlook.com','aol.com','latofonts.com','googlefonts.com','typekit.com','fontawesome.com',
    'cloudflare.com','amazonaws.com','azurewebsites.net','example.com','test.com','localhost'
}
INVALID_PREFIXES = {
    'noreply','no-reply','donotreply','do-not-reply','filler','test','admin','postmaster','webmaster',
    'support@example','info@example','sales@example'
}
GENERIC_GUESSES = ("info", "contact", "hello", "office", "sales")

ENABLE_SMTP = True
SMTP_TIMEOUT = 4
SMTP_HELO = "example.com"


class EmailFinder:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    # -------- Domain discovery --------
    @lru_cache(maxsize=100)
    async def find_company_domain(self, company_name: str, country: str = "") -> str:
        domain = await self._search_duckduckgo(company_name, country)
        if domain and self._similar(company_name, domain) > 0.4:
            return domain

        fallback = await self._search_duckduckgo(company_name, "")
        if fallback and self._similar(company_name, fallback) > 0.4:
            return fallback

        slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
        for tld in ['.com', '.it', '.co.uk', '.fr', '.es', '.ch']:
            candidate = f"{slug}{tld}"
            if await self._domain_exists(candidate):
                return candidate
        return f"{slug}.com"

    async def _search_duckduckgo(self, company_name: str, country: str) -> str:
        query = f"{company_name} {country} official website".strip()
        url = "https://html.duckduckgo.com/html/"
        try:
            r = await self.client.post(url, data={'q': query})
            soup = BeautifulSoup(r.text, 'html.parser')
            candidates = []
            for link in soup.find_all('a', class_='result__a'):
                href = link.get('href')
                if href and href.startswith('http'):
                    parsed = urlparse(href)
                    domain = parsed.netloc.replace('www.', '')
                    score = self._similar(company_name, domain)
                    candidates.append((score, domain))
            if candidates:
                best = max(candidates, key=lambda x: x[0])
                if best[0] > 0.35:
                    logger.info(f"[DuckDuckGo] Best match: {best[1]} (score={best[0]:.2f})")
                    return best[1]
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
        return None

    async def _domain_exists(self, domain: str) -> bool:
        try:
            r = await self.client.head(f"https://{domain}")
            return r.status_code < 500
        except Exception:
            return False

    def _similar(self, name: str, domain: str) -> float:
        import difflib
        cleaned_name = re.sub(r'[^a-z0-9]', '', name.lower())
        cleaned_domain = domain.lower().split('.')[0]
        return difflib.SequenceMatcher(None, cleaned_name, cleaned_domain).ratio()

    # -------- Email scraping --------
    async def find_emails(self, domain: str) -> List[str]:
        base_url = f"https://{domain}" if not domain.startswith('http') else domain
        pages = ['/', '/contact', '/about', '/team', '/staff', '/contatti', '/chi-siamo', '/contacts']
        found_emails: Set[str] = set()
        target_domain = domain.replace('https://','').replace('http://','').replace('www.','')

        async def fetch_page(page):
            url = urljoin(base_url, page)
            try:
                r = await self.client.get(url)
                if r.status_code >= 400:
                    return set()
                soup = BeautifulSoup(r.text, 'html.parser')
                found = set()
                for mailto in soup.find_all('a', href=re.compile(r'^mailto:', re.I)):
                    email = mailto['href'].replace('mailto:', '').split('?')[0]
                    if self._is_valid_email(email):
                        found.add(email.lower())
                for email in EMAIL_RE.findall(r.text):
                    if self._is_valid_email(email):
                        found.add(email.lower())
                await asyncio.sleep(random.uniform(0.05, 0.15))
                return found
            except Exception as e:
                logger.debug(f"Error fetching {url}: {e}")
                return set()

        results = await asyncio.gather(*(fetch_page(p) for p in pages))
        for res in results:
            found_emails.update(res)

        domain_emails = [e for e in found_emails if target_domain in e]
        return domain_emails or list(found_emails)

    def _is_valid_email(self, email: str) -> bool:
        email = email.lower().strip()
        if '@' not in email or '.' not in email.split('@')[-1]:
            return False
        if any(ext in email for ext in ('.jpg','.jpeg','.png','.gif','.svg','.pdf','.doc','.zip','.js')):
            return False
        try:
            local, domain = email.split('@')
        except ValueError:
            return False
        if domain in DISPOSABLE_DOMAINS or domain in GENERIC_DOMAINS:
            return False
        for prefix in INVALID_PREFIXES:
            if local.startswith(prefix):
                return False
        if any(x in email for x in ['user@domain.com','example.','test@','dummy','fake','sample']):
            return False
        return True

    # -------- Orchestrator --------
    async def find_company_emails(self, company_name: str, country: str = "", verify: bool = False) -> Dict:
        logger.info(f"Searching for: {company_name}")
        domain = await self.find_company_domain(company_name, country)
        logger.info(f"Target domain: {domain}")

        emails = []
        try:
            emails = await self.find_emails(domain)
        except Exception as e:
            logger.error(f"find_emails failed for {domain}: {e}")
            emails = []

        logger.info(f"✅ Found {len(emails)} email(s)")
        return {"company": company_name, "domain": domain, "emails": emails, "success": True}


finder = EmailFinder()

@app.route('/find-email', methods=['POST','GET'])
def find_email():
    try:
        logger.info(f"Request received: {request.method} {request.url}")
        if request.method == 'POST':
            if request.is_json:
                data = request.get_json()
            else:
                return jsonify({"success": False, "error": "Unsupported Media Type"}), 415
        else:
            data = request.args.to_dict()

        company_name = data.get('company_name') or data.get('company')
        country = data.get('country', '')
        if not company_name:
            return jsonify({"success": False, "error": "Missing 'company_name'"}), 400

        # Each request uses a fresh event loop safely
        result = asyncio.run(finder.find_company_emails(company_name, country))
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"})


if __name__ == '__main__':
    logger.info("✅ Flask async app starting...")
    app.run(host='0.0.0.0', port=8080, debug=True)
