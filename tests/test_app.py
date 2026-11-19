import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from io import BytesIO
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app

class Invoices(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.original=app.DATA
        app.DATA=Path(self.tmp.name)
        app.init()
        app.save_settings({'name':'CreativeCrafts Test','nip':'5260250995','address':'Testowa 1','postal':'00-001','city':'Warszawa','prefix':'TEST','next':17,'production':True})
    def tearDown(self):
        app.DATA=self.original
        self.tmp.cleanup()
    def doc(self):
        return app.prepare_document({'buyer':{'name':'Łukasz Żółć','address':'Długa 1','postal':'00-001','city':'Łódź'},'items':[{'name':'Ramka','quantity':'2','price_brutto':'149.99','tax_rate':23}]})
    def test_rounding_and_vat_groups(self):
        rows,totals=app.calculate([{'name':'A','quantity':3,'price_brutto':'.335','tax_rate':23}, {'name':'B','quantity':1,'price_brutto':'10.80','tax_rate':8}])
        self.assertEqual(rows[0]['gross'],'1.01')
        self.assertEqual(rows[0]['net'],'0.82')
        self.assertEqual(totals,{'gross':'11.81','net':'10.82','vat':'0.99'})
    def test_invalid_amounts_and_unsupported_rates(self):
        for qty,price,rate in [('NaN',1,23),(1,'Infinity',23),(0,1,23),(1,-1,23),(1,1,-1),(1,1,7)]:
            with self.assertRaises(app.Problem):
                app.calculate([{'name':'X','quantity':qty,'price_brutto':price,'tax_rate':rate}])
    def test_duplicate_base_order_and_sequence(self):
        d=self.doc();d.update(base_id=100,order_id=200)
        first=app.issue(d)
        second=app.issue(d)
        self.assertEqual(first,second)
        app.issue(self.doc())
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT number FROM invoices ORDER BY id').fetchall()[0][0],f'TEST/0017/{dt.date.today().year}')
            self.assertEqual(c.execute('SELECT next FROM sequences').fetchone()[0],19)
            pdf=c.execute('SELECT pdf FROM invoices LIMIT 1').fetchone()[0]
            self.assertTrue(pdf.startswith(b'%PDF'))
    def test_pdf_failure_does_not_consume_number(self):
        with patch.object(app,'make_pdf',side_effect=RuntimeError('Disk')):
            with self.assertRaises(RuntimeError):app.issue(self.doc())
        self.assertEqual(app.issue(self.doc()),1)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT number FROM invoices').fetchone()[0],f'TEST/0017/{dt.date.today().year}')
    def test_b2b_prepared_and_preview_available(self):
        d=self.doc();d['buyer']['nip']='5260250995'
        self.assertGreater(app.issue(d),0)
        self.assertTrue(app.make_pdf(d,True).startswith(b'%PDF'))
    def test_preview_does_not_create_record(self):
        app.make_pdf(self.doc(),True)
        with app.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM invoices').fetchone()[0],0)
    def test_backup_contains_committed_pdf_and_no_token(self):
        app.TOKEN='secret-token-never-store'
        app.issue(self.doc())
        with zipfile.ZipFile(BytesIO(app.backup())) as z:
            self.assertIn('faktury.sqlite3',z.namelist())
            self.assertIn('dokumenty/1.pdf',z.namelist())
            self.assertNotIn(app.TOKEN.encode(),b''.join(z.read(n) for n in z.namelist()))
        app.TOKEN=''
    def test_upload_retry_uses_same_document(self):
        d=self.doc();d.update(base_id=100,order_id=200);i=app.issue(d)
        calls=[]
        def fake(method,params):
            calls.append((method,params))
            if method=='getInvoices':return {'invoices':[{'invoice_id':100,'order_id':200,'external_invoice_number':''}]}
            if len([x for x in calls if x[0]=='addOrderInvoiceFile'])==1:raise app.Problem('timeout')
            return {'status':'SUCCESS'}
        with patch.object(app,'base',side_effect=fake):
            with self.assertRaises(app.Problem):app.upload_invoice(i)
            app.upload_invoice(i)
        files=[p for m,p in calls if m=='addOrderInvoiceFile']
        self.assertEqual(files[0],files[1])
        with app.connect() as c:self.assertEqual(c.execute('SELECT sent FROM invoices').fetchone()[0],1)
    def test_other_external_invoice_not_overwritten(self):
        d=self.doc();d.update(base_id=100,order_id=200);i=app.issue(d)
        with patch.object(app,'base',return_value={'invoices':[{'invoice_id':100,'order_id':200,'external_invoice_number':'OTHER/1'}]}) as mocked:
            with self.assertRaises(app.Problem):app.upload_invoice(i)
            self.assertEqual(mocked.call_count,1)
    def test_long_pdf(self):
        d=self.doc();d['items']=[dict(d['items'][0],name='Żółta gablotka akrylowa '+str(i)) for i in range(80)]
        pdf=app.make_pdf(d,True)
        self.assertGreater(len(pdf),20000)
    def test_remote_changes_block_upload(self):
        remote={'invoice_id':100,'order_id':200,'items':[{'name':'Ramka'}]}
        d=self.doc();d.update(base_id=100,order_id=200,base_fingerprint=app.fingerprint(remote))
        i=app.issue(d)
        remote['items'][0]['name']='Inny produkt'
        with patch.object(app,'base',return_value={'invoices':[remote]}) as mocked:
            with self.assertRaises(app.Problem):app.upload_invoice(i)
            self.assertEqual(mocked.call_count,1)
    def test_pagination_and_upsert(self):
        batch=[{'order_id':n,'confirmed':True} for n in range(1,101)]
        with patch.object(app,'base',side_effect=[{'orders':batch},{'orders':[{'order_id':101}]}]) as mocked:
            self.assertEqual(app.sync_orders(30)['count'],101)
            self.assertEqual(mocked.call_args_list[1].args[1]['id_from'],101)
        with patch.object(app,'base',return_value={'orders':[{'order_id':1,'confirmed':True,'email':'updated'}]}):
            app.sync_orders(30)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],101)
            self.assertEqual(json.loads(c.execute('SELECT payload FROM orders WHERE id=1').fetchone()[0])['email'],'updated')

if __name__=='__main__':unittest.main()
