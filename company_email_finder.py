import re
import requests
import time
import json
import dns.resolver
import logging
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from functools import lru_cache
from typing import List, Dict, Set

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')

# Common disposable/temporary email domains
DISPOSABLE_DOMAINS = {'tempmail.com', 'guerrillamail.com', 'mailinator.com', '10minutemail.com'}

# Generic/CDN/Service domains that aren't real company emails
GENERIC_DOMAINS = {
    'godaddy.com', 'wix.com', 'wordpress.com', 'squarespace.com',
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com',
    'latofonts.com', 'googlefonts.com', 'typekit.com', 'fontawesome.com',
    'cloudflare.com', 'amazonaws.com', 'azurewebsites.net',
    'example.com', 'test.com', 'localhost'
}

# Common prefixes for non-human emails
INVALID_PREFIXES = {
    'noreply', 'no-reply', 'donotreply', 'do-not-reply',
    'filler', 'test', 'admin', 'postmaster', 'webmaster',
    'support@example', 'info@example', 'sales@example'
}

class EmailFinder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    @lru_cache(maxsize=100)
    def find_company_domain(self, company_name: str, country: str = "") -> str:
        """Enhanced domain finding with multiple strategies"""
        # Strategy 1: DuckDuckGo HTML scraping
        domain = self._search_duckduckgo(company_name, country)
        if domain:
            return domain
        
        # Strategy 2: Direct .com guess
        slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
        return f"{slug}.com"
    
    def _search_duckduckgo(self, company_name: str, country: str) -> str:
        """Scrape DuckDuckGo HTML results"""
        query = f"{company_name} {country} official website".strip()
        url = "https://html.duckduckgo.com/html/"
        try:
            r = self.session.post(url, data={'q': query}, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            # Look for result links
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
        """Extract emails from website with enhanced scraping"""
        base_url = f"https://{domain}" if not domain.startswith('http') else domain
        pages = ['/', '/contact', '/about', '/team', '/staff', '/contatti', '/chi-siamo']
        
        found_emails: Set[str] = set()
        target_domain = domain.replace('https://', '').replace('http://', '').replace('www.', '')
        
        for page in pages:
            url = urljoin(base_url, page)
            
            try:
                r = self.session.get(url, timeout=10, allow_redirects=True)
                r.raise_for_status()
                
                # Parse with BeautifulSoup for better extraction
                soup = BeautifulSoup(r.text, 'html.parser')
                
                # Extract from mailto links
                for mailto in soup.find_all('a', href=re.compile(r'^mailto:')):
                    email = mailto['href'].replace('mailto:', '').split('?')[0]
                    if self._is_valid_email(email):
                        found_emails.add(email.lower())
                
                # Extract from text content
                emails = EMAIL_RE.findall(r.text)
                for email in emails:
                    if self._is_valid_email(email):
                        found_emails.add(email.lower())
                
                time.sleep(1)  # Rate limiting
                
            except Exception as e:
                logger.debug(f"Failed to fetch {url}: {e}")
        
        # Filter to prioritize emails from the target domain
        domain_emails = [e for e in found_emails if target_domain in e]
        
        # If we found emails from the target domain, return only those
        if domain_emails:
            return domain_emails
        
        # Otherwise return all valid emails found
        return list(found_emails)
    
    def _is_valid_email(self, email: str) -> bool:
        """Filter out invalid/unwanted emails with strict validation"""
        email = email.lower().strip()
        
        # Must contain @ and a dot after @
        if '@' not in email or '.' not in email.split('@')[-1]:
            return False
        
        # Filter out anything that looks like a file path or has file extensions
        file_extensions = (
            '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.bmp', '.ico',
            '.pdf', '.doc', '.docx', '.zip', '.css', '.js', '.json', '.xml'
        )
        if any(ext in email for ext in file_extensions):
            return False
        
        # Split email into local and domain parts
        try:
            local, domain = email.split('@')
        except ValueError:
            return False
        
        # Filter disposable domains
        if domain in DISPOSABLE_DOMAINS:
            return False
        
        # Filter generic service domains (Gmail, GoDaddy, etc.)
        if domain in GENERIC_DOMAINS:
            return False
        
        # Filter invalid prefixes
        for prefix in INVALID_PREFIXES:
            if local.startswith(prefix):
                return False
        
        # Email must have reasonable length
        if len(local) < 2 or len(domain) < 4:
            return False
        
        # Domain should have at least one dot
        if '.' not in domain:
            return False
        
        # Filter emails with suspicious patterns
        if any(x in email for x in ['example.', 'test@', 'dummy', 'fake', 'sample']):
            return False
        
        # Local part shouldn't be too generic
        generic_locals = {'info', 'contact', 'admin', 'sales', 'support', 'hello', 'hi'}
        if local in generic_locals:
            # Allow these only if they match the target domain
            # This will be checked later in filtering
            pass
        
        return True
    
    def verify_mx_record(self, email: str) -> bool:
        """Basic MX record verification (faster than SMTP)"""
        domain = email.split('@')[-1]
        try:
            mx_records = dns.resolver.resolve(domain, 'MX', lifetime=5)
            return len(mx_records) > 0
        except Exception:
            return False
    
    def find_company_emails(self, company_name: str, country: str = "", verify: bool = False) -> Dict:
        """Main orchestrator with optional verification"""
        logger.info(f"Searching for: {company_name}")
        
        domain = self.find_company_domain(company_name, country)
        logger.info(f"Target domain: {domain}")
        
        emails = self.find_emails(domain)
        logger.info(f"Found {len(emails)} email(s)")
        
        if verify:
            verified_emails = [e for e in emails if self.verify_mx_record(e)]
            logger.info(f"Verified {len(verified_emails)} email(s)")
            return {
                "company": company_name,
                "domain": domain,
                "emails": verified_emails,
                "all_emails": emails
            }
        
        return {
            "company": company_name,
            "domain": domain,
            "emails": emails
        }

def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python email_finder.py <company name> [country] [--verify]")
        print("Example: python email_finder.py 'Acme Corp' Italy --verify")
        exit(1)
    
    args = sys.argv[1:]
    verify = '--verify' in args
    if verify:
        args.remove('--verify')
    
    company_name = args[0]
    country = args[1] if len(args) > 1 else ""
    
    finder = EmailFinder()
    result = finder.find_company_emails(company_name, country, verify)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()