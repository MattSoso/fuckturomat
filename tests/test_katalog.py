from pathlib import Path
import tempfile,unittest,sys,json,datetime as dt,zipfile,threading,urllib.request,urllib.error
from io import BytesIO
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,katalog

class Catalogue(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.prev=app.DATA;app.DATA=Path(self.tmp.name);app.init()
        app.save_settings({'name':'Test','nip':'5260250995','address':'Test 1','postal':'00-001','city':'Warszawa','prefix':'CC','next':7,'production':True})
    def tearDown(self):app.DATA=self.prev;self.tmp.cleanup()
    def doc(self,name='Łukasz Żółć',nip=''):
        return app.prepare_document({'buyer':{'name':name,'nip':nip,'address':'Długa 1','postal':'00-001','city':'Łódź','email':'test@example.com'},'items':[{'name':'Ramka','quantity':1,'price_brutto':123,'tax_rate':23}]})
    def archive(self,iid=1,source='test.fakturownia.pl',kind='vat',income=True,paid='123'):
        data={'id':iid,'number':f'OLD/{iid}/2025','issue_date':'2025-12-01','sell_date':'2025-12-01','kind':kind,'income':income,
              'currency':'PLN','price_gross':'123','price_net':'100','price_tax':'23','paid':paid,'payment_to':'2025-12-01',
              'buyer_name':'Firma Test','buyer_tax_no':'5260250995','buyer_street':'Długa 1','buyer_post_code':'00-001','buyer_city':'Łódź','buyer_email':'test@example.com',
              'seller_name':'Dostawca','seller_tax_no':'123','seller_country':'PL','seller_street':'Test 1','seller_post_code':'00-001','seller_city':'Warszawa',
              'positions':[{'name':'Ramka','quantity':'1','quantity_unit':'szt','price_gross':'123','tax':'23','total_price_gross':'123','total_price_net':'100','total_price_tax':'23'}]}
        with app.connect() as c:c.execute('INSERT INTO ft_invoices VALUES (?,?,?,?,?,?,?,?)',(source,iid,json.dumps(data,ensure_ascii=False),b'%PDF original',b'zip','hash','downloaded','now'))
        return data
    def migrate(self):
        with app.connect() as c:c.execute("DELETE FROM catalogue_migrations WHERE name='clients_v05'")
        app.init()
    def test_migration_preserves_invoices_and_contacts_idempotently(self):
        native=app.issue(self.doc());original=self.archive()
        contact={'name':'Firma Test','tax_no':'526-025-09-95','street':'Długa 1','post_code':'00-001','city':'Łódź','phone':'123456789','email':'test@example.com','country':'PL','bank_account':'original-extra'}
        with app.connect() as c:
            c.execute('INSERT INTO ft_clients VALUES (?,?,?)',('test.fakturownia.pl',1,json.dumps(contact)))
            before=dict(c.execute('SELECT * FROM invoices WHERE id=?',(native,)).fetchone());seq=c.execute('SELECT next FROM sequences').fetchone()[0]
        self.migrate();clients=katalog.client_list(app.connect)['clients'];self.assertEqual(len(clients),2)
        business=next(row for row in clients if row['data']['nip']);self.assertEqual(business['data']['phone'],'123456789')
        business['data']['name']='Zmieniona nazwa';katalog.save_client(app.connect,business,app.valid_nip)
        app.init();app.init();self.assertEqual(katalog.client_list(app.connect)['total'],2)
        self.assertIn('Zmieniona nazwa',[row['data']['name'] for row in katalog.client_list(app.connect)['clients']])
        with app.connect() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM invoices WHERE id=?',(native,)).fetchone()),before)
            self.assertEqual(c.execute('SELECT next FROM sequences').fetchone()[0],seq)
            self.assertEqual(json.loads(c.execute('SELECT payload FROM ft_invoices').fetchone()[0]),original)
            self.assertEqual(json.loads(c.execute('SELECT payload FROM ft_clients').fetchone()[0]),contact)
        result=katalog.invoice_list(app.connect);self.assertEqual(result['total'],2)
        self.assertEqual({(row['kind'],row['id']) for row in result['invoices']},{('local',1),('archive',1)})
    def test_new_invoice_adds_buyer_once_preview_does_not(self):
        d=self.doc('Nowy klient');app.make_pdf(d,True);self.assertEqual(katalog.client_list(app.connect)['total'],0)
        first=app.issue(d);app.issue(self.doc('Nowy klient'));self.assertEqual(katalog.client_list(app.connect)['total'],1)
        with app.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM invoices').fetchone()[0],2)
        with patch.object(app,'make_pdf',side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):app.issue(self.doc('Nie zapisuj mnie'))
        self.assertEqual(katalog.client_list(app.connect)['total'],1)
    def test_manual_contact_update_duplicate_and_stale_edit(self):
        data={'name':'Firma','nip':'526-025-09-95','address':'A1','city':'Łódź','country':'pl','email':'a@example.com','phone':'123'}
        cid=katalog.save_client(app.connect,{'data':data},app.valid_nip)['id'];row=katalog.client_list(app.connect)['clients'][0]
        with self.assertRaises(ValueError):katalog.save_client(app.connect,{'data':data},app.valid_nip)
        row['data']['phone']='456';katalog.save_client(app.connect,row,app.valid_nip)
        with self.assertRaises(ValueError):katalog.save_client(app.connect,row,app.valid_nip)
        self.assertEqual(katalog.client_list(app.connect,'456')['clients'][0]['id'],cid)
        app.issue(self.doc('Inna nazwa na fakturze','5260250995'))
        self.assertEqual(katalog.client_list(app.connect)['total'],1)
        self.assertEqual(katalog.client_list(app.connect)['clients'][0]['data']['phone'],'456')
    def test_same_name_different_people_not_merged(self):
        for iid in (1,2):
            with app.connect() as c:c.execute('INSERT INTO ft_clients VALUES (?,?,?)',('test.fakturownia.pl',iid,json.dumps({'name':'Jan Kowalski'})))
        self.migrate();self.assertEqual(katalog.client_list(app.connect)['total'],2)
    def test_unified_search_payments_costs_corrections_and_sources(self):
        native=app.issue(self.doc());self.archive();self.archive(2,income='false');self.archive(3,kind='correction',paid='0')
        self.assertEqual(katalog.invoice_list(app.connect)['total'],4)
        self.assertEqual(katalog.invoice_list(app.connect,source_filter='local')['total'],1)
        self.assertEqual(katalog.invoice_list(app.connect,source_filter='archive')['total'],3)
        self.assertEqual(katalog.invoice_list(app.connect,query='ŁUKASZ')['total'],1)
        self.assertEqual(katalog.invoice_list(app.connect,document_type='costs')['invoices'][0]['buyer'],'Dostawca')
        self.assertEqual(katalog.invoice_list(app.connect,document_type='corrections')['total'],1)
        self.assertEqual(katalog.invoice_list(app.connect,status='paid')['total'],2)
        details=app.invoice_details('archive','test.fakturownia.pl',1)
        app.payments.save(app.connect,'archive','test.fakturownia.pl',1,details['data'],{'revision':0,'paid':'50','method':'Gotówka','due':'2025-12-01'})
        rows=katalog.invoice_list(app.connect,status='partial')['invoices'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['payment_info']['remaining'],'73.00')
        self.assertEqual(katalog.invoice_list(app.connect,status='overdue')['total'],2)
    def test_pagination_all_records_not_limited_to_100(self):
        for i in range(1,112):self.archive(i)
        pages=[katalog.invoice_list(app.connect,page=p) for p in (1,2,3)]
        self.assertEqual([len(p['invoices']) for p in pages],[50,50,11]);self.assertEqual(len({r['id'] for p in pages for r in p['invoices']}),111)
        for i in range(111):katalog.save_client(app.connect,{'data':{'name':f'Kontrahent {i:03d}'}},app.valid_nip)
        self.assertEqual(katalog.client_list(app.connect,page=3)['total'],111);self.assertEqual(len(katalog.client_list(app.connect,page=3)['clients']),11)
    def test_backup_complete_and_missing_pdf_visible(self):
        self.archive();self.migrate()
        with app.connect() as c:c.execute('UPDATE ft_invoices SET pdf=NULL,attachments=NULL')
        row=katalog.invoice_list(app.connect)['invoices'][0];self.assertFalse(row['has_pdf']);self.assertFalse(row['has_attachments'])
        with zipfile.ZipFile(BytesIO(app.backup())) as z:
            self.assertEqual(len(json.loads(z.read('kontrahenci/lista.json'))),1)
            self.assertIn('fakturownia/test.fakturownia.pl/dokumenty/1.json',z.namelist())
            self.assertIn('faktury.sqlite3',z.namelist())
    def test_offline_archive_has_no_api(self):
        self.assertFalse(hasattr(app.ft,'Client'));self.assertFalse(hasattr(app.ft,'start'))
    def test_http_unified_list_contacts_and_removed_integration(self):
        self.archive();app.issue(self.doc());self.migrate()
        server=app.HTTPServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}';opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        def post(path,data):
            req=urllib.request.Request(url+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','Origin':url,'X-CC-Request':'1'})
            return opener.open(req)
        try:
            opener.open(url+'/start?key='+app.SESSION).read()
            state=json.loads(opener.open(url+'/api/state').read());self.assertEqual(state['invoice_total'],2)
            self.assertEqual(json.loads(opener.open(url+'/api/invoices').read())['total'],2)
            self.assertEqual(json.loads(opener.open(url+'/api/clients').read())['total'],2)
            result=json.loads(post('/api/client/save',{'data':{'name':'Ręczny','phone':'123'}}).read());self.assertGreater(result['id'],0)
            self.assertEqual(json.loads(opener.open(url+'/api/clients?q=123').read())['total'],1)
            self.assertEqual(opener.open(url+'/ft/pdf?source=test.fakturownia.pl&id=1').read(),b'%PDF original')
            for path in ('/api/ft/state','/api/ft/archive'):
                with self.assertRaises(urllib.error.HTTPError) as error:opener.open(url+path)
                self.assertEqual(error.exception.code,404)
            with self.assertRaises(urllib.error.HTTPError) as error:post('/api/ft/start',{'domain':'x','token':'dummy'})
            self.assertEqual(error.exception.code,404)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
