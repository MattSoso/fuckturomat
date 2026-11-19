"""Local contact registry and one searchable list of existing invoice snapshots."""
import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal
import rozliczenia as payments

FIELDS=('name','nip','address','postal','city','email','phone','country')

def clean(data):
    result={key:str(data.get(key) or '').strip() for key in FIELDS}
    result['country']=result['country'].upper() or 'PL'
    result['nip']=re.sub(r'[\s-]','',result['nip']).upper()
    return result

def from_archive(data,prefix='buyer'):
    return clean({key:data.get(prefix+'_'+field) for key,field in [('name','name'),('nip','tax_no'),('address','street'),('postal','post_code'),('city','city'),('email','email'),('phone','phone'),('country','country')]})

def from_client(data):
    return clean({key:data.get(field) for key,field in [('name','name'),('nip','tax_no'),('address','street'),('postal','post_code'),('city','city'),('email','email'),('phone','phone'),('country','country')]})

def identity(data,fallback):
    if data['nip']:raw=('nip',data['country'],data['nip'])
    elif data['name'] and data['address'] and data['city']:
        raw=('address',*[re.sub(r'\s+',' ',data[k]).casefold() for k in ('name','address','postal','city','country')])
    elif data['name'] and data['email']:raw=('email',data['name'].casefold(),data['email'].casefold())
    else:raw=('unique',fallback)
    return hashlib.sha256(json.dumps(raw,ensure_ascii=False).encode()).hexdigest()

def add_from_document(c,data,origin,origin_key):
    """Missing fields may be filled, but a newer registry edit never changes a snapshot."""
    if c.execute('SELECT 1 FROM client_origins WHERE origin_key=?',(origin_key,)).fetchone():return
    data=clean(data)
    if not data['name']:return
    key=identity(data,origin_key)
    found=c.execute('SELECT * FROM clients WHERE identity_key=?',(key,)).fetchone()
    now=dt.datetime.now().isoformat()
    if found:
        cid=found['id'];current=json.loads(found['payload']);merged=dict(current)
        for field in FIELDS:
            if not merged.get(field) and data[field]:merged[field]=data[field]
        if merged!=current:
            c.execute('UPDATE clients SET payload=?,revision=revision+1,updated=? WHERE id=?',(json.dumps(merged,ensure_ascii=False),now,cid))
    else:
        cursor=c.execute('INSERT INTO clients(identity_key,payload,origin,revision,updated) VALUES (?,?,?,?,?)',
                         (key,json.dumps(data,ensure_ascii=False),origin,1,now));cid=cursor.lastrowid
    c.execute('INSERT INTO client_origins VALUES (?,?)',(origin_key,cid))

def init(connect):
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS clients (id INTEGER PRIMARY KEY,identity_key TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL,origin TEXT NOT NULL,revision INTEGER NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_origins (origin_key TEXT PRIMARY KEY,client_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS catalogue_migrations (name TEXT PRIMARY KEY);
        ''')
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM catalogue_migrations WHERE name=?',('clients_v05',)).fetchone():return
        for row in c.execute('SELECT * FROM ft_clients').fetchall():
            add_from_document(c,from_client(json.loads(row['payload'])),'archive',f'ft-client:{row["source"]}:{row["id"]}')
        for row in c.execute('SELECT source,id,payload FROM ft_invoices').fetchall():
            data=json.loads(row['payload'])
            if is_cost(data):continue
            add_from_document(c,from_archive(data),'archive',f'archive-invoice:{row["source"]}:{row["id"]}')
        for row in c.execute('SELECT id,payload FROM invoices').fetchall():
            add_from_document(c,json.loads(row['payload'])['buyer'],'invoice',f'local-invoice:{row["id"]}')
        c.execute('INSERT INTO catalogue_migrations VALUES (?)',('clients_v05',))

def client_list(connect,query='',page=1,page_size=50):
    needle=str(query).strip().casefold()[:200]
    with connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM clients ORDER BY id')]
    result=[]
    for row in rows:
        data=json.loads(row.pop('payload'));row.pop('identity_key');row['data']=data
        if not needle or needle in ' '.join(data.values()).casefold():result.append(row)
    result.sort(key=lambda row:(row['data']['name'].casefold(),row['id']))
    pages=max(1,(len(result)+page_size-1)//page_size);page=max(1,min(int(page),pages))
    return {'total':len(result),'page':page,'pages':pages,'clients':result[(page-1)*page_size:page*page_size]}

def save_client(connect,values,valid_nip):
    data=clean(values.get('data',{}))
    if not data['name']:raise ValueError('Podaj nazwę firmy lub imię i nazwisko.')
    if any(len(value)>500 for value in data.values()):raise ValueError('Pole kontrahenta jest zbyt długie (maks. 500 znaków).')
    if not re.fullmatch('[A-Z]{2}',data['country']):raise ValueError('Podaj dwuliterowy kod kraju, np. PL.')
    if data['country']=='PL' and data['nip'] and not valid_nip(data['nip']):raise ValueError('Nieprawidłowy polski NIP kontrahenta.')
    iid=int(values.get('id') or 0);now=dt.datetime.now().isoformat()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM clients WHERE id=?',(iid,)).fetchone() if iid else None
        if iid and not row:raise ValueError('Nie znaleziono kontrahenta.')
        if row and int(values.get('revision',-1))!=row['revision']:raise ValueError('Kontrahent zmienił się w innym oknie. Otwórz edycję ponownie.')
        key=identity(data,f'local:{iid}' if iid else uuid.uuid4().hex)
        duplicate=c.execute('SELECT id FROM clients WHERE identity_key=? AND id<>?',(key,iid)).fetchone()
        if duplicate:raise ValueError('Taki kontrahent już jest na liście. Wyszukaj go i użyj opcji Edytuj.')
        payload=json.dumps(data,ensure_ascii=False)
        if row:c.execute('UPDATE clients SET identity_key=?,payload=?,revision=revision+1,updated=? WHERE id=?',(key,payload,now,iid))
        else:iid=c.execute('INSERT INTO clients(identity_key,payload,origin,revision,updated) VALUES (?,?,?,?,?)',(key,payload,'manual',1,now)).lastrowid
    return {'id':iid}

def is_cost(data):
    return data.get('income') is False or str(data.get('income','')).lower() in ('0','no','false')

def invoice_list(connect,query='',page=1,status='all',source_filter='all',document_type='all',page_size=50):
    if source_filter not in ('all','local','archive') or document_type not in ('all','sales','costs','corrections') or status not in ('all','paid','unpaid','partial','overpayment','overdue'):
        raise ValueError('Nieprawidłowy filtr dokumentów.')
    needle=str(query).strip().casefold()[:200];rows=[]
    with connect() as c:
        c.execute('BEGIN')
        ledger={}
        for change in c.execute('SELECT * FROM payment_changes ORDER BY id'):
            ledger[(change['kind'],change['source'],change['invoice_id'])]=json.loads(change['payload'])
        if source_filter!='archive':
            for row in c.execute('SELECT id,number,order_id,base_id,sent,created,payload FROM invoices'):
                data=json.loads(row['payload']);item=dict(row);item.pop('payload')
                item.update(kind='local',source='',issued=data['issued'],buyer=data['buyer']['name'],nip=data['buyer'].get('nip',''),
                            currency=data.get('currency','PLN'),document_type='corrections' if data.get('correction') else 'sales',
                            document_label='Korekta VAT' if data.get('correction') else 'Faktura VAT',is_cost=False,is_correction=bool(data.get('correction')),has_pdf=True,has_attachments=False,
                            b2b=bool(data['buyer'].get('nip')),ksef_ready=bool(data.get('ksef_number') and data.get('ksef_environment')=='prod'),
                            ksef_status='KSeF: '+data['ksef_environment'] if data.get('ksef_number') else 'Przygotowana do KSeF')
                rows.append((item,data))
        if source_filter!='local':
            for row in c.execute('SELECT source,id,payload,(pdf IS NOT NULL AND length(pdf)>0) AS has_pdf,(attachments IS NOT NULL AND length(attachments)>0) AS has_attachments FROM ft_invoices'):
                data=json.loads(row['payload']);cost=is_cost(data);correction=data.get('kind') in ('correction','correction_note')
                label={'vat':'Faktura VAT','correction':'Korekta VAT','proforma':'Proforma','advance':'Zaliczka','receipt':'Paragon','final':'Faktura końcowa'}.get(data.get('kind'),data.get('kind') or 'Dokument')
                item={'kind':'archive','source':row['source'],'id':row['id'],'number':data.get('number',''),
                      'issued':data.get('issue_date') or '', 'buyer':data.get('seller_name' if cost else 'buyer_name') or '',
                      'nip':data.get('seller_tax_no' if cost else 'buyer_tax_no') or '', 'currency':data.get('currency') or 'PLN',
                      'document_type':'corrections' if correction else 'costs' if cost else 'sales','document_label':label,
                      'is_cost':cost,'is_correction':correction,'has_pdf':bool(row['has_pdf']),'has_attachments':bool(row['has_attachments']),'b2b':False,
                      'order_id':None,'base_id':None,'sent':False}
                rows.append((item,data))
    result=[];status_names={'paid':'Opłacona','unpaid':'Nieopłacona','partial':'Częściowo opłacona','overpayment':'Nadpłata'}
    for item,data in rows:
        if document_type=='costs' and not item['is_cost']:continue
        if document_type=='sales' and item['is_cost']:continue
        if document_type=='corrections' and not item['is_correction']:continue
        if needle and needle not in (' '.join(str(data.get(k) or '') for k in ('buyer_email','seller_email','description'))+' '+str(item['number'])+' '+str(item['buyer'])+' '+str(item['nip'])).casefold():continue
        payment=payments.original(data,item['kind']);change=ledger.get((item['kind'],item['source'],item['id']))
        if change:payment=payments.summary(payment['gross'],change['paid'],change['due'],change['paid_date'],change['method'])
        if status=='overdue' and not payment['overdue']:continue
        if status in status_names and not (payment['status']==status_names[status] or status in ('paid','partial') and payment['status'].startswith(status_names[status]+' —')):continue
        item['payment_info']=payment;result.append(item)
    result.sort(key=lambda row:(str(row['issued']),row['id'],row['kind'],row['source']),reverse=True)
    pages=max(1,(len(result)+page_size-1)//page_size);page=max(1,min(int(page),pages))
    return {'total':len(result),'page':page,'pages':pages,'invoices':result[(page-1)*page_size:page*page_size]}

def export(z,c):
    rows=[]
    for row in c.execute('SELECT id,payload,origin,revision,updated FROM clients'):
        item=dict(row);item['data']=json.loads(item.pop('payload'));rows.append(item)
    z.writestr('kontrahenci/lista.json',json.dumps(rows,ensure_ascii=False))
