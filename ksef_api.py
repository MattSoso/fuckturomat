"""KSeF API 2.0: online FA(3), token authentication, durable single-send queue.

No invoice POST is retried. After an uncertain result only status is queried.
Sources: https://github.com/CIRFMF/ksef-api and official /docs/v2/openapi.json.
"""
import base64, datetime as dt, hashlib, json, os, re, threading, time
import urllib.request, urllib.error, urllib.parse
from cryptography import x509
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lxml import etree as E
import fa3

HOSTS={'test':'https://api-test.ksef.mf.gov.pl/v2','demo':'https://api-demo.ksef.mf.gov.pl/v2','prod':'https://api.ksef.mf.gov.pl/v2'}
class Error(ValueError):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def b64(data):return base64.b64encode(data).decode('ascii')
def digest(data):return b64(hashlib.sha256(data).digest())
def utc(value):return dt.datetime.fromisoformat(value.replace('Z','+00:00'))
def ref(value):
    if not re.fullmatch(r'[A-Za-z0-9-]{1,150}',value):raise Error('Nieprawidłowa referencja KSeF.')
    return value

def encrypted_invoice(xml,key,iv):
    pad=padding.PKCS7(128).padder();plain=pad.update(xml)+pad.finalize()
    enc=Cipher(algorithms.AES(key),modes.CBC(iv)).encryptor();cipher=enc.update(plain)+enc.finalize()
    return {'invoiceHash':digest(xml),'invoiceSize':len(xml),'encryptedInvoiceHash':digest(cipher),
            'encryptedInvoiceSize':len(cipher),'encryptedInvoiceContent':b64(cipher),'offlineMode':False}

class Client:
    def __init__(self,environment,nip,token):
        self.host=HOSTS[environment];self.nip=nip;self.token=token;self.access='';self.until=0
        self.opener=urllib.request.build_opener(NoRedirect())
    def clean(self,message):
        for secret in (self.token,self.access):
            if secret:message=message.replace(secret,'[ukryto]')
        return message[:3000]
    def request(self,method,path,data=None,bearer=None,raw=False,headers=None):
        h={'Accept':'application/xml' if raw else 'application/json','X-Error-Format':'problem-details'}
        if headers:h.update(headers)
        if bearer:h['Authorization']='Bearer '+bearer
        body=json.dumps(data,separators=(',',':')).encode() if data is not None else None
        if body is not None:h['Content-Type']='application/json'
        try:
            with self.opener.open(urllib.request.Request(self.host+path,data=body,headers=h,method=method),timeout=30) as r:
                payload=r.read(6000000)
                if raw:
                    expected=r.headers.get('x-ms-meta-hash')
                    if expected and digest(payload)!=expected:raise Error('Niezgodny skrót pobranego UPO.')
                    E.fromstring(payload,E.XMLParser(resolve_entities=False,no_network=True))
                    return payload
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            # Never serialize arbitrary auth response bodies: some servers echo input.
            raise Error(f'KSeF HTTP {e.code}. Sprawdź środowisko, uprawnienia i dostępność API.') from None
        except (urllib.error.URLError,TimeoutError,OSError):
            raise Error('Brak odpowiedzi KSeF. Wynik wysyłki może być nieznany; nie ponawiaj faktury.') from None
    def certificate(self,usage):
        now=dt.datetime.now(dt.timezone.utc)
        for item in self.request('GET','/security/public-key-certificates'):
            if usage in item['usage'] and utc(item['validFrom'])<=now<utc(item['validTo']):
                cert=x509.load_der_x509_certificate(base64.b64decode(item['certificate']))
                key=cert.public_key()
                if isinstance(key,rsa.RSAPublicKey) and cert.not_valid_before_utc<=now<cert.not_valid_after_utc:
                    return key,item.get('publicKeyId')
        raise Error('Brak aktualnego certyfikatu szyfrującego MF.')
    def rsa_encrypt(self,data,usage):
        key,kid=self.certificate(usage)
        return b64(key.encrypt(data,rsa_padding.OAEP(mgf=rsa_padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))),kid
    def authenticate(self):
        if self.access and time.time()<self.until:return self.access
        challenge=self.request('POST','/auth/challenge')
        encrypted,kid=self.rsa_encrypt((self.token+'|'+str(challenge['timestampMs'])).encode(),'KsefTokenEncryption')
        body={'challenge':challenge['challenge'],'contextIdentifier':{'type':'Nip','value':self.nip},'encryptedToken':encrypted}
        if kid:body['publicKeyId']=kid
        started=self.request('POST','/auth/ksef-token',body);auth=started['authenticationToken']['token']
        for _ in range(20):
            status=self.request('GET','/auth/'+ref(started['referenceNumber']),bearer=auth)['status']
            if status['code']==200:break
            if status['code']!=100:raise Error('Uwierzytelnienie KSeF odrzucone, kod '+str(status['code'])+'. Sprawdź token i uprawnienia.')
            time.sleep(1)
        else:raise Error('Uwierzytelnienie KSeF nadal trwa. Spróbuj sprawdzić połączenie później.')
        tokens=self.request('POST','/auth/token/redeem',bearer=auth)
        self.access=tokens['accessToken']['token'];self.until=utc(tokens['accessToken']['validUntil']).timestamp()-60
        return self.access
    def call(self,method,path,data=None,**kw):return self.request(method,path,data,bearer=self.authenticate(),**kw)
    def open_session(self):
        key=os.urandom(32);iv=os.urandom(16);encrypted,kid=self.rsa_encrypt(key,'SymmetricKeyEncryption')
        encryption={'encryptedSymmetricKey':encrypted,'initializationVector':b64(iv)}
        if kid:encryption['publicKeyId']=kid
        result=self.call('POST','/sessions/online',{'formCode':{'systemCode':'FA (3)','schemaVersion':'1-0E','value':'FA'},'encryption':encryption})
        return ref(result['referenceNumber']),key,iv
    def send(self,session,xml,key,iv):return self.call('POST','/sessions/online/'+ref(session)+'/invoices',encrypted_invoice(xml,key,iv))
    def close(self,session):return self.call('POST','/sessions/online/'+ref(session)+'/close')
    def status(self,session,invoice):return self.call('GET','/sessions/'+ref(session)+'/invoices/'+ref(invoice))
    def find(self,session,xml_hash):
        # One invoice per session; pagination supported for protocol completeness.
        token=None
        for _ in range(100):
            page=self.call('GET','/sessions/'+ref(session)+'/invoices?pageSize=100',headers={'x-continuation-token':token} if token else {})
            matches=[x for x in page['invoices'] if x['invoiceHash']==xml_hash]
            if matches:return matches[0]
            token=page.get('continuationToken')
            if not token:return None
        raise Error('Lista sesji przekroczyła limit. Sprawdź dokument w KSeF.')
    def upo(self,session,invoice):return self.call('GET','/sessions/'+ref(session)+'/invoices/'+ref(invoice)+'/upo',raw=True)

class Service:
    def __init__(self,app,client_factory=Client):
        self.app=app;self.factory=client_factory;self.lock=threading.RLock();self.clients={};self.stop=threading.Event()
    def init(self):
        with self.app.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS ksef_jobs (
              invoice_id INTEGER PRIMARY KEY, environment TEXT NOT NULL, nip TEXT NOT NULL,
              xml_hash TEXT NOT NULL, state TEXT NOT NULL, session_ref TEXT, invoice_ref TEXT,
              message TEXT NOT NULL DEFAULT '', response TEXT, upo BLOB, closed INTEGER NOT NULL DEFAULT 0,
              updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ksef_xml_versions (
              id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL, xml BLOB NOT NULL, created TEXT NOT NULL);
            ''')
    def config(self):
        path=self.app.DATA/'ksef-settings.json'
        if not path.exists():return {'environment':'test','automatic':False,'token':''}
        return json.loads(path.read_text(encoding='utf-8'))
    def public_config(self):
        c=self.config();return {'environment':c['environment'],'automatic':c['automatic'],'token_set':bool(c.get('token'))}
    def save(self,data):
        with self.lock:
            previous=self.config();env=str(data.get('environment','test'))
            if env not in HOSTS:raise Error('Nieprawidłowe środowisko KSeF.')
            token=str(data.get('token','')).strip()
            if not token and env==previous['environment']:token=previous.get('token','')
            if data.get('remove_token'):token=''
            if len(token)>2000 or '\n' in token or '\r' in token:raise Error('Nieprawidłowy token KSeF.')
            automatic=data.get('automatic') is True
            if automatic and not token:raise Error('Najpierw dodaj token dla wybranego środowiska.')
            if automatic and env=='prod' and data.get('production_confirmed') is not True:raise Error('Potwierdź automatyczną wysyłkę do produkcyjnego KSeF.')
            config={'environment':env,'automatic':automatic,'token':token}
            path=self.app.DATA/'ksef-settings.json';temp=path.with_suffix('.tmp')
            fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(config,f)
            os.replace(temp,path);self.clients.clear()
            return self.public_config()
    def client(self,environment,nip):
        config=self.config()
        if config['environment']!=environment or not config.get('token'):raise Error('Ustaw token dla środowiska '+environment+' w ustawieniach KSeF.')
        cache=(environment,nip)
        if cache not in self.clients:self.clients[cache]=self.factory(environment,nip,config['token'])
        return self.clients[cache]
    def connection(self):
        with self.lock:
            nip=re.sub(r'[\s-]','',self.app.settings()['nip'])
            if not self.app.valid_nip(nip):raise Error('Uzupełnij poprawny NIP sprzedawcy.')
            self.client(self.config()['environment'],nip).authenticate()
            return {'connected':True}
    def job(self,i):
        with self.app.connect() as c:r=c.execute('SELECT * FROM ksef_jobs WHERE invoice_id=?',(i,)).fetchone()
        return dict(r) if r else None
    def public_job(self,i):
        r=self.job(i)
        if r:
            r['has_upo']=bool(r.pop('upo'));r.pop('response',None)
        return r
    def update(self,i,**fields):
        fields['updated']=dt.datetime.now().isoformat()
        with self.app.connect() as c:c.execute('UPDATE ksef_jobs SET '+','.join(k+'=?' for k in fields)+' WHERE invoice_id=?',(*fields.values(),i))
    def queue(self,i,automatic=False):
        with self.lock:
            config=self.config()
            if automatic and not config['automatic']:return None
            if not config.get('token'):raise Error('Dodaj token KSeF w ustawieniach.')
            env=config['environment']
            with self.app.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute('SELECT i.payload,k.xml,k.state FROM invoices i JOIN ksef_documents k ON k.invoice_id=i.id WHERE i.id=?',(i,)).fetchone()
                if not r:raise Error('Brak XML faktury.')
                d=json.loads(r['payload'])
                if automatic and not d['buyer'].get('nip'):return None
                if r['state']=='confirmed':raise Error('Ta faktura ma już numer KSeF.')
                if d['issued']!=dt.date.today().isoformat():raise Error('Wysyłka online wymaga dzisiejszej daty faktury. Starsze dokumenty wymagają obsługi właściwego trybu KSeF.')
                parent=d.get('correction',{}).get('environment')
                if parent and parent!=env:raise Error('Korekta wymaga środowiska faktury pierwotnej.')
                # Do not silently change a previously downloaded XML snapshot.
                root=E.fromstring(r['xml'])
                for line in root.findall('.//{'+fa3.NS+'}FaWiersz'):
                    if line.find('{'+fa3.NS+'}P_11') is not None and line.find('{'+fa3.NS+'}P_11A') is not None:
                        raise Error('Starszy XML zawiera jednocześnie netto i brutto. Użyj naprawy XML po sprawdzeniu, że KSeF go nie przyjął.')
                nip=re.sub(r'[\s-]','',d['seller']['nip'])
                if nip!=re.sub(r'[\s-]','',self.app.settings()['nip']):raise Error('NIP faktury różni się od aktualnego sprzedawcy.')
                c.execute('INSERT OR IGNORE INTO ksef_jobs(invoice_id,environment,nip,xml_hash,state,updated) VALUES (?,?,?,?,?,?)',
                          (i,env,nip,digest(r['xml']),'queued',dt.datetime.now().isoformat()))
            return self.public_job(i)
    def enqueue_new(self,c,i,doc,xml):
        config=self.config()
        if not config['automatic'] or not config.get('token') or not doc['buyer'].get('nip'):return
        env=config['environment'];parent=doc.get('correction',{}).get('environment')
        blocked=bool(parent and parent!=env)
        c.execute('INSERT INTO ksef_jobs(invoice_id,environment,nip,xml_hash,state,message,updated) VALUES (?,?,?,?,?,?,?)',
                  (i,env,re.sub(r'[\s-]','',doc['seller']['nip']),digest(xml),'blocked' if blocked else 'queued',
                   'Korekta wymaga środowiska faktury pierwotnej.' if blocked else 'Oczekuje na wysyłkę.',dt.datetime.now().isoformat()))
    def repair(self,i,not_accepted):
        if not_accepted is not True:raise Error('Potwierdź, że XML nie został przyjęty w KSeF.')
        with self.lock,self.app.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT xml,state FROM ksef_documents WHERE invoice_id=?',(i,)).fetchone()
            if not r or r['state']=='confirmed':raise Error('Nie można zmienić XML przyjętej faktury.')
            if c.execute('SELECT 1 FROM ksef_jobs WHERE invoice_id=?',(i,)).fetchone():raise Error('Faktura ma próbę wysyłki API. XML pozostaje zablokowany; sprawdź jej status.')
            xml=repair_xml(r['xml'])
            if xml!=r['xml']:
                c.execute('INSERT INTO ksef_xml_versions(invoice_id,xml,created) VALUES (?,?,?)',(i,r['xml'],dt.datetime.now().isoformat()))
                c.execute('UPDATE ksef_documents SET xml=? WHERE invoice_id=?',(xml,i))
        return {'repaired':True}
    def process(self,i):
        with self.lock:
            job=self.job(i)
            if not job or job['state'] in ('opening','rejected','blocked','complete'):return
            try:
                client=self.client(job['environment'],job['nip'])
                with self.app.connect() as c:r=c.execute('SELECT xml FROM ksef_documents WHERE invoice_id=?',(i,)).fetchone()
                xml=r['xml']
                if digest(xml)!=job['xml_hash']:raise Error('XML zmienił się po zakolejkowaniu. Wysyłka zablokowana.')
                if job['state']=='queued':
                    root=E.fromstring(xml)
                    if root.findtext('.//{'+fa3.NS+'}P_1')!=dt.date.today().isoformat():
                        self.update(i,state='blocked',message='Zmieniła się data. Nie wysłano; wymagany właściwy tryb KSeF.');return
                    # Claim atomically across processes as well as worker threads.
                    with self.app.connect() as c:
                        claimed=c.execute("UPDATE ksef_jobs SET state='opening' WHERE invoice_id=? AND state='queued'",(i,)).rowcount
                    if not claimed:return
                    session,key,iv=client.open_session()
                    # Persist before POST; a crash here is deliberately treated as uncertain.
                    self.update(i,state='sending',session_ref=session,message='Wysyłanie XML do KSeF.')
                    result=client.send(session,xml,key,iv)
                    self.update(i,state='pending',invoice_ref=ref(result['referenceNumber']),message='KSeF przetwarza dokument.')
                    job=self.job(i)
                if not job['invoice_ref']:
                    status=client.find(job['session_ref'],job['xml_hash'])
                    if not status:
                        self.update(i,state='unknown',message='Wynik wysyłki nieznany. Nie wysyłamy ponownie. Sprawdź sesję w KSeF i użyj „Sprawdź status”.');return
                    self.update(i,invoice_ref=ref(status['referenceNumber']),state='pending');job=self.job(i)
                if not job['closed']:
                    try:
                        client.close(job['session_ref']);self.update(i,closed=1)
                    except Error:
                        # A previous close may have succeeded before the local commit.
                        # Status/UPO retrieval remains useful even if close fails.
                        pass
                result=client.status(job['session_ref'],job['invoice_ref'])
                if result['invoiceHash']!=job['xml_hash']:raise Error('KSeF zwrócił inny skrót faktury. Weryfikacja wymagana.')
                status=result['status'];code=status['code']
                message=client.clean(str(code)+': '+status.get('description','')+' '+json.dumps(status.get('details',[]),ensure_ascii=False))
                # Keep business status, not presigned UPO URLs or auth data.
                self.update(i,response=json.dumps({'status':status,'invoiceHash':result['invoiceHash'],'ksefNumber':result.get('ksefNumber')},ensure_ascii=False),message=message)
                if code==200:
                    self.app.confirm_ksef({'id':i,'environment':job['environment'],'number':result['ksefNumber'],'accepted':True,'_api_verified':True})
                    self.update(i,state='accepted')
                    upo=client.upo(job['session_ref'],job['invoice_ref'])
                    self.update(i,state='complete',upo=upo)
                elif code in (100,150):self.update(i,state='pending')
                else:self.update(i,state='rejected')
            except Exception as e:
                current=self.job(i)
                state='unknown' if current['state']=='sending' else ('queued' if current['state']=='opening' else current['state'])
                # Generic errors never include token-containing object representations.
                message=str(e) if isinstance(e,Error) else 'Nie udało się zakończyć obsługi KSeF. Sprawdź status; nie wystawiaj faktury ponownie.'
                self.update(i,state=state,message=message)
    def start(self):
        with self.app.connect() as c:
            c.execute("UPDATE ksef_jobs SET state='blocked',message='Program zamknięto podczas otwierania sesji. Wymagana kontrola; nie ponowiono wysyłki.' WHERE state='opening'")
        def loop():
            while not self.stop.is_set():
                with self.app.connect() as c:
                    ids=[r[0] for r in c.execute("SELECT invoice_id FROM ksef_jobs WHERE state IN ('queued','sending','pending','accepted') ORDER BY updated LIMIT 10")]
                for i in ids:
                    if self.stop.is_set():break
                    self.process(i)
                self.stop.wait(30)
        threading.Thread(target=loop,daemon=True).start()

def repair_xml(xml):
    root=E.fromstring(xml,E.XMLParser(resolve_entities=False,no_network=True))
    changed=False
    for row in root.findall('.//{'+fa3.NS+'}FaWiersz'):
        net=row.find('{'+fa3.NS+'}P_11');gross=row.find('{'+fa3.NS+'}P_11A')
        if net is not None and gross is not None:
            has_net=row.find('{'+fa3.NS+'}P_9A') is not None
            has_gross=row.find('{'+fa3.NS+'}P_9B') is not None
            if has_net==has_gross:raise Error('Nie można jednoznacznie określić sposobu liczenia pozycji.')
            row.remove(gross if has_net else net);changed=True
    if not changed:return xml
    fa3.schema().assertValid(root)
    return E.tostring(root,encoding='UTF-8',xml_declaration=True)
