from flask import Flask, request, jsonify
import re, requests, time, dns.resolver, logging, sys
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from functools import lru_cache
from typing import List, Dict, Set

app = Flask(__name__)

# --- Logging setup for Render ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)
logger = logging.getLogger(__name__)

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

class EmailFinder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    @lru_cache(maxsize=100)
    def find_company_domain(self, company_name: str, country: str = "") -> str:
        domain = self._search_duckduckgo(company_name, country)
        if domain:
            return domain
        slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
        return f"{slug}.com"
    
    def _search_duckduckgo(self, company_name: str, country: str) -> str:
        query = f"{company_name} {country} official website".strip()
        url = "https://html.duckduckgo.com/html/"
        try:
            r = self.session.post(url, data={'q': query}, timeout=8)
            soup = BeautifulSoup(r.text, 'html.parser')
            for link in soup.find_all('a', class_='result__a'):
                href = link.get('href')
                if href and href.startswith('http'):
                    parsed = urlparse(href)
                    domain = parsed.netloc.replace('www.', '')
                    if domain and '.' in domain:
                        logger.info(f"Found domain: {domain}")
                        return domain
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
        return None
    
    def find_emails(self, domain: str) -> List[str]:
        base_url = f"https://{domain}" if not domain.startswith('http') else domain
        pages = ['/', '/contact', '/about', '/team', '/staff', '/contatti', '/chi-siamo']
        found_emails: Set[str] = set()
        target_domain = domain.replace('https://','').replace('http://','').replace('www.','')

        for page in pages:
            url = urljoin(base_url, page)
            try:
                # Shorter timeout + no redirects = prevents hanging or loops
                r = self.session.get(url, timeout=5, allow_redirects=False)
                if r.status_code not in (200, 301, 302):
                    continue
                soup = BeautifulSoup(r.text, 'html.parser')

                for mailto in soup.find_all('a', href=re.compile(r'^mailto:')):
                    email = mailto['href'].replace('mailto:', '').split('?')[0]
                    if self._is_valid_email(email):
                        found_emails.add(email.lower())

                for email in EMAIL_RE.findall(r.text):
                    if self._is_valid_email(email):
                        found_emails.add(email.lower())

                time.sleep(0.5)

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
        if any(x in email for x in ['example.','test@','dummy','fake','sample']):
            return False
        return True
    
    def verify_mx_record(self, email: str) -> bool:
        domain = email.split('@')[-1]
        try:
            mx_records = dns.resolver.resolve(domain, 'MX', lifetime=4)
            return len(mx_records) > 0
        except Exception:
            return False
    
    def find_company_emails(self, company_name: str, country: str = "", verify: bool = False) -> Dict:
        logger.info(f"Searching for: {company_name}")
        domain = self.find_company_domain(company_name, country)
        logger.info(f"Target domain: {domain}")
        try:
            emails = self.find_emails(domain)
        except Exception as e:
            logger.error(f"find_emails failed for {domain}: {e}")
            emails = []
        logger.info(f"Found {len(emails)} email(s)")
        if verify:
            verified_emails = [e for e in emails if self.verify_mx_record(e)]
            logger.info(f"Verified {len(verified_emails)} email(s)")
            return {"company": company_name, "domain": domain, "emails": verified_emails,
                    "all_emails": emails, "success": True}
        return {"company": company_name, "domain": domain, "emails": emails, "success": True}

finder = EmailFinder()

@app.route('/find-email', methods=['POST','GET'])
def find_email():
    try:
        logger.info(f"Request received: {request.method} {request.url}")

        # Handle GET and POST separately
        if request.method == 'POST':
            if request.is_json:
                data = request.get_json()
            else:
                return jsonify({
                    "success": False,
                    "error": "Unsupported Media Type: POST requests must include 'Content-Type: application/json'"
                }), 415
        else:
            data = request.args.to_dict()

        company_name = data.get('company_name') or data.get('company')
        country = data.get('country', '')
        verify = str(data.get('verify', 'false')).lower() == 'true'

        if not company_name:
            return jsonify({"success": False, "error": "Missing 'company_name'"}), 400

        result = finder.find_company_emails(company_name, country, verify)
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
