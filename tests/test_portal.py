"""Integration tests using a temporary SQLite database; no real emails or payments."""
import importlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient

os.environ['DATABASE_PATH']=str(Path(tempfile.mkdtemp())/'helix.db')
os.environ['PRIVATE_UPLOAD_DIR']=str(Path(tempfile.mkdtemp())/'proofs')
os.environ['SECRET_KEY']='test_secret_key_secure_abcdefghijklmnopqrstuvwxyz0123456'
os.environ['ENVIRONMENT']='development'
os.environ['EMAIL_MODE']='console'
os.environ['ADMIN_EMAIL']='admin@helix.example'
os.environ['ADMIN_PASSWORD']='test-administrator-password-unique'
os.environ['BTC_ADDRESS']='bc1qtestonlynotarealaddress000000000000'
os.environ['GIFT_CARD_INSTRUCTIONS']='Use an approved authorized merchant; never share PINs.'
os.environ['GIFT_CARD_ENABLED']='1'
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
main=importlib.import_module('main')
latest={}
def fake_send(email,subject,body):
    code=re.search(r'(\d{6})',body)
    if code: latest[email]=code.group(1)
main.send_email=fake_send


def token(response):
    match=re.search(r'name="csrf"[^>]*value="([^"]+)"',response.text)
    assert match, response.text[:2000]
    return match.group(1)

class PortalTest(unittest.TestCase):
    def setUp(self):
        self.cl=TestClient(main.app,follow_redirects=False)
        r=self.cl.get('/age'); self.assertEqual(r.status_code,200)
        r=self.cl.post('/age',data={'csrf':token(r),'confirm':'adult'}); self.assertEqual(r.status_code,303)

    def signup(self,email='fan@example.com'):
        p=self.cl.get('/register')
        r=self.cl.post('/register',data={'csrf':token(p),'full_name':'Example Fan','email':email,'password':'abc123abc123abc123','age_confirm':'on'})
        self.assertEqual(r.status_code,303, r.text[:500]); self.assertIn('verify/signup',r.headers['location'])
        v=self.cl.get('/verify/signup')
        r=self.cl.post('/verify/signup',data={'csrf':token(v),'code':latest[email]})
        self.assertEqual(r.status_code,303, r.text[:500])
        return r

    def test_all_basic_routes(self):
        for u in ['/','/register','/login','/terms','/content']:
            r=self.cl.get(u)
            self.assertIn(r.status_code,(200,303))
        self.assertIn('HELIX STUDIOS',self.cl.get('/').text)
        self.assertIn('$',self.cl.get('/').text)

    def test_signup_otp_login_duplicate_and_support(self):
        email='member+'+os.urandom(3).hex()+'@example.com'
        self.signup(email)
        r=self.cl.get('/dashboard'); self.assertEqual(r.status_code,200)
        r=self.cl.post('/support',data={'csrf':token(r),'message':'I need help with the membership options'})
        self.assertEqual(r.status_code,303)
        self.assertIn('I need help with the membership options',self.cl.get('/dashboard').text)
        self.cl.post('/logout',data={'csrf':token(self.cl.get('/dashboard'))})
        a=self.cl.get('/age'); self.cl.post('/age',data={'csrf':token(a),'confirm':'adult'})
        p=self.cl.get('/register')
        r=self.cl.post('/register',data={'csrf':token(p),'full_name':'Other Name','email':email.upper(),'password':'abc123abc123abc123','age_confirm':'on'})
        self.assertIn('already has an account',r.text)
        p=self.cl.get('/login')
        r=self.cl.post('/login',data={'csrf':token(p),'email':email,'password':'abc123abc123abc123'})
        self.assertEqual(r.status_code,303)
        v=self.cl.get('/verify/login')
        bad=self.cl.post('/verify/login',data={'csrf':token(v),'code':'000000'})
        self.assertEqual(bad.status_code,200)
        r=self.cl.post('/verify/login',data={'csrf':token(self.cl.get('/verify/login')),'code':latest[email]})
        self.assertEqual(r.status_code,303)
        self.assertEqual(self.cl.get('/dashboard').status_code,200)

    def test_btc_payment_admin_approval_and_private_proof(self):
        email='payer+'+os.urandom(3).hex()+'@example.com'
        self.signup(email)
        p=self.cl.get('/checkout/all-access'); self.assertEqual(p.status_code,200)
        im=Image.new('RGB',(30,30),'white'); buff=io.BytesIO(); im.save(buff,format='PNG')
        r=self.cl.post('/checkout/all-access',data={'csrf':token(p),'method':'bitcoin','reference':'some-blockchain-txid-1234'},files={'proof':('receipt.png',buff.getvalue(),'image/png')})
        self.assertEqual(r.status_code,303, r.text[:400])
        self.assertIn('pending',self.cl.get('/dashboard').text)
        # Login as admin in a fresh browser session.
        admin=TestClient(main.app,follow_redirects=False)
        p=admin.get('/age'); admin.post('/age',data={'csrf':token(p),'confirm':'adult'})
        p=admin.get('/login'); r=admin.post('/login',data={'csrf':token(p),'email':os.environ['ADMIN_EMAIL'],'password':os.environ['ADMIN_PASSWORD']})
        self.assertEqual(r.status_code,303)
        v=admin.get('/verify/login'); r=admin.post('/verify/login',data={'csrf':token(v),'code':latest[os.environ['ADMIN_EMAIL']]})
        self.assertEqual(r.status_code,303)
        page=admin.get('/admin')
        self.assertEqual(page.status_code,200)
        self.assertIn(email,page.text)
        with main.db_session() as c:
            pid=c.execute('SELECT id FROM payment_requests ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertEqual(self.cl.get('/admin/proof/'+str(pid)).status_code,303)
        self.assertEqual(admin.get('/admin/proof/'+str(pid)).status_code,200)
        # Cannot approve without independently verified cleared funds.
        r=admin.post(f'/admin/payments/{pid}',data={'csrf':token(page),'action':'approve'})
        self.assertEqual(r.status_code,303)
        with main.db_session() as c:
            self.assertEqual(c.execute('SELECT status FROM payment_requests WHERE id=?',(pid,)).fetchone()[0],'pending')
        r=admin.post(f'/admin/payments/{pid}',data={'csrf':token(admin.get('/admin')),'action':'approve','verified':'yes','note':'Independently verified cleared BTC transaction'})
        self.assertEqual(r.status_code,303)
        with main.db_session() as c:
            self.assertEqual(c.execute('SELECT status FROM payment_requests WHERE id=?',(pid,)).fetchone()[0],'approved')
        self.assertIn('All Access',self.cl.get('/dashboard').text)

    def test_password_reset_and_secure_admin_routes(self):
        email='reset+'+os.urandom(3).hex()+'@example.com'
        self.signup(email)
        self.assertEqual(self.cl.get('/admin').status_code,303)
        p=self.cl.get('/forgot')
        r=self.cl.post('/forgot',data={'csrf':token(p),'email':email,'new_password':'new-unique-strong-pass-12345'})
        self.assertEqual(r.status_code,303)
        v=self.cl.get('/verify/reset')
        r=self.cl.post('/verify/reset',data={'csrf':token(v),'code':latest[email]})
        self.assertEqual(r.status_code,303)
        self.cl.post('/logout',data={'csrf':token(self.cl.get('/dashboard'))})
        a=self.cl.get('/age'); self.cl.post('/age',data={'csrf':token(a),'confirm':'adult'})
        p=self.cl.get('/login')
        r=self.cl.post('/login',data={'csrf':token(p),'email':email,'password':'new-unique-strong-pass-12345'})
        self.assertEqual(r.status_code,303)
        v=self.cl.get('/verify/login')
        r=self.cl.post('/verify/login',data={'csrf':token(v),'code':latest[email]})
        self.assertEqual(r.status_code,303)

    def test_gift_receipt_codes_not_accepted(self):
        email='gift+'+os.urandom(3).hex()+'@example.com'
        self.signup(email)
        p=self.cl.get('/checkout/the-fan')
        r=self.cl.post('/checkout/the-fan',data={'csrf':token(p),'method':'gift_card','gift_brand':'Example merchant','reference':'1234567890123456'})
        self.assertEqual(r.status_code,200)
        self.assertIn('Do not enter gift-card numbers',r.text)

if __name__=='__main__': unittest.main()
