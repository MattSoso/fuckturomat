"""Local payment ledger. Original invoices and archived bytes remain unchanged."""
import datetime as dt
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import json

class PaymentProblem(Exception):
    pass

CENT=Decimal('.01')
METHODS={'transfer':'Przelew','card':'Karta','cash':'Gotówka','cash_on_delivery':'Pobranie','payu':'PayU',
         'paypal':'PayPal','compensation':'Kompensata','barter':'Barter','off':'Nie podano'}

def amount(value):
    try:
        n=Decimal(str(value).replace(',','.'))
    except (InvalidOperation,ValueError):
        raise PaymentProblem('Nieprawidłowa kwota zapłaty.')
    if not n.is_finite():
        raise PaymentProblem('Nieprawidłowa kwota zapłaty.')
    return n.quantize(CENT,rounding=ROUND_HALF_UP)

def method_label(value):
    return METHODS.get(value,value or 'Nie podano')

def summary(total,paid,due=None,paid_date=None,method='',source_status='',today=None):
    today=today or dt.date.today()
    gross=amount(total)
    known=paid not in (None,'')
    received=amount(paid) if known else None
    balance=gross-received if known else None
    overdue=False
    if balance is not None and balance>0 and due:
        try:
            overdue=dt.date.fromisoformat(str(due)[:10])<today
        except ValueError:
            pass
    if gross<0:
        status='Korekta / rozliczenie zwrotu'
        balance=None
    elif not known:
        status={'paid':'Opłacona — status źródła','partial':'Częściowo opłacona — status źródła'}.get(source_status,'Brak danych o wpłacie')
    elif received>gross:
        status='Nadpłata'
    elif balance==0:
        status='Opłacona'
    elif received>0:
        status='Częściowo opłacona'
    else:
        status='Nieopłacona'
    return {'gross':str(gross),'paid':str(received) if known else None,'remaining':str(max(balance,Decimal(0))) if balance is not None else None,
            'overpayment':str(max(-balance,Decimal(0))) if balance is not None else None,'status':status,'overdue':overdue,
            'due':due or '', 'paid_date':paid_date or '', 'method':method or '', 'method_label':method_label(method),
            'source_status':source_status or ''}

def init(connect):
    with connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS payment_changes(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,source TEXT NOT NULL,
         invoice_id INTEGER NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS payment_lookup ON payment_changes(kind,source,invoice_id,id)')

def original(data,kind):
    if kind=='local':
        return summary(data['totals']['gross'],data.get('paid','0'),data.get('due'),data.get('paid_date'),data.get('payment',''))
    return summary(data.get('price_gross','0'),data.get('paid'),data.get('payment_to'),data.get('paid_date'),
                   data.get('payment_type',''),data.get('status',''))

def get(connect,kind,source,invoice_id,data):
    info=original(data,kind)
    with connect() as c:
        rows=c.execute('SELECT id,payload,created FROM payment_changes WHERE kind=? AND source=? AND invoice_id=? ORDER BY id DESC',
                       (kind,source,int(invoice_id))).fetchall()
    if rows:
        latest=json.loads(rows[0]['payload'])
        current=summary(info['gross'],latest['paid'],latest['due'],latest['paid_date'],latest['method'])
        current.update(note=latest['note'])
    else:
        current=dict(info,note='')
    return {'original':info,'current':current,'revision':rows[0]['id'] if rows else 0,
            'history':[{'id':r['id'],'created':r['created'],**json.loads(r['payload'])} for r in rows]}

def save(connect,kind,source,invoice_id,data,values):
    before=original(data,kind)
    if amount(before['gross'])<0:
        raise PaymentProblem('Rozliczenie zwrotów z korekt ujemnych wymaga osobnej obsługi. Oryginalne dane są dostępne w szczegółach.')
    paid=amount(values.get('paid',''))
    if paid<0:
        raise PaymentProblem('Kwota zapłacona nie może być ujemna.')
    due=str(values.get('due','')).strip()
    date=str(values.get('paid_date','')).strip()
    for v in (due,date):
        if v:
            try:dt.date.fromisoformat(v)
            except ValueError:raise PaymentProblem('Nieprawidłowa data rozliczenia.')
    method=str(values.get('method','')).strip()
    note=str(values.get('note','')).strip()
    if len(method)>100 or len(note)>2000:
        raise PaymentProblem('Sposób płatności: do 100 znaków, notatka: do 2000 znaków.')
    if date and paid==0:
        raise PaymentProblem('Data zapłaty wymaga dodatniej kwoty wpłaty.')
    payload={'paid':str(paid),'due':due,'paid_date':date,'method':method,'note':note}
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT id FROM payment_changes WHERE kind=? AND source=? AND invoice_id=? ORDER BY id DESC LIMIT 1',
                          (kind,source,int(invoice_id))).fetchone()
        if (current['id'] if current else 0)!=int(values.get('revision',0)):
            raise PaymentProblem('Rozliczenie zmieniło się w innym oknie. Otwórz szczegóły ponownie.')
        c.execute('INSERT INTO payment_changes(kind,source,invoice_id,payload,created) VALUES (?,?,?,?,?)',
            (kind,source,int(invoice_id),json.dumps(payload,ensure_ascii=False),dt.datetime.now().isoformat()))
    return get(connect,kind,source,invoice_id,data)

def export(z,c):
    rows=[dict(x,payload=json.loads(x['payload'])) for x in c.execute('SELECT * FROM payment_changes ORDER BY id')]
    z.writestr('rozliczenia/historia.json',json.dumps(rows,ensure_ascii=False))
