import sys, tempfile, unittest, json, copy, datetime as dt
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from io import BytesIO
import zipfile
from lxml import etree as E
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader=None
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,fa3

class FA3(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=app.DATA;app.DATA=Path(self.tmp.name);app.init()
        app.save_settings({'name':'Test & Żółć','nip':'5260250995','address':'Testowa 1','postal':'00-001','city':'Łódź','prefix':'CC','next':1,'production':True})
    def tearDown(self):app.DATA=self.old;self.tmp.cleanup()
    def doc(self,b2b=True):
        return app.prepare_document({'buyer':{'name':'Firma <Test>','nip':'5260250995' if b2b else '', 'address':'Test 2','postal':'00-002','city':'Łódź'},'items':[{'name':'Ramka','quantity':'2','price_brutto':'123','tax_rate':'23','quantity_unit':'szt'}]})
    def stored(self,i):
        with app.connect() as c:return dict(c.execute('SELECT * FROM invoices WHERE id=?',(i,)).fetchone()),dict(c.execute('SELECT * FROM ksef_documents WHERE invoice_id=?',(i,)).fetchone())
    def xml(self,i):return E.fromstring(self.stored(i)[1]['xml'])
    def values(self,i,key):return self.xml(i).xpath('//*[local-name()="'+key+'"] /text()')
    def confirm(self,i,env='prod'):
        return app.confirm_ksef({'id':i,'environment':env,'number':'5260250995-'+dt.date.today().strftime('%Y%m%d')+'-ABCDEF123456-AA','accepted':True})
    def correction(self,i,b2b=False):
        x=app.correction_source({'kind':'local','id':i});x.update(reason='Zwrot',original_outside_ksef=True)
        x['items']=copy.deepcopy(x['items']);x['items'][0]['quantity']='1'
        return x
    def test_b2b_xml_stable_payment_escaping(self):
        d=self.doc();d['paid']='100';d['paid_date']=d['issued'];i=app.issue(d)
        self.assertEqual(self.values(i,'P_15'),['246.00']);self.assertEqual(self.values(i,'P_14_1'),['46.00'])
        self.assertEqual(self.values(i,'ZnacznikZaplatyCzesciowej'),['1'])
        self.assertEqual(self.values(i,'Nazwa'),['Test & Żółć','Firma <Test>'])
        old=self.stored(i)[1]['xml'];self.confirm(i);self.assertEqual(old,self.stored(i)[1]['xml'])
        row,_=self.stored(i);doc=json.loads(row['payload']);self.assertIn('/invoice/5260250995/',doc['ksef_qr'])
        if PdfReader:self.assertIn('ABCDEF123456-AA',''.join(p.extract_text() for p in PdfReader(BytesIO(row['pdf'])).pages))
        with zipfile.ZipFile(BytesIO(app.backup())) as z:self.assertEqual(z.read(f'dokumenty/{i}-FA3.xml'),old)
    def test_four_rates_schema_and_sum(self):
        d=self.doc();d['items'],d['totals']=app.calculate([{'name':str(r),'quantity':1,'price_brutto':123,'tax_rate':r} for r in (23,8,5,0)])
        i=app.issue(d);self.assertEqual(self.values(i,'P_15'),['492.00']);self.assertEqual(self.values(i,'P_13_6_1'),['123.00'])
    def test_b2b_requires_nip_and_base_confirmation(self):
        d=self.doc();d.update(base_id=1,order_id=2);i=app.issue(d)
        with patch.object(app,'base') as base:
            with self.assertRaises(app.Problem):app.upload_invoice(i)
            base.assert_not_called()
            self.confirm(i,'test')
            with self.assertRaises(app.Problem):app.upload_invoice(i)
            base.assert_not_called()
        d=self.doc();d['buyer']['nip']='1234567890'
        with self.assertRaises(app.Problem):app.issue(d)
    def test_xml_failure_rolls_back(self):
        d=self.doc();d['seller']['name']='' # issue copies current settings; instead force failing build
        with patch.object(fa3,'build',side_effect=fa3.XMLProblem('invalid')):
            with self.assertRaises(fa3.XMLProblem):app.issue(d)
        i=app.issue(self.doc());self.assertEqual(json.loads(self.stored(i)[0]['payload'])['number'].split('/')[1],'0001')
    def test_correction_before_after_and_number(self):
        i=app.issue(self.doc(False));old=self.stored(i)[0];x=self.correction(i);ci=app.issue(app.prepare_correction(x));row,_=self.stored(ci)
        self.assertIn('CC-KOR/0001/',row['number']);self.assertEqual(self.values(ci,'P_15'),['-123.00'])
        self.assertEqual(self.values(ci,'StanPrzed'),['1']);self.assertEqual(self.values(ci,'P_8B'),['2','1'])
        self.assertEqual(self.values(ci,'P_14_1'),['-23.00']);self.assertEqual(self.stored(i)[0],old)
        self.assertIsNone(app.invoice_details('local','',ci)['payment']['current']['overpayment'])
        if PdfReader:
            text=''.join(p.extract_text() for p in PdfReader(BytesIO(row['pdf'])).pages)
            self.assertIn('PRZED:',text);self.assertIn('Zwrot',text)
        with self.assertRaises(app.Problem):app.issue(app.prepare_correction(x))
        x=self.correction(i);x['items'][0]['quantity']='0';nextid=app.issue(app.prepare_correction(x))
        self.assertEqual(self.values(nextid,'P_15'),['-123.00']);self.assertEqual(self.values(nextid,'P_8B'),['1','0'])
    def test_positive_correction_vat_change_and_b2b_pending(self):
        i=app.issue(self.doc())
        with self.assertRaises(app.Problem):self.correction(i)
        self.confirm(i);x=self.correction(i);x['items'][0]['quantity']='3';ci=app.issue(app.prepare_correction(x))
        self.assertEqual(self.values(ci,'P_15'),['123.00']);self.assertEqual(self.values(ci,'NrKSeF'),['1'])
        with self.assertRaises(app.Problem):self.correction(i)
        self.confirm(ci);self.assertEqual(self.correction(i)['version'],1)
    def test_confirmation_constraints(self):
        i=app.issue(self.doc())
        for args in ({'number':'bad','accepted':True,'environment':'prod'},{'number':'5260250995-20261006-ABCDEF123456-AA','accepted':False,'environment':'prod'}):
            with self.assertRaises(ValueError if args['accepted'] else app.Problem):app.confirm_ksef(dict(args,id=i))
        self.confirm(i);self.confirm(i)
        with self.assertRaises(app.Problem):self.confirm(i,'test')
    def test_archive_rounding_original_preserved(self):
        today=dt.date.today().isoformat()
        d={'kind':'vat','income':True,'number':'71/2026','issue_date':today,'sell_date':today,'currency':'PLN','seller_tax_no':'5260250995','buyer_name':'Test','buyer_street':'A1','buyer_post_code':'00-001','buyer_city':'Test','price_gross':'284','price_net':'230.9','price_tax':'53.1','positions':[{'name':'A','quantity':'1','quantity_unit':'szt','price_gross':'269','tax':'23','total_price_net':'218.7','total_price_tax':'50.3','total_price_gross':'269'},{'name':'B','quantity':'1','quantity_unit':'szt','price_gross':'15.01','tax':'23','total_price_net':'12.2','total_price_tax':'2.8','total_price_gross':'15'}]}
        with app.connect() as c:c.execute('INSERT INTO ft_invoices(source,id,payload,updated) VALUES (?,?,?,?)',('test.fakturownia.pl',1,json.dumps(d),today))
        x=app.correction_source({'kind':'archive','source':'test.fakturownia.pl','id':1});x.update(reason='Zwrot A',original_outside_ksef=True);x['items'][0]['quantity']='0'
        # copy before rows so caller mutation does not change source (source reloaded inside prepare)
        corrected=app.prepare_correction(x);self.assertEqual(corrected['items'][1]['gross'],'15')
        i=app.issue(corrected);self.assertEqual(self.values(i,'P_15'),['-269.00'])
        with app.connect() as c:self.assertEqual(json.loads(c.execute('SELECT payload FROM ft_invoices').fetchone()[0]),d)

    def test_rate_change_and_environment_consistency(self):
        i=app.issue(self.doc());self.confirm(i,'test');x=self.correction(i)
        x['items'][0]['quantity']='2';x['items'][0]['tax_rate']='8'
        ci=app.issue(app.prepare_correction(x))
        self.assertEqual(self.values(ci,'P_15'),['0.00']);self.assertEqual(self.values(ci,'P_14_1'),['-46.00'])
        self.assertEqual(self.values(ci,'P_14_2'),['18.22'])
        with self.assertRaises(app.Problem):self.confirm(ci,'prod')
        self.confirm(ci,'test')
    def test_http_correction_xml_confirmation(self):
        import threading,urllib.request
        server=app.HTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}';opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        def post(path,data):
            req=urllib.request.Request(url+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','Origin':url,'X-CC-Request':'1'})
            return opener.open(req).read()
        try:
            opener.open(url+'/start?key='+app.SESSION).read()
            i=app.issue(self.doc(False))
            x=json.loads(post('/api/correction/source',{'kind':'local','id':i}));x.update(reason='Zwrot',original_outside_ksef=True);x['items'][0]['quantity']='0'
            self.assertTrue(post('/api/correction/preview',x).startswith(b'%PDF'))
            ci=json.loads(post('/api/correction/issue',x))['id']
            xml=opener.open(url+f'/xml/{ci}').read();self.assertTrue(fa3.schema().validate(E.fromstring(xml)))
            state=json.loads(opener.open(url+'/api/state').read());self.assertEqual(len(state['invoices']),2)
            confirm={'id':ci,'environment':'test','number':'5260250995-'+dt.date.today().strftime('%Y%m%d')+'-ABCDEF123456-AA','accepted':True}
            self.assertTrue(json.loads(post('/api/ksef/confirm',confirm))['confirmed'])
            details=json.loads(opener.open(url+f'/api/invoice/details?kind=local&id={ci}').read())
            self.assertEqual(details['ksef']['state'],'confirmed');self.assertEqual(opener.open(url+f'/xml/{ci}').read(),xml)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
