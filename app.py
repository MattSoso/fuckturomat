#!/usr/bin/env python3
"""Local invoice MVP. Python 3.9+, loopback-only; no scheduled issuance."""
import base64
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from io import BytesIO
import json
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
import zipfile
import archiwum as ft
import katalog
import rozliczenia as payments
import fa3
import ksef_api
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = (Path.home() / 'Library' / 'Application Support' / 'CreativeCraftsFaktury' if sys.platform == 'darwin'
                else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CreativeCraftsFaktury')
DATA = Path(os.environ.get('CC_FAKTURY_DATA', str(DEFAULT_DATA)))
SESSION = secrets.token_urlsafe(32)
TOKEN = ''  # Loaded from a separate local file; excluded from backups.
KSEF = ksef_api.Service(sys.modules[__name__])
CENT = Decimal('0.01')

class Problem(Exception):
    pass

def money(v):
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)

def number(v, label):
    try:
        n = Decimal(str(v).replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise Problem('Nieprawidłowa wartość: ' + label)
    if not n.is_finite():
        raise Problem('Nieprawidłowa wartość: ' + label)
    return n

def calculate(items):
    if not items or len(items) > 300:
        raise Problem('Dokument musi mieć od 1 do 300 pozycji.')
    result = []
    for item in items:
        name = str(item.get('name', '')).strip()
        qty = number(item.get('quantity', 1), 'ilość')
        net_mode='price_netto' in item
        unit = number(item.get('price_netto') if net_mode else item.get('price_brutto', ''), 'cena netto' if net_mode else 'cena brutto')
        rate = number(item.get('tax_rate', 23), 'VAT')
        if not name or len(name) > 500 or qty <= 0 or unit < 0 or rate not in (Decimal(0), Decimal(5), Decimal(8), Decimal(23)):
            raise Problem('Obsługiwane są dodatnie ilości, ceny ≥ 0 i VAT 0/5/8/23%. Zwolnienie i transakcje szczególne wymagają rozszerzenia.')
        if net_mode:
            net=money(unit*qty)
            gross=net+money(net*rate/100)
        else:
            gross = money(unit * qty)
            net = money(gross / (1 + rate / 100))
        quantity_unit=str(item.get('quantity_unit','szt')).strip()[:20] or 'szt'
        result.append({'name': name, 'quantity': str(qty), 'quantity_unit':quantity_unit,'price_brutto': str(unit*(1+rate/100) if net_mode else unit), 'tax_rate': str(rate),
                       'net': str(net), 'vat': str(gross-net), 'gross': str(gross)})
        if net_mode:result[-1]['price_netto']=str(unit)
    totals = {key: str(sum((Decimal(x[key]) for x in result), Decimal(0))) for key in ('net', 'vat', 'gross')}
    return result, totals

def connect():
    c = sqlite3.connect(DATA / 'faktury.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init():
    global TOKEN
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        DATA.chmod(0o700)
    except OSError:
        pass
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sequences (year INTEGER PRIMARY KEY, next INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
          base_id INTEGER UNIQUE, order_id INTEGER UNIQUE, payload TEXT NOT NULL, pdf BLOB NOT NULL,
          sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
        ''')
        defaults = {'name':'', 'nip':'', 'address':'', 'postal':'', 'city':'', 'bank':'', 'prefix':'CC', 'next':1,
                    'production':False}
        c.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (json.dumps(defaults),))
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS ksef_documents (invoice_id INTEGER PRIMARY KEY, xml BLOB NOT NULL,
          state TEXT NOT NULL, number TEXT, environment TEXT, confirmed TEXT);
        CREATE TABLE IF NOT EXISTS ksef_prepared (invoice_id INTEGER PRIMARY KEY, pdf BLOB NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS correction_sequences (year INTEGER PRIMARY KEY, next INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS correction_refs (invoice_id INTEGER PRIMARY KEY, original_key TEXT NOT NULL,
          version INTEGER NOT NULL, UNIQUE(original_key,version));
        ''')
    ft.init(connect)
    payments.init(connect)
    katalog.init(connect)
    KSEF.init()
    token_path=DATA/"base-key.txt"
    TOKEN=token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""

def settings():
    with connect() as c:
        return json.loads(c.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])

def valid_nip(nip):
    n = ''.join(x for x in nip if x.isdigit())
    return len(n)==10 and sum(int(x)*w for x,w in zip(n[:9], [6,5,7,2,3,4,5,6,7]))%11 == int(n[-1])

def save_settings(s):
    previous=settings()
    clean = {k: str(s.get(k, previous.get(k,''))).strip() for k in ('name','nip','address','postal','city','bank','prefix','bank_name','email','phone','person','place')}
    clean['next'] = int(s.get('next',1))
    clean['production'] = s.get('production') is True
    if not clean['prefix'] or len(clean['prefix']) > 8 or not all(x.isascii() and (x.isalnum() or x=='-') for x in clean['prefix']):
        raise Problem('Prefiks: od 1 do 8 liter lub cyfr ASCII, ewentualnie myślnik.')
    if not 1 <= clean['next'] <= 999999:
        raise Problem('Numer początkowy musi wynosić od 1 do 999999.')
    if clean['production'] and (not valid_nip(clean['nip']) or any(not clean[k] for k in ('name','address','postal','city'))):
        raise Problem('Przed wystawianiem uzupełnij dane sprzedawcy i prawidłowy NIP.')
    with connect() as c:
        c.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(clean, ensure_ascii=False),))
    return clean

def base(method, params):
    if not TOKEN:
        raise Problem('Wprowadź token Base w ustawieniach. Token działa do zamknięcia programu.')
    if method not in ('getOrders','getInvoices','addOrderInvoiceFile'):
        raise Problem('Niedozwolona operacja API.')
    req = urllib.request.Request('https://api.baselinker.com/connector.php',
        data=urllib.parse.urlencode({'method':method, 'parameters':json.dumps(params)}).encode(),
        headers={'X-BLToken':TOKEN, 'Content-Type':'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            obj = json.loads(response.read(), parse_float=Decimal)
    except Exception:
        raise Problem('Nie udało się połączyć z Base. Sprawdź internet i token. Przy wysyłce ponów tę samą fakturę — nie wystawiaj kolejnej.')
    if obj.get('status') != 'SUCCESS':
        # Avoid reflecting server messages that might contain submitted credentials.
        raise Problem('Base odrzucił operację. Sprawdź token i uprawnienia w Base.')
    return obj

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)

def sync_orders(days):
    days = int(days)
    if not 1 <= days <= 365:
        raise Problem('Zakres: od 1 do 365 dni.')
    start = int((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)).timestamp())
    cursor = 0
    count = 0
    for _ in range(100):
        batch = base('getOrders', {'date_confirmed_from':start, 'id_from':cursor, 'get_unconfirmed_orders':False}).get('orders',[])
        if not batch:
            break
        with connect() as c:
            for o in batch:
                c.execute('INSERT INTO orders VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                          (int(o['order_id']), dumps(o)))
        count += len(batch)
        nxt = max(int(o['order_id']) for o in batch) + 1
        if nxt <= cursor:
            raise Problem('Base zwrócił powtarzającą się stronę zamówień. Już pobrane dane są zapisane.')
        cursor = nxt
        if len(batch) < 100:
            break
    else:
        raise Problem('Osiągnięto limit 10 000 zamówień. Skróć zakres synchronizacji.')
    return {'count':count}

def font_name():
    if 'CCFont' in pdfmetrics.getRegisteredFontNames():
        return 'CCFont'
    candidates = [ROOT/'fonts'/'DejaVuSans.ttf', Path('/System/Library/Fonts/Supplemental/Arial.ttf'),
                  Path('/Library/Fonts/Arial.ttf'), Path('C:/Windows/Fonts/arial.ttf'),
                  Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    for p in candidates:
        if p.exists():
            pdfmetrics.registerFont(TTFont('CCFont', str(p)))
            return 'CCFont'
    raise Problem('Brak czcionki z polskimi znakami. Sprawdź folder fonts w paczce.')

def make_pdf(doc, preview=False):
    output = BytesIO()
    font = font_name()
    styles = getSampleStyleSheet()
    body = ParagraphStyle('CC', fontName=font, fontSize=9, leading=14, textColor=colors.HexColor('#253549'))
    title = ParagraphStyle('TitleCC', parent=body, fontSize=20, leading=26)
    def p(text, style=body):
        return Paragraph(escape(str(text)).replace('\n','<br/>'), style)
    company = doc['seller']
    buyer = doc['buyer']
    parts = [p('PODGLĄD — NIE JEST FAKTURĄ' if preview else ('Faktura korygująca' if doc.get('correction') else 'Faktura VAT'), title),
             Spacer(1,12), p('Numer: '+doc['number']), p('Data wystawienia: '+doc['issued']),
             p('Data sprzedaży: '+doc['sold']), Spacer(1,15)]
    if doc.get('buyer',{}).get('nip') and not preview:
        parts.append(p('KSeF: '+doc['ksef_number'] if doc.get('ksef_number') else 'PRZYGOTOWANA DO KSeF — NIE POTWIERDZONO PRZYJĘCIA'))
        if doc.get('ksef_environment') in ('test','demo'):
            parts.append(p('ŚRODOWISKO TESTOWE — DOKUMENT TESTOWY'))
    if doc.get('correction'):
        ref=doc['correction']
        parts += [p('Korekta do: '+ref['number']+' z dnia '+ref['issued']),p('Przyczyna: '+ref['reason']),
                  p('Pozycje poniżej: stan PO korekcie. Kwota korekty oznacza różnicę.')]
        for x in doc['before_items']:
            parts.append(p('PRZED: '+x['name']+' | '+x['quantity']+' '+x.get('quantity_unit','szt')+' | cena brutto '+x['price_brutto']+' | VAT '+x['tax_rate']+'% | netto '+x['net']+' | brutto '+x['gross']))
    seller_txt = '\n'.join(x for x in [company['name'], 'NIP: '+company['nip'],company['address'],company['postal']+' '+company['city']] if x)
    buyer_txt = '\n'.join(x for x in [buyer['name'],buyer['address'],buyer['postal']+' '+buyer['city'],buyer.get('nip','')] if x)
    parties = Table([[p('SPRZEDAWCA'),p('NABYWCA')],[p(seller_txt),p(buyer_txt)]],colWidths=[257,257])
    parts += [parties,Spacer(1,20)]
    rows = [[p(x) for x in ('Nazwa usługi / towaru','Ilość','Cena netto','Wartość netto','VAT %','Wartość VAT','Wartość brutto')]]
    for x in doc['items']:
        rows.append([p(x['name']),p(x['quantity']+' '+x.get('quantity_unit','szt')),p(x.get('price_netto',str(money(Decimal(x['price_brutto'])/(1+Decimal(x['tax_rate'])/100))))),p(x['net']),p(x['tax_rate']+'%'),p(x['vat']),p(x['gross'])])
    table = Table(rows,colWidths=[135,40,67,75,35,70,92],repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e8eef6')),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),9),
        ('TOPPADDING',(0,0),(-1,-1),9),('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#dce3ed'))]))
    parts += [table,Spacer(1,14)]
    groups = {}
    for x in doc['items']:
        g = groups.setdefault(x['tax_rate'], {'net':Decimal(0),'vat':Decimal(0),'gross':Decimal(0)})
        for k in g:
            g[k] += Decimal(x[k])
    if doc.get('correction'):
        for rate,values in fa3.groups(doc['before_items']).items():
            g=groups.setdefault(rate,{'net':Decimal(0),'vat':Decimal(0),'gross':Decimal(0)})
            g['net']-=values[0];g['vat']-=values[1];g['gross']-=values[0]+values[1]
    for rate,g in groups.items():
        parts.append(p(f"VAT {rate}%: netto {g['net']:.2f}, podatek {g['vat']:.2f}, brutto {g['gross']:.2f} PLN"))
    parts += [p('Razem netto: '+doc['totals']['net']+' PLN'),p('Razem VAT: '+doc['totals']['vat']+' PLN')]
    parts += [Spacer(1,12),p(('Kwota korekty brutto: ' if doc.get('correction') else 'Razem brutto: ')+doc['totals']['gross']+' PLN', title),
              p('Sposób płatności: '+doc['payment']),p('Termin płatności: '+doc['due'])]
    if company.get('bank'):
        parts.append(p('Rachunek: '+company['bank']))
    if company.get('bank_name'):
        parts.append(p('Bank: '+company['bank_name']))
    payment_info=payments.original(doc,'local')
    if doc.get('correction') and Decimal(doc['totals']['gross'])<0:
        parts.append(p('Zmniejszenie należności / do rozliczenia zwrotu: '+str(-Decimal(doc['totals']['gross']))+' PLN'))
    if Decimal(doc['totals']['gross'])>=0:
        parts += [p('Zapłacono: '+payment_info['paid']+' PLN'),p('Pozostało do zapłaty: '+payment_info['remaining']+' PLN')]
    if payment_info['overpayment'] is not None and Decimal(payment_info['overpayment'])>0:
        parts.append(p('Nadpłata: '+payment_info['overpayment']+' PLN'))
    if doc.get('paid_date'):
        parts.append(p('Data zapłaty: '+doc['paid_date']))
    if company.get('place'):
        parts.append(p('Miejsce wystawienia: '+company['place']))
    if doc.get('description'):
        parts += [Spacer(1,10),p('Uwagi: '+doc['description'])]
    if company.get('email') or company.get('phone'):
        parts.append(p('Kontakt: '+company.get('email','')+' '+company.get('phone','')))
    if company.get('person'):
        parts.append(p('Wystawił(a): '+company['person']))
    parts.append(Spacer(1,15))
    if doc.get('base_id'):
        parts.append(p('Powiązanie Base: zamówienie '+str(doc['order_id'])+', rekord faktury '+str(doc['base_id'])))
    if doc.get('ksef_qr'):
        widget=QrCodeWidget(doc['ksef_qr']);bounds=widget.getBounds();size=90
        drawing=Drawing(size,size,transform=[size/(bounds[2]-bounds[0]),0,0,size/(bounds[3]-bounds[1]),0,0])
        drawing.add(widget);parts += [drawing,p(doc['ksef_number'])]
    def footer(canvas, pdfdoc):
        canvas.setFont(font,8)
        canvas.drawString(40,25,'CreativeCrafts Faktury • '+doc['number'])
        canvas.drawRightString(A4[0]-40,25,'Strona '+str(pdfdoc.page))
    SimpleDocTemplate(output,pagesize=A4,rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=45).build(parts,onFirstPage=footer,onLaterPages=footer)
    return output.getvalue()

def prepare_document(data):
    items, totals = calculate(data.get('items',[]))
    buyer = {k:str(data.get('buyer',{}).get(k,'')).strip() for k in ('name','nip','address','postal','city','email','phone')}
    if not buyer['name']:
        raise Problem('Uzupełnij nazwę nabywcy.')
    today = dt.date.today().isoformat()
    dates = {k:str(data.get(k,today)) for k in ('issued','sold','due')}
    for date in dates.values():
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            raise Problem('Nieprawidłowa data.')
    paid=payments.amount(data.get('paid','0'))
    if paid<0:
        raise Problem('Kwota zapłaty nie może być ujemna.')
    paid_date=str(data.get('paid_date','')).strip()
    if paid_date:
        dt.date.fromisoformat(paid_date)
        if paid==0:
            raise Problem('Data zapłaty wymaga dodatniej wpłaty.')
    return {'buyer':buyer, 'seller':settings(), 'items':items, 'totals':totals, **dates,
            'paid':str(paid),'paid_date':paid_date,'description':str(data.get('description',''))[:2000],
            'payment':str(data.get('payment','Przelew')), 'number':'PODGLĄD', 'currency':'PLN'}

def base_document(order_id):
    records = base('getInvoices', {'order_id':int(order_id),'get_external_invoices':True,'get_government_data':True}).get('invoices',[])
    if len(records) != 1 or records[0].get('type') != 'normal':
        raise Problem('W Base musi istnieć dokładnie jedna zwykła faktura do tego zamówienia. Korekty nie są jeszcze obsługiwane.')
    inv = records[0]
    if inv.get('external_invoice_number') or inv.get('external_id'):
        raise Problem('Ta faktura ma już numer / powiązanie z zewnętrznym systemem. Nie zastępujemy dokumentów z Fakturowni.')
    if inv.get('gov_id') or inv.get('gov_status'):
        raise Problem('Ta faktura ma status KSeF. Nie zastępujemy dokumentów przesłanych do KSeF.')
    if inv.get('currency') != 'PLN' or inv.get('invoice_country_code') != 'PL':
        raise Problem('Pierwsza wersja obsługuje wyłącznie krajowe faktury w PLN.')
    def date(ts):
        return dt.datetime.fromtimestamp(int(ts)).date().isoformat() if ts else dt.date.today().isoformat()
    doc = prepare_document({'buyer': {'name':inv.get('invoice_company') or inv.get('invoice_fullname',''),
        'nip':inv.get('invoice_nip',''), 'address':inv.get('invoice_address',''), 'postal':inv.get('invoice_postcode',''),
        'city':inv.get('invoice_city','')}, 'items':inv.get('items',[]), 'issued':date(inv.get('date_add')),
        'sold':date(inv.get('date_sell')), 'due':date(inv.get('date_pay_to')), 'payment':inv.get('payment','Przelew')})
    if money(doc['totals']['gross']) != money(inv['total_price_brutto']) or money(doc['totals']['net']) != money(inv['total_price_netto']):
        raise Problem('Sumy po przeliczeniu różnią się od Base. Sprawdź rabaty i metodę zaokrąglania; dokument nie został wystawiony.')
    doc.update(base_id=int(inv['invoice_id']),order_id=int(order_id),base_fingerprint=fingerprint(inv))
    orders=base('getOrders',{'order_id':int(order_id),'get_unconfirmed_orders':False}).get('orders',[])
    if len(orders)!=1 or orders[0].get('currency')!='PLN':
        raise Problem('Nie udało się potwierdzić danych płatności zamówienia w Base.')
    if orders[0].get('payment_done') is not None:
        paid=payments.amount(orders[0]['payment_done'])
        if paid<0:
            raise Problem('Ujemna kwota płatności zamówienia wymaga sprawdzenia.')
        doc['paid']=str(paid)
    doc['buyer']['email']=orders[0].get('email','')
    doc['buyer']['phone']=orders[0].get('phone','')
    return doc

def invoice_total():
    with connect() as c:
        return c.execute('SELECT (SELECT count(*) FROM invoices)+(SELECT count(*) FROM ft_invoices)').fetchone()[0]

def invoice_details(kind,source,invoice_id):
    if kind=='local':
        source=''
        with connect() as c:
            row=c.execute('SELECT payload,number FROM invoices WHERE id=?',(int(invoice_id),)).fetchone()
        if not row:
            raise Problem('Nie znaleziono faktury.')
        data=json.loads(row['payload'])
    elif kind=='archive':
        source=ft.normalize_domain(source)
        data=json.loads(ft.record(source,invoice_id)['payload'])
    else:
        raise Problem('Nieprawidłowy rodzaj dokumentu.')
    ksef=None
    if kind=='local':
        with connect() as c:
            entry=c.execute('SELECT state,number,environment,confirmed FROM ksef_documents WHERE invoice_id=?',(int(invoice_id),)).fetchone()
            ksef=dict(entry) if entry else None
    if ksef:ksef['api']=KSEF.public_job(int(invoice_id))
    return {'kind':kind,'source':source,'id':int(invoice_id),'data':data,'ksef':ksef,
            'payment':payments.get(connect,kind,source,invoice_id,data)}

def correction_source(data):
    kind=data.get('kind','local');source=data.get('source','');iid=int(data['id'])
    details=invoice_details(kind,source,iid);original=details['data']
    if kind=='local':
        if original.get('correction'):raise Problem('Wybierz fakturę pierwotną, nie wcześniejszą korektę.')
        if original['buyer'].get('nip') and not original.get('ksef_number'):
            raise Problem('Najpierw potwierdź przyjęcie faktury pierwotnej w KSeF.')
        doc=json.loads(dumps(original))
    else:
        if original.get('kind')!='vat' or original.get('income') in (False,'false'):
            raise Problem('Korekta obsługuje wyłącznie własne zwykłe faktury sprzedaży VAT.')
        if original.get('currency')!='PLN' or original.get('buyer_country','PL') not in ('PL',None,''):
            raise Problem('Obsługiwane są krajowe faktury w PLN.')
        def party(prefix):
            return {key:str(original.get(prefix+'_'+field) or '') for key,field in [('name','name'),('nip','tax_no'),('address','street'),('postal','post_code'),('city','city'),('email','email'),('phone','phone')]}
        rows=[]
        for position in original.get('positions',[]):
            tax=str(position.get('tax'))
            if tax not in ('0','5','8','23'):raise Problem('Nieobsługiwana stawka VAT faktury pierwotnej.')
            rows.append({key:str(position.get(field) or '0') for key,field in [('name','name'),('quantity','quantity'),('quantity_unit','quantity_unit'),('price_brutto','price_gross'),('tax_rate','tax'),('net','total_price_net'),('vat','total_price_tax'),('gross','total_price_gross')]})
        doc={'buyer':party('buyer'),'seller':party('seller'),'number':original['number'],'issued':original['issue_date'],
             'sold':original.get('sell_date') or original['issue_date'],'items':rows,'currency':'PLN',
             'totals':{key:str(original.get('price_'+field) or 0) for key,field in [('net','net'),('vat','tax'),('gross','gross')]},
             'ksef_number':original.get('ksef_number') or original.get('gov_id') or ''}
    normalize=lambda value:''.join(x for x in value if x.isdigit())
    if normalize(doc['seller']['nip'])!=normalize(settings()['nip']):
        raise Problem('NIP sprzedawcy faktury pierwotnej musi być zgodny z ustawieniami firmy.')
    if not doc['items'] or doc.get('currency','PLN')!='PLN':raise Problem('Brakuje pozycji lub nieobsługiwana waluta.')
    if any(str(x['tax_rate']) not in ('0','5','8','23') for x in doc['items']):raise Problem('Nieobsługiwana stawka VAT.')
    for key in ('net','vat','gross'):
        if money(sum(Decimal(x[key]) for x in doc['items']))!=money(doc['totals'][key]):
            raise Problem('Sumy faktury pierwotnej nie zgadzają się z pozycjami. Sprawdź dokument źródłowy.')
    original_key=f'{kind}:{details["source"]}:{iid}'
    with connect() as c:
        latest=c.execute('SELECT r.version,i.payload FROM correction_refs r JOIN invoices i ON i.id=r.invoice_id WHERE original_key=? ORDER BY version DESC LIMIT 1',(original_key,)).fetchone()
    effective=doc['items'];version=0
    if latest:
        previous=json.loads(latest['payload']);version=latest['version']
        if previous['buyer'].get('nip') and not previous.get('ksef_number'):
            raise Problem('Poprzednia korekta oczekuje na przyjęcie w KSeF.')
        effective=previous['items']
    return {'kind':kind,'source':details['source'],'id':iid,'key':original_key,'version':version,
            'original':doc,'items':effective}

def prepare_correction(data):
    context=correction_source(data);original=context['original'];before=context['items']
    if int(data.get('version',-1))!=context['version']:raise Problem('Stan zmienił się. Otwórz korektę ponownie.')
    reason=str(data.get('reason','')).strip()
    if not reason or len(reason)>256:raise Problem('Podaj przyczynę korekty (do 256 znaków).')
    ksef_number=str(data.get('original_ksef_number') or original.get('ksef_number') or '').strip()
    if original.get('ksef_number') and ksef_number!=original['ksef_number']:
        raise Problem('Nie można zmieniać zapisanego numeru KSeF faktury pierwotnej.')
    if ksef_number:ksef_number=fa3.check_number(ksef_number,original['seller']['nip'])
    elif data.get('original_outside_ksef') is not True:
        raise Problem('Podaj numer KSeF faktury pierwotnej lub potwierdź, że wystawiono ją poza KSeF.')
    incoming=data.get('items',[])
    if len(incoming)!=len(before):raise Problem('Korekta musi zachować układ pozycji. Zerowa ilość oznacza zwrot całej pozycji.')
    after=[]
    for current,old in zip(incoming,before):
        current={key:str(current.get(key,'')).strip() for key in ('name','quantity','quantity_unit','price_brutto','tax_rate')}
        qty=Decimal(current['quantity'])
        if not qty.is_finite() or qty<0:raise Problem('Ilość po korekcie nie może być ujemna.')
        if current=={key:str(old.get(key,'')) for key in current}:
            after.append(dict(old));continue
        entry=dict(current);entry['quantity']='1' if qty==0 else str(qty)
        if 'price_netto' in old and current['price_brutto']==old['price_brutto'] and current['tax_rate']==old['tax_rate']:
            entry['price_netto']=old['price_netto']
        rows,_=calculate([entry]);row=rows[0]
        if qty==0:
            row['quantity']='0'
            for key in ('net','vat','gross'):row[key]='0.00'
        after.append(row)
    totals={key:f'{sum(Decimal(x[key]) for x in after)-sum(Decimal(x[key]) for x in before):.2f}' for key in ('net','vat','gross')}
    today=dt.date.today().isoformat();due=str(data.get('due') or today);dt.date.fromisoformat(due)
    return {'buyer':original['buyer'],'seller':settings(),'number':'PODGLĄD','issued':today,'sold':original['sold'],
            'due':due,'payment':str(data.get('payment') or 'Przelew'),'paid':'0.00','paid_date':'','currency':'PLN',
            'items':after,'before_items':before,'totals':totals,'description':str(data.get('description',''))[:2000],
            'correction':{'key':context['key'],'version':context['version'],'reason':reason,
                          'number':original['number'],'issued':original['issued'],'ksef_number':ksef_number,
                          'environment':original.get('ksef_environment','')}}

def confirm_ksef(data):
    if data.get('accepted') is not True:raise Problem('Potwierdź, że dokument został przyjęty w KSeF.')
    env=str(data.get('environment',''))
    if env not in ('prod','test','demo'):raise Problem('Wybierz środowisko KSeF.')
    if KSEF.job(int(data['id'])) and not data.get('_api_verified'):
        raise Problem('Ten dokument jest obsługiwany przez API. Użyj sprawdzenia statusu KSeF.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM invoices WHERE id=?',(int(data['id']),)).fetchone()
        xmlrow=c.execute('SELECT * FROM ksef_documents WHERE invoice_id=?',(int(data['id']),)).fetchone()
        if not row or not xmlrow:raise Problem('Brak dokumentu XML.')
        doc=json.loads(row['payload'])
        parent_env=doc.get('correction',{}).get('environment')
        if parent_env and parent_env!=env:raise Problem('Korekta musi być przyjęta w tym samym środowisku KSeF co faktura pierwotna.')
        number=fa3.check_number(str(data['number']),doc['seller']['nip'])
        if xmlrow['state']=='confirmed':
            if number==xmlrow['number'] and env==xmlrow['environment']:return {'confirmed':True}
            raise Problem('Dokument ma już zapisane potwierdzenie. Nie można zastępować numeru KSeF.')
        doc.update(ksef_number=number,ksef_environment=env,ksef_qr=fa3.qr_url(xmlrow['xml'],doc,env))
        pdf=make_pdf(doc)
        c.execute('UPDATE invoices SET payload=?,pdf=? WHERE id=?',(dumps(doc),pdf,row['id']))
        c.execute('UPDATE ksef_documents SET state=?,number=?,environment=?,confirmed=? WHERE invoice_id=?',
                  ('confirmed',number,env,dt.datetime.now().isoformat(),row['id']))
    return {'confirmed':True}


def fingerprint(inv):
    fields = ('invoice_id','order_id','type','date_add','date_sell','date_pay_to','currency',
              'total_price_brutto','total_price_netto','payment','invoice_fullname','invoice_company',
              'invoice_nip','invoice_address','invoice_postcode','invoice_city','invoice_country_code','items')
    raw = json.dumps({k:inv.get(k) for k in fields},ensure_ascii=False,sort_keys=True,default=str)
    return hashlib.sha256(raw.encode()).hexdigest()

def issue(doc):
    s = settings()
    if not s['production']:
        raise Problem('Włącz wystawianie w ustawieniach po uzupełnieniu danych firmy i numeru początkowego.')
    if doc['buyer']['nip'] and not valid_nip(doc['buyer']['nip']):
        raise Problem('Nieprawidłowy polski NIP nabywcy.')
    if any(not doc['buyer'][k] for k in ('name','address','postal','city')):
        raise Problem('Uzupełnij pełny adres nabywcy.')
    if dt.date.fromisoformat(doc['issued']) != dt.date.today():
        raise Problem('Pierwsza wersja wystawia dokumenty z dzisiejszą datą. Starsze rekordy Base można obejrzeć w podglądzie.')
    year = dt.date.today().year
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if doc.get('order_id'):
            row = c.execute('SELECT id FROM invoices WHERE order_id=?',(doc['order_id'],)).fetchone()
            if row:
                return row['id']
        sequence='correction_sequences' if doc.get('correction') else 'sequences'
        if doc.get('correction'):
            ref=doc['correction']
            current=c.execute('SELECT MAX(version) FROM correction_refs WHERE original_key=?',(ref['key'],)).fetchone()[0] or 0
            if current!=ref['version']:
                raise Problem('Stan faktury zmienił się. Otwórz korektę ponownie.')
        c.execute(f'INSERT OR IGNORE INTO {sequence} VALUES (?,?)',(year,1 if doc.get('correction') else s['next']))
        n = c.execute(f'SELECT next FROM {sequence} WHERE year=?',(year,)).fetchone()[0]
        doc['number'] = f"{s['prefix']+'-KOR' if doc.get('correction') else s['prefix']}/{n:04d}/{year}"
        if len(doc['number'])>30:
            raise Problem('Numer jest za długi dla Base.')
        doc['seller'] = s
        xml=fa3.build(doc)
        pdf = make_pdf(doc)
        cur = c.execute('INSERT INTO invoices (number,base_id,order_id,payload,pdf,created) VALUES (?,?,?,?,?,?)',
                        (doc['number'],doc.get('base_id'),doc.get('order_id'),dumps(doc),pdf,dt.datetime.now().isoformat()))
        c.execute(f'UPDATE {sequence} SET next=next+1 WHERE year=?',(year,))
        c.execute('INSERT INTO ksef_documents(invoice_id,xml,state) VALUES (?,?,?)',(cur.lastrowid,xml,'prepared'))
        c.execute('INSERT INTO ksef_prepared VALUES (?,?,?)',(cur.lastrowid,pdf,dumps(doc)))
        katalog.add_from_document(c,doc['buyer'],'invoice',f'local-invoice:{cur.lastrowid}')
        if doc.get('correction'):
            c.execute('INSERT INTO correction_refs VALUES (?,?,?)',(cur.lastrowid,doc['correction']['key'],doc['correction']['version']+1))
        KSEF.enqueue_new(c,cur.lastrowid,doc,xml)
        return cur.lastrowid

def upload_invoice(local_id):
    with connect() as c:
        row = c.execute('SELECT * FROM invoices WHERE id=?',(int(local_id),)).fetchone()
    if not row or not row['base_id']:
        raise Problem('Ta faktura nie ma powiązania z Base.')
    doc=json.loads(row['payload'])
    if doc['buyer'].get('nip') and (not doc.get('ksef_number') or doc.get('ksef_environment')!='prod'):
        raise Problem('Najpierw prześlij XML do produkcyjnego KSeF i zapisz potwierdzenie przyjęcia.')
    if row['sent']:
        return {'sent':True}
    records = base('getInvoices',{'invoice_id':row['base_id'],'get_external_invoices':True,'get_government_data':True}).get('invoices',[])
    if len(records)!=1 or int(records[0]['order_id']) != row['order_id']:
        raise Problem('Nie znaleziono zgodnego rekordu faktury w Base.')
    remote = records[0]
    if remote.get('gov_id') or remote.get('gov_status'):
        raise Problem('Rekord ma status KSeF. Wysyłka zatrzymana.')
    external = remote.get('external_invoice_number','')
    if remote.get('external_id'):
        raise Problem('Rekord został powiązany z innym systemem. Wysyłka zatrzymana.')
    if external and external != row['number']:
        raise Problem('W Base znajduje się już inna faktura zewnętrzna. Wysyłka zatrzymana.')
    snapshot = json.loads(row['payload'])
    if snapshot.get('base_fingerprint') and fingerprint(remote) != snapshot['base_fingerprint']:
        raise Problem('Dane faktury w Base zmieniły się od wystawienia lokalnego dokumentu. Wysyłka zatrzymana; sprawdź dokumenty.')
    # Re-uploading this identical PDF and number is idempotent even after an uncertain timeout.
    base('addOrderInvoiceFile',{'invoice_id':row['base_id'],'external_invoice_number':row['number'],
                              'file':'data:'+base64.b64encode(row['pdf']).decode()})
    with connect() as c:
        c.execute('UPDATE invoices SET sent=1 WHERE id=?',(row['id'],))
    return {'sent':True}

def backup():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    with connect() as source:
        source.backup(db)
    # SQLite serialize is not available on every supported Python build.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'faktury.sqlite3'
        disk = sqlite3.connect(path)
        db.backup(disk)
        disk.close()
        out = BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
            z.write(path,'faktury.sqlite3')
            for row in db.execute('SELECT id,number,payload,pdf FROM invoices'):
                z.writestr(f'dokumenty/{row[0]}.pdf',row[3])
                z.writestr(f'dokumenty/{row[0]}.json',row[2])
            z.writestr('PRZYWRACANIE.txt','Zamknij program. Zachowaj obecną bazę. Zastąp plik faktury.sqlite3 w folderze danych plikiem z kopii. Uruchom program. Token Base nie jest częścią kopii. Kopia zawiera dane klientów i nie jest szyfrowana: przechowuj ją na zaszyfrowanym dysku. Nie uruchamiaj równolegle dwóch kopii programu z tą samą numeracją.')
            ft.add_to_backup(z,db)
            payments.export(z,db)
            katalog.export(z,db)
            for prepared in db.execute('SELECT invoice_id,pdf,payload FROM ksef_prepared'):
                z.writestr(f'przygotowane/{prepared[0]}.pdf',prepared[1])
                z.writestr(f'przygotowane/{prepared[0]}.json',prepared[2])
            for xmlrow in db.execute('SELECT invoice_id,xml FROM ksef_documents'):
                z.writestr(f'dokumenty/{xmlrow[0]}-FA3.xml',xmlrow[1])
            for item in db.execute('SELECT invoice_id,upo FROM ksef_jobs WHERE upo IS NOT NULL'):
                z.writestr(f'ksef/{item[0]}-UPO.xml',item[1])
            for item in db.execute('SELECT invoice_id,id,xml FROM ksef_xml_versions'):
                z.writestr(f'ksef/poprzednie-xml/{item[0]}-{item[1]}.xml',item[2])
        db.close()
        return out.getvalue()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def send(self, obj, kind='application/json; charset=utf-8', filename=None):
        raw = obj if isinstance(obj,bytes) else dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'")
        if filename:
            self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(raw)
    def trusted(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
    def authenticated(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie',''))
            return cookie.get('cc_session') and secrets.compare_digest(cookie['cc_session'].value,SESSION)
        except Exception:
            return False
    def do_GET(self):
        if not self.trusted():
            self.send_error(403)
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == '/start':
            supplied = urllib.parse.parse_qs(url.query).get('key',[''])[0]
            if not secrets.compare_digest(supplied,SESSION):
                self.send_error(403)
                return
            self.send_response(303)
            self.send_header('Location','/')
            self.send_header('Set-Cookie',f'cc_session={SESSION}; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Referrer-Policy','no-referrer')
            self.end_headers()
            return
        if not self.authenticated():
            self.send_error(403,'Open the app using its launcher')
            return
        try:
            if url.path=='/':
                self.send((ROOT/'templates'/'index.html').read_bytes(),'text/html; charset=utf-8')
            elif url.path=='/api/state':
                with connect() as c:
                    orders = [json.loads(x[0]) for x in c.execute('SELECT payload FROM orders ORDER BY id DESC LIMIT 1000')]
                    invoices=[]
                    for x in c.execute('SELECT id,number,order_id,base_id,sent,created,payload FROM invoices ORDER BY id DESC'):
                        item=dict(x);doc=json.loads(item.pop('payload'))
                        item['buyer']=doc['buyer']['name'];item['currency']=doc.get('currency','PLN')
                        item['b2b']=bool(doc['buyer'].get('nip'));item['ksef_ready']=bool(doc.get('ksef_number') and doc.get('ksef_environment')=='prod')
                        item['ksef_status']='KSeF: '+doc['ksef_environment'] if doc.get('ksef_number') else 'Przygotowana do KSeF'
                        item['payment_info']=payments.get(connect,'local','',item['id'],doc)['current']
                        invoices.append(item)
                    seq = c.execute('SELECT next FROM sequences WHERE year=?',(dt.date.today().year,)).fetchone()
                self.send({'settings':settings(),'orders':orders,'invoices':invoices,'token_set':bool(TOKEN),'ksef_settings':KSEF.public_config(),
                           'data_dir':str(DATA),'next_number':seq[0] if seq else settings()['next'],
                           'invoice_total':invoice_total()})
            elif url.path.startswith('/upo/'):
                job=KSEF.job(int(url.path.split('/')[-1]))
                if not job or not job['upo']:raise Problem('UPO nie zostało jeszcze pobrane.')
                self.send(job['upo'],'application/xml','UPO-'+str(job['invoice_id'])+'.xml')
            elif url.path.startswith('/xml/'):
                with connect() as c:
                    row=c.execute('SELECT xml FROM ksef_documents WHERE invoice_id=?',(int(url.path.split('/')[-1]),)).fetchone()
                if not row:raise Problem('Ten starszy dokument nie ma zapisanego XML. Nie wystawiaj archiwum ponownie w KSeF.')
                self.send(row[0],'application/xml',f'FA3-{url.path.split("/")[-1]}.xml')
            elif url.path.startswith('/pdf/'):
                with connect() as c:
                    row = c.execute('SELECT pdf FROM invoices WHERE id=?',(int(url.path.split('/')[-1]),)).fetchone()
                if not row:
                    raise Problem('Nie znaleziono faktury.')
                self.send(row[0],'application/pdf',f'faktura-{url.path.split("/")[-1]}.pdf')
            elif url.path=='/backup':
                self.send(backup(),'application/zip','Kopia-faktur-'+dt.date.today().isoformat()+'.zip')
            elif url.path=='/api/invoices':
                query=urllib.parse.parse_qs(url.query)
                self.send(katalog.invoice_list(connect,query.get('q',[''])[0],query.get('page',[1])[0],
                          query.get('status',['all'])[0],query.get('source',['all'])[0],query.get('type',['all'])[0]))
            elif url.path=='/api/clients':
                query=urllib.parse.parse_qs(url.query)
                self.send(katalog.client_list(connect,query.get('q',[''])[0],query.get('page',[1])[0]))
            elif url.path=='/api/invoice/details':
                query=urllib.parse.parse_qs(url.query)
                self.send(invoice_details(query.get('kind',['local'])[0],query.get('source',[''])[0],query.get('id',[0])[0]))
            elif url.path in ('/ft/pdf','/ft/json','/ft/attachments'):
                query=urllib.parse.parse_qs(url.query)
                row=ft.record(query.get('source',[''])[0],query.get('id',[0])[0])
                key={'/ft/pdf':'pdf','/ft/json':'payload','/ft/attachments':'attachments'}[url.path]
                if not row[key]:
                    raise Problem('Ten plik nie został pobrany. Sprawdź raport importu.')
                kind,extension={'pdf':('application/pdf','pdf'),'payload':('application/json; charset=utf-8','json'),'attachments':('application/zip','zip')}[key]
                content=row[key].encode() if key=='payload' else row[key]
                self.send(content,kind,'Fakturownia-'+str(row['id'])+'.'+extension)
            else:
                self.send_error(404)
        except (Problem,ft.ImportProblem,payments.PaymentProblem,ValueError) as e:
            self.send({'error':str(e)})
    def do_POST(self):
        global TOKEN
        expected = f'http://127.0.0.1:{self.server.server_port}'
        if not self.trusted() or not self.authenticated() or self.headers.get('Origin')!=expected or self.headers.get('X-CC-Request')!='1':
            self.send_error(403)
            return
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 1000000:
                raise Problem('Nieprawidłowy rozmiar żądania.')
            data = json.loads(self.rfile.read(size))
            if self.path=='/api/settings':
                result = save_settings(data)
            elif self.path=='/api/token':
                token = str(data.get('token','')).strip()
                if len(token)>500 or '\n' in token or '\r' in token:
                    raise Problem('Nieprawidłowy token.')
                path=DATA/'base-key.txt'
                if token:
                    temp=DATA/'base-key.tmp'
                    fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                    with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(token)
                    os.replace(temp,path)
                else:path.unlink(missing_ok=True)
                TOKEN = token
                result = {'token_set':bool(TOKEN)}
            elif self.path=='/api/sync':
                result = sync_orders(data.get('days',30))
            elif self.path=='/api/base-preview':
                doc = base_document(data['order_id'])
                self.send(make_pdf(doc,True),'application/pdf')
                return
            elif self.path=='/api/preview':
                self.send(make_pdf(prepare_document(data),True),'application/pdf')
                return
            elif self.path=='/api/issue':
                doc = prepare_document(data)
                result = {'id':issue(doc)}
            elif self.path=='/api/base-issue':
                oid = int(data['order_id'])
                with connect() as c:
                    existing = c.execute('SELECT id FROM invoices WHERE order_id=?',(oid,)).fetchone()
                result = {'id':existing[0] if existing else issue(base_document(oid))}
            elif self.path=='/api/correction/source':
                result=correction_source(data)
            elif self.path in ('/api/correction/preview','/api/correction/issue'):
                doc=prepare_correction(data)
                if self.path.endswith('/preview'):
                    self.send(make_pdf(doc,True),'application/pdf');return
                result={'id':issue(doc)}
            elif self.path=='/api/ksef/confirm':
                data.pop('_api_verified',None)
                result=confirm_ksef(data)
            elif self.path=='/api/ksef/settings':
                result=KSEF.save(data)
            elif self.path=='/api/ksef/connection':
                result=KSEF.connection()
            elif self.path=='/api/ksef/send':
                if data.get('environment')!=KSEF.config()['environment']:
                    raise Problem('Środowisko KSeF zmieniło się. Odśwież panel i sprawdź ustawienia.')
                result=KSEF.queue(int(data['id']))
            elif self.path=='/api/ksef/status':
                job=KSEF.job(int(data['id']))
                if not job:raise Problem('Brak próby wysyłki API.')
                if job['state'] not in ('queued','complete','rejected','blocked'):
                    threading.Thread(target=KSEF.process,args=(int(data['id']),),daemon=True).start()
                result={'checking':True}
            elif self.path=='/api/ksef/repair':
                result=KSEF.repair(int(data['id']),data.get('not_accepted'))
            elif self.path=='/api/upload':
                result = upload_invoice(data['id'])
            elif self.path=='/api/client/save':
                result = katalog.save_client(connect,data,valid_nip)
            elif self.path=='/api/payment/save':
                details=invoice_details(data.get('kind','local'),data.get('source',''),data.get('id',0))
                result=payments.save(connect,details['kind'],details['source'],details['id'],details['data'],data)
            else:
                self.send_error(404)
                return
            self.send(result)
        except (Problem,ft.ImportProblem,payments.PaymentProblem,ValueError,TypeError,KeyError,sqlite3.IntegrityError) as e:
            self.send({'error':str(e)})
        except Exception:
            self.send({'error':'Operacja nie powiodła się. Sprawdź dane i miejsce na dysku. Nie wystawiaj ponownie dokumentu, jeśli jest już na liście faktur.'})

def main():
    os.umask(0o077)
    init()
    KSEF.start()
    server = HTTPServer(('127.0.0.1',0),Handler)
    url = f'http://127.0.0.1:{server.server_port}/start?key={SESSION}'
    print('CreativeCrafts Faktury działa lokalnie. Zamknij to okno lub naciśnij Ctrl+C, aby zakończyć.')
    print('Folder danych:',DATA)
    print('Jeśli panel się nie otworzy, otwórz w przeglądarce:',url)
    if not os.environ.get('CC_NO_BROWSER'):
        threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        KSEF.stop.set()
        server.server_close()

if __name__=='__main__':
    main()
