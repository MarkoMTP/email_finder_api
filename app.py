from flask import Flask, request, jsonify
import re, requests, time, dns.resolver, logging, sys, os, json, difflib, smtplib, random, string, socket
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from functools import lru_cache
from typing import List, Dict, Set, Tuple
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- Logging ---
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
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    # -------- Domain discovery --------
    @lru_cache(maxsize=100)
    def find_company_domain(self, company_name: str, country: str = "") -> str:
        """Find the company's domain using DuckDuckGo and fallback guessing."""
        domain = self._search_duckduckgo(company_name, country)
        if domain and self._similar(company_name, domain) > 0.4:
            return domain

        fallback = self._search_duckduckgo(company_name, "")
        if fallback and self._similar(company_name, fallback) > 0.4:
            return fallback

        # Fallback: guess common TLDs
        slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
        for tld in ['.com', '.it', '.co.uk', '.fr', '.es', '.ch']:
            candidate = f"{slug}{tld}"
            if self._domain_exists(candidate):
                return candidate
        return f"{slug}.com"

    def _search_duckduckgo(self, company_name: str, country: str) -> str:
        query = f"{company_name} {country} official website".strip()
        url = "https://html.duckduckgo.com/html/"
        try:
            r = self.session.post(url, data={'q': query}, timeout=10)
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

    def _similar(self, name: str, domain: str) -> float:
        cleaned_name = re.sub(r'[^a-z0-9]', '', name.lower())
        cleaned_domain = domain.lower().split('.')[0]
        return difflib.SequenceMatcher(None, cleaned_name, cleaned_domain).ratio()

    def _domain_exists(self, domain: str) -> bool:
        try:
            r = self.session.head(f"https://{domain}", timeout=5)
            return r.status_code < 500
        except Exception:
            return False

    # -------- Email scraping --------
    def find_emails(self, domain: str) -> List[str]:
        base_url = f"https://{domain}" if not domain.startswith('http') else domain
        pages = ['/', '/contact', '/about', '/team', '/staff', '/contatti', '/chi-siamo', '/contacts']
        found_emails: Set[str] = set()
        target_domain = domain.replace('https://','').replace('http://','').replace('www.','')

        for page in pages:
            url = urljoin(base_url, page)
            try:
                r = self.session.get(url, timeout=12, allow_redirects=True)
                if r.status_code >= 400:
                    continue
                soup = BeautifulSoup(r.text, 'html.parser')

                for mailto in soup.find_all('a', href=re.compile(r'^mailto:', re.I)):
                    email = mailto['href'].replace('mailto:', '').split('?')[0]
                    if self._is_valid_email(email):
                        found_emails.add(email.lower())

                for email in EMAIL_RE.findall(r.text):
                    if self._is_valid_email(email):
                        found_emails.add(email.lower())

                time.sleep(0.4)
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request failed for {url}: {e}")
                continue
            except Exception as e:
                logger.debug(f"Unexpected error on {url}: {e}")
                continue

        domain_emails = [e for e in found_emails if target_domain in e]
        return domain_emails or list(found_emails)

    def _is_valid_email(self, email: str) -> bool:
        email = email.lower().strip()
        if '@' not in email or '.' not in email.split('@')[-1]:
            return False
        if any(ext in email for ext in (
            '.jpg','.jpeg','.png','.gif','.svg','.webp','.bmp','.ico','.pdf','.doc','.docx',
            '.zip','.css','.js','.json','.xml')):
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
        if len(local) < 2 or len(domain) < 4 or '.' not in domain:
            return False
        if any(x in email for x in ['user@domain.com','example.','test@','dummy','fake','sample','name@domain']):
            return False
        return True

    # -------- Verification (strict) --------
    def _mx_hosts(self, domain: str) -> List[str]:
        try:
            answers = dns.resolver.resolve(domain, 'MX', lifetime=3)
            hosts = [str(r.exchange).rstrip('.') for r in answers]
            return hosts
        except Exception as e:
            logger.info(f"MX lookup failed for {domain}: {e}")
            return []

    def verify_mx_domain(self, domain: str) -> bool:
        return len(self._mx_hosts(domain)) > 0

    def _smtp_rcpt(self, mx_host: str, email: str) -> Tuple[bool, int]:
        try:
            with smtplib.SMTP(mx_host, 25, timeout=SMTP_TIMEOUT) as srv:
                srv.ehlo_or_helo_if_needed()
                try:
                    srv.mail(f"probe@{SMTP_HELO}")
                except smtplib.SMTPResponseException:
                    pass
                code, _ = srv.rcpt(email)
                return (code in (250, 251)), code
        except Exception:
            return (False, -1)

    def smtp_verify(self, email: str) -> Tuple[bool, bool]:
        local, domain = email.split('@', 1)
        mx_hosts = self._mx_hosts(domain)
        if not mx_hosts:
            return (False, False)

        random_local = ''.join(random.choices(string.ascii_lowercase, k=12))
        random_addr = f"{random_local}@{domain}"

        for host in mx_hosts[:2]:
            rand_ok, _ = self._smtp_rcpt(host, random_addr)
            target_ok, code = self._smtp_rcpt(host, email)
            if rand_ok and target_ok:
                logger.info(f"Catch-all detected on {domain} via {host}")
                return (False, True)
            if target_ok:
                logger.info(f"SMTP verified {email} via {host} (code {code})")
                return (True, False)
        return (False, False)

    # -------- Guessing --------
    def guess_possible_emails(self, domain: str, company_name: str) -> List[str]:
        base = domain.replace("https://", "").replace("http://", "").replace("www.", "")
        guessed: Set[str] = set()
        for p in GENERIC_GUESSES:
            guessed.add(f"{p}@{base}")

        filtered = [e for e in guessed if self._is_valid_email(e)]
        if not filtered:
            return []

        if not self.verify_mx_domain(base):
            logger.info(f"No MX for {base}; skipping guesses.")
            return []

        verified: List[str] = []
        is_catch_all = False
        for email in filtered:
            ok, catch_all = self.smtp_verify(email) if ENABLE_SMTP else (True, False)
            if catch_all:
                is_catch_all = True
                break
            if ok:
                verified.append(email)
            time.sleep(0.2)

        if is_catch_all:
            logger.info(f"Catch-all: {base} — suppressing guesses.")
            return []
        return verified

    # -------- Orchestrator --------
    def find_company_emails(self, company_name: str, country: str = "", verify: bool = False) -> Dict:
        logger.info(f"Searching for: {company_name}")
        domain = self.find_company_domain(company_name, country)
        logger.info(f"Target domain: {domain}")

        emails = []
        try:
            emails = self.find_emails(domain)
            if not emails:
                logger.info("No visible emails found — trying guesses...")
                guessed = self.guess_possible_emails(domain, company_name)
                if guessed:
                    emails = guessed
        except Exception as e:
            logger.error(f"find_emails failed for {domain}: {e}")
            emails = []

        logger.info(f"✅ Found {len(emails)} email(s) total")
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

        result = finder.find_company_emails(company_name, country)
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"})


if __name__ == '__main__':
    logger.info("✅ Flask app starting successfully...")
    app.run(host='0.0.0.0', port=8080, debug=True)
