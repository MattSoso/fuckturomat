import sys,tempfile,unittest,json,datetime as dt,base64,copy,zipfile
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,fa3,ksef_api as k
from lxml import etree as E
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

class FakeClient:
    def __init__(self,*args):self.sent=0;self.xml=None;self.timeout=False;self.code=200;self.bad_hash=False;self.upo_fail=False
    def authenticate(self):return 'access'
    def open_session(self):return 'SESSION',b'a'*32,b'b'*16
    def send(self,session,xml,key,iv):
        self.sent+=1;self.xml=xml
        if self.timeout:raise k.Error('Timeout')
        return {'referenceNumber':'INVOICE'}
    def status(self,*args):
        return {'referenceNumber':'INVOICE','invoiceHash':'wrong' if self.bad_hash else k.digest(self.xml),
                'ksefNumber':'5260250995-'+dt.date.today().strftime('%Y%m%d')+'-ABCDEF123456-AA',
                'status':{'code':self.code,'description':'Status','details':['pole P_11']}}
    def find(self,*args):return self.status() if self.xml else None
    def close(self,*args):return {}
    def clean(self,s):return s
    def upo(self,*args):
        if self.upo_fail:raise k.Error('UPO jeszcze niedostępne')
        return b'<?xml version="1.0"?><UPO>test</UPO>'

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_data=app.DATA;self.old_service=app.KSEF
        app.DATA=Path(self.tmp.name);self.client=FakeClient();app.KSEF=k.Service(app,lambda *a:self.client);app.init()
        app.save_settings({'name':'Test','nip':'5260250995','address':'A 1','postal':'00-001','city':'Test','prefix':'CC','next':1,'production':True})
        app.KSEF.save({'environment':'test','token':'SECRET-KSEF','automatic':False})
    def tearDown(self):app.DATA=self.old_data;app.KSEF=self.old_service;self.tmp.cleanup()
    def doc(self,net=True,b2b=True):
        return app.prepare_document({'buyer':{'name':'Test','nip':'5260250995' if b2b else '', 'address':'A 1','postal':'00-001','city':'Test'},'items':[{'name':'Usługa','quantity':'1',('price_netto' if net else 'price_brutto'):'25000','tax_rate':'23'}]})
    def issue(self,**kw):return app.issue(self.doc(**kw))
    def xml(self,i):
        with app.connect() as c:return c.execute('SELECT xml FROM ksef_documents WHERE invoice_id=?',(i,)).fetchone()[0]
    def test_second_worker_cannot_send_claimed_document(self):
        i=self.issue();app.KSEF.queue(i)
        other=k.Service(app,lambda *a:self.client)
        def opening():
            other.process(i)
            return 'SESSION',b'a'*32,b'b'*16
        with patch.object(self.client,'open_session',side_effect=opening):app.KSEF.process(i)
        self.assertEqual(self.client.sent,1);self.assertEqual(app.KSEF.job(i)['state'],'complete')
    def test_http_settings_send_status_and_upo(self):
        import threading,urllib.request
        server=app.HTTPServer(('127.0.0.1',0),app.Handler)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        url=f'http://127.0.0.1:{server.server_port}'
        opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        def post(path,data):
            return json.loads(opener.open(urllib.request.Request(url+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','Origin':url,'X-CC-Request':'1'})).read())
        try:
            opener.open(url+'/start?key='+app.SESSION).read()
            result=post('/api/ksef/settings',{'environment':'test','token':'HTTP-SECRET','automatic':False})
            self.assertTrue(result['token_set']);self.assertNotIn('HTTP-SECRET',json.dumps(result))
            self.assertTrue(post('/api/ksef/connection',{})['connected'])
            i=self.issue();self.assertIn('error',post('/api/ksef/send',{'id':i,'environment':'prod'}))
            self.assertIsNone(app.KSEF.job(i))
            self.assertEqual(post('/api/ksef/send',{'id':i,'environment':'test'})['state'],'queued')
            self.assertIn('error',post('/api/ksef/confirm',{'id':i,'environment':'test','accepted':True,'number':'x','_api_verified':True}))
            app.KSEF.process(i)
            self.assertEqual(opener.open(url+f'/upo/{i}').read(),self.client.upo())
            state=opener.open(url+'/api/state').read();self.assertNotIn(b'HTTP-SECRET',state)
        finally:server.shutdown();server.server_close();t.join()
    def test_net_gross_exclusive_and_totals(self):
        for net in (True,False):
            i=self.issue(net=net);root=E.fromstring(self.xml(i));row=root.find('.//{'+fa3.NS+'}FaWiersz')
            self.assertIsNotNone(row.find('{'+fa3.NS+('}P_11' if net else '}P_11A')))
            self.assertIsNone(row.find('{'+fa3.NS+('}P_11A' if net else '}P_11')))
            self.assertTrue(fa3.schema().validate(root))
        self.assertEqual(app.calculate(self.doc()['items'])[1]['gross'],'30750.00')
    def test_auto_queue_only_new_b2b_atomic(self):
        old=self.issue();app.KSEF.save({'environment':'test','automatic':True})
        b2c=self.issue(b2b=False);new=self.issue()
        self.assertIsNone(app.KSEF.job(old));self.assertIsNone(app.KSEF.job(b2c));self.assertEqual(app.KSEF.job(new)['state'],'queued')
    def test_acceptance_number_upo_and_backup_without_secrets(self):
        i=self.issue();app.KSEF.queue(i);app.KSEF.process(i)
        job=app.KSEF.job(i);self.assertEqual(job['state'],'complete');self.assertEqual(self.client.sent,1)
        self.assertEqual(app.invoice_details('local','',i)['ksef']['state'],'confirmed')
        app.KSEF.process(i);self.assertEqual(self.client.sent,1)
        with zipfile.ZipFile(BytesIO(app.backup())) as z:
            self.assertEqual(z.read(f'ksef/{i}-UPO.xml'),job['upo'])
            for n in z.namelist():self.assertNotIn(b'SECRET-KSEF',z.read(n))
    def test_timeout_and_restart_recovers_without_resend(self):
        i=self.issue();app.KSEF.queue(i);self.client.timeout=True;app.KSEF.process(i)
        self.assertEqual(app.KSEF.job(i)['state'],'unknown')
        app.KSEF=k.Service(app,lambda *a:self.client);app.KSEF.init();app.KSEF.process(i)
        self.assertEqual(self.client.sent,1);self.assertEqual(app.KSEF.job(i)['state'],'complete')
    def test_unknown_missing_invoice_never_reposts(self):
        i=self.issue();app.KSEF.queue(i);app.KSEF.update(i,state='sending',session_ref='SESSION')
        app.KSEF.process(i);app.KSEF.process(i)
        self.assertEqual(app.KSEF.job(i)['state'],'unknown');self.assertEqual(self.client.sent,0)
    def test_rejection_and_duplicate_are_not_acceptance(self):
        for code in (430,440,450):
            i=self.issue();app.KSEF.queue(i);self.client.code=code;app.KSEF.process(i)
            self.assertEqual(app.KSEF.job(i)['state'],'rejected')
            self.assertIn('pole P_11',app.KSEF.job(i)['message'])
            self.assertEqual(app.invoice_details('local','',i)['ksef']['state'],'prepared')
    def test_wrong_hash_does_not_confirm(self):
        i=self.issue();app.KSEF.queue(i);self.client.bad_hash=True;app.KSEF.process(i)
        self.assertEqual(app.invoice_details('local','',i)['ksef']['state'],'prepared')
        self.assertIn('inny skrót',app.KSEF.job(i)['message'])
    def test_pending_then_acceptance_and_delayed_upo(self):
        i=self.issue();app.KSEF.queue(i);self.client.code=150;app.KSEF.process(i)
        self.assertEqual(app.KSEF.job(i)['state'],'pending')
        self.client.code=200;self.client.upo_fail=True;app.KSEF.process(i)
        self.assertEqual(app.KSEF.job(i)['state'],'accepted')
        self.client.upo_fail=False;app.KSEF.process(i)
        self.assertEqual(app.KSEF.job(i)['state'],'complete');self.assertEqual(self.client.sent,1)
    def test_environment_and_confirmation_guards(self):
        with self.assertRaises(k.Error):app.KSEF.save({'environment':'prod','token':'PROD','automatic':True})
        app.KSEF.save({'environment':'demo'});self.assertFalse(app.KSEF.public_config()['token_set'])
        app.KSEF.save({'environment':'test','token':'TEST'})
        i=self.issue();app.KSEF.queue(i)
        with self.assertRaises(app.Problem):app.confirm_ksef({'id':i,'environment':'test','accepted':True,'number':'x'})
        with self.assertRaises(k.Error):app.KSEF.repair(i,True)
    def test_xml_repair_keeps_values_and_original(self):
        i=self.issue();root=E.fromstring(self.xml(i));ns='{'+fa3.NS+'}';row=root.find('.//'+ns+'FaWiersz')
        gross=E.Element(ns+'P_11A');gross.text='30750.00';row.insert(list(row).index(row.find(ns+'P_11'))+1,gross)
        bad=E.tostring(root,encoding='UTF-8',xml_declaration=True)
        with app.connect() as c:c.execute('UPDATE ksef_documents SET xml=? WHERE invoice_id=?',(bad,i))
        with self.assertRaises(k.Error):app.KSEF.queue(i)
        with self.assertRaises(k.Error):app.KSEF.repair(i,False)
        app.KSEF.repair(i,True);fixed=E.fromstring(self.xml(i))
        self.assertIsNone(fixed.find('.//'+ns+'P_11A'));self.assertEqual(fixed.findtext('.//'+ns+'P_15'),'30750.00')
        with app.connect() as c:self.assertEqual(c.execute('SELECT xml FROM ksef_xml_versions').fetchone()[0],bad)
        app.KSEF.queue(i)

class ProtocolTests(unittest.TestCase):
    def test_aes_hash_and_exact_bytes(self):
        xml='<Faktura>Łódź</Faktura>'.encode();key=b'a'*32;iv=b'b'*16
        payload=k.encrypted_invoice(xml,key,iv);cipher=base64.b64decode(payload['encryptedInvoiceContent'])
        decrypt=Cipher(algorithms.AES(key),modes.CBC(iv)).decryptor();raw=decrypt.update(cipher)+decrypt.finalize()
        unpad=padding.PKCS7(128).unpadder();self.assertEqual(unpad.update(raw)+unpad.finalize(),xml)
        self.assertEqual(payload['invoiceSize'],len(xml));self.assertEqual(payload['invoiceHash'],k.digest(xml))
        self.assertEqual(payload['encryptedInvoiceHash'],k.digest(cipher));self.assertFalse(payload['offlineMode'])
    def test_auth_challenge_encryption_and_redeem(self):
        c=k.Client('test','5260250995','TOKEN')
        expires=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat()
        responses=[{'challenge':'CHALLENGE','timestampMs':123}, {'referenceNumber':'AUTH','authenticationToken':{'token':'AUTH-TOKEN'}},
                   {'status':{'code':200}}, {'accessToken':{'token':'ACCESS','validUntil':expires}}]
        with patch.object(c,'request',side_effect=responses) as req,patch.object(c,'rsa_encrypt',return_value=('ENCRYPTED','KEYID')) as encrypt:
            self.assertEqual(c.authenticate(),'ACCESS');self.assertEqual(c.authenticate(),'ACCESS')
            encrypt.assert_called_once_with(b'TOKEN|123','KsefTokenEncryption')
            self.assertEqual(req.call_args_list[1].args[2]['contextIdentifier'],{'type':'Nip','value':'5260250995'})
            self.assertEqual(req.call_count,4);self.assertEqual(req.call_args_list[-1].kwargs['bearer'],'AUTH-TOKEN')
    def test_rsa_oaep_and_certificate_usage(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa,padding as rp
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        now=dt.datetime.now(dt.timezone.utc)
        name=x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME,'test')])
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(1).not_valid_before(now-dt.timedelta(days=1)).not_valid_after(now+dt.timedelta(days=1)).sign(key,hashes.SHA256()))
        info={'certificate':k.b64(cert.public_bytes(serialization.Encoding.DER)),'publicKeyId':'ID','usage':['KsefTokenEncryption'],
              'validFrom':(now-dt.timedelta(days=1)).isoformat(),'validTo':(now+dt.timedelta(days=1)).isoformat()}
        c=k.Client('test','5260250995','TOKEN')
        with patch.object(c,'request',return_value=[info]):
            encrypted,kid=c.rsa_encrypt(b'TOKEN|123','KsefTokenEncryption');self.assertEqual(kid,'ID')
            self.assertEqual(key.decrypt(base64.b64decode(encrypted),rp.OAEP(mgf=rp.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None)),b'TOKEN|123')
            with self.assertRaises(k.Error):c.certificate('SymmetricKeyEncryption')
    def test_no_redirects(self):self.assertIsNone(k.NoRedirect().redirect_request(None,None,None,None,None,None))

if __name__=='__main__':unittest.main()
