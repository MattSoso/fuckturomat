from pathlib import Path
import datetime as dt
from io import BytesIO
import json
import sqlite3
import threading
import urllib.request
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import rozliczenia as pay

class Payments(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.prev=app.DATA;app.DATA=Path(self.tmp.name);app.init()
    def tearDown(self):
        app.DATA=self.prev;self.tmp.cleanup()
    def archived(self):
        # Derived from supplied XML; no real buyer/contact details are saved as a test fixture.
        return {'id':71,'number':'71/2026','kind':'vat','issue_date':'2026-09-29','payment_to':'2026-09-29',
                'paid':'284.0','price_gross':'284.0','price_net':'230.9','price_tax':'53.1','currency':'PLN',
                'payment_type':'Wybierz Bank Online','positions':[{'quantity':'1.0','price_gross':'269.0','total_price_gross':'269.0'},
                                                           {'quantity':'1.0','price_gross':'15.01','total_price_gross':'15.0'}]}
    def test_xml_example_fully_paid_and_unknown_date(self):
        p=pay.original(self.archived(),'archive')
        self.assertEqual(p['status'],'Opłacona');self.assertEqual(p['paid'],'284.00')
        self.assertEqual(p['remaining'],'0.00');self.assertEqual(p['method_label'],'Wybierz Bank Online')
        self.assertEqual(p['paid_date'],'');self.assertFalse(p['overdue'])
    def test_partial_overpaid_unpaid_due_today_and_late(self):
        today=dt.date(2026,10,6)
        p=pay.summary(100,20,'2026-10-05',today=today)
        self.assertEqual(p['status'],'Częściowo opłacona');self.assertEqual(p['remaining'],'80.00');self.assertTrue(p['overdue'])
        p=pay.summary(100,110,'2026-10-01',today=today)
        self.assertEqual(p['status'],'Nadpłata');self.assertEqual(p['overpayment'],'10.00');self.assertFalse(p['overdue'])
        self.assertFalse(pay.summary(100,0,'2026-10-06',today=today)['overdue'])
        self.assertEqual(pay.summary(100,0)['status'],'Nieopłacona')
    def test_missing_paid_not_invented_from_source_status(self):
        p=pay.summary(100,None,source_status='paid')
        self.assertEqual(p['paid'],None);self.assertEqual(p['remaining'],None)
        self.assertEqual(p['status'],'Opłacona — status źródła')
    def test_ledger_preserves_archived_invoice_and_history(self):
        d=self.archived();payload=json.dumps(d);pdf=b'%PDF-1.4 original %%EOF'
        with app.connect() as c:c.execute('INSERT INTO ft_invoices VALUES (?,?,?,?,?,?,?,?)',('test.fakturownia.pl',71,payload,pdf,None,'hash','not_requested','now'))
        x=pay.save(app.connect,'archive','test.fakturownia.pl',71,d,{'paid':100,'method':'Gotówka','due':'2026-10-06','paid_date':'2026-10-05','revision':0})
        self.assertEqual(x['current']['remaining'],'184.00');self.assertEqual(x['original']['paid'],'284.00')
        x=pay.save(app.connect,'archive','test.fakturownia.pl',71,d,{'paid':284,'method':'Przelew','revision':x['revision']})
        self.assertEqual(len(x['history']),2)
        with app.connect() as c:
            row=c.execute('SELECT payload,pdf FROM ft_invoices').fetchone();self.assertEqual(row[0],payload);self.assertEqual(row[1],pdf)
        with zipfile.ZipFile(BytesIO(app.backup())) as z:
            self.assertEqual(len(json.loads(z.read('rozliczenia/historia.json'))),2)
    def test_stale_update_rejected(self):
        d=self.archived();pay.save(app.connect,'archive','test.fakturownia.pl',71,d,{'paid':100,'revision':0})
        with self.assertRaises(pay.PaymentProblem):pay.save(app.connect,'archive','test.fakturownia.pl',71,d,{'paid':150,'revision':0})
    def test_invalid_values_and_negative_corrections(self):
        for value in [-1,'NaN','Infinity']:
            with self.assertRaises(pay.PaymentProblem):pay.save(app.connect,'archive','test.fakturownia.pl',71,self.archived(),{'paid':value})
        with self.assertRaises(pay.PaymentProblem):pay.save(app.connect,'archive','test.fakturownia.pl',71,self.archived(),{'paid':0,'paid_date':'2026-10-01'})
        d=self.archived();d['price_gross']='-100'
        with self.assertRaises(pay.PaymentProblem):pay.save(app.connect,'archive','test.fakturownia.pl',71,d,{'paid':100})
    def test_new_document_pdf_payment_fields(self):
        d=app.prepare_document({'buyer':{'name':'Test','email':'test@example.com'},'items':[{'name':'Ramka','quantity':1,'quantity_unit':'szt','price_brutto':284,'tax_rate':23}],
                                'paid':284,'paid_date':'2026-09-29','payment':'Wybierz Bank Online','description':'Odbiór osobisty'})
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("Opcjonalna biblioteka pypdf nie jest zainstalowana.")
        text=''.join(p.extract_text() for p in PdfReader(BytesIO(app.make_pdf(d,True))).pages)
        self.assertIn('Zapłacono: 284.00 PLN',text);self.assertIn('Pozostało do zapłaty: 0.00 PLN',text)
        self.assertIn('Odbiór osobisty',text)
    def test_http_details_save_and_archive_status(self):
        d=self.archived()
        with app.connect() as c:c.execute('INSERT INTO ft_invoices VALUES (?,?,?,?,?,?,?,?)',('test.fakturownia.pl',71,json.dumps(d),b'%PDF original',None,'hash','not_requested','now'))
        server=app.HTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        try:
            opener.open(url+'/start?key='+app.SESSION).read()
            details=json.loads(opener.open(url+'/api/invoice/details?kind=archive&source=test.fakturownia.pl&id=71').read())
            self.assertEqual(details['payment']['current']['status'],'Opłacona')
            req=urllib.request.Request(url+'/api/payment/save',data=json.dumps({'kind':'archive','source':'test.fakturownia.pl','id':71,'revision':0,'paid':'100','method':'Gotówka'}).encode(),headers={'Content-Type':'application/json','Origin':url,'X-CC-Request':'1'})
            response=json.loads(opener.open(req).read());self.assertEqual(response['current']['remaining'],'184.00')
            archive=json.loads(opener.open(url+'/api/invoices').read())
            self.assertEqual(archive['invoices'][0]['payment_info']['status'],'Częściowo opłacona')
            self.assertEqual(opener.open(url+'/ft/pdf?source=test.fakturownia.pl&id=71').read(),b'%PDF original')
        finally:
            server.shutdown();server.server_close();thread.join()
    def test_base_payment_done_read_without_date_invention(self):
        stamp=int(dt.datetime.now().timestamp())
        invoice={'invoice_id':10,'order_id':20,'type':'normal','currency':'PLN','invoice_country_code':'PL',
                 'invoice_fullname':'Test','invoice_address':'Test 1','invoice_postcode':'00-001','invoice_city':'Test',
                 'date_add':stamp,'date_sell':stamp,'items':[{'name':'Ramka','quantity':1,'price_brutto':123,'tax_rate':23}],
                 'total_price_brutto':'123','total_price_netto':'100','payment':'Karta'}
        with patch.object(app,'base',side_effect=[{'invoices':[invoice]},{'orders':[{'order_id':20,'currency':'PLN','payment_done':'123','email':'test@example.com'}]}]):
            d=app.base_document(20)
        self.assertEqual(d['paid'],'123.00');self.assertEqual(d['paid_date'],'')

if __name__=='__main__':unittest.main()
