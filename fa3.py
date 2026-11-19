"""Manual ONLINE FA(3) export. No transmission or claim of KSeF acceptance."""
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone
from functools import lru_cache
import hashlib, base64, re
from lxml import etree as E

NS='http://crd.gov.pl/wzor/2025/06/25/13775/'
SCHEMAS=Path(__file__).resolve().parent/'schemas'
class XMLProblem(ValueError): pass
class Resolver(E.Resolver):
    def resolve(self,url,pubid,context):
        path=SCHEMAS/url.rsplit('/',1)[-1]
        if path.is_file(): return self.resolve_filename(str(path),context)
        raise XMLProblem('Brakuje lokalnego schematu XML: '+path.name)
@lru_cache(maxsize=1)
def schema():
    parser=E.XMLParser(no_network=True,resolve_entities=False,load_dtd=False)
    parser.resolvers.add(Resolver())
    return E.XMLSchema(E.parse(str(SCHEMAS/'schemat.xsd'),parser))
def add(parent,name,value=None,**attrs):
    node=E.SubElement(parent,'{'+NS+'}'+name,**attrs)
    if value is not None: node.text=str(value)
    return node

def groups(items):
    result={}
    for row in items:
        group=result.setdefault(str(row['tax_rate']),[Decimal(0),Decimal(0)])
        group[0]+=Decimal(row['net']);group[1]+=Decimal(row['vat'])
    return result

def build(doc):
    root=E.Element('{'+NS+'}Faktura',nsmap={None:NS})
    head=add(root,'Naglowek')
    add(head,'KodFormularza','FA',kodSystemowy='FA (3)',wersjaSchemy='1-0E')
    add(head,'WariantFormularza','3')
    add(head,'DataWytworzeniaFa',datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    add(head,'SystemInfo','CreativeCrafts Faktury 0.7')
    for role,data in [('Podmiot1',doc['seller']),('Podmiot2',doc['buyer'])]:
        node=add(root,role);ident=add(node,'DaneIdentyfikacyjne')
        nip=re.sub(r'[\s-]','',data.get('nip',''))
        if nip: add(ident,'NIP',nip)
        elif role=='Podmiot2': add(ident,'BrakID','1')
        add(ident,'Nazwa',data['name'])
        address=add(node,'Adres');add(address,'KodKraju','PL')
        add(address,'AdresL1',data['address'])
        add(address,'AdresL2',data['postal']+' '+data['city'])
        if role=='Podmiot2':add(node,'JST','2');add(node,'GV','2')
    fa=add(root,'Fa');add(fa,'KodWaluty','PLN');add(fa,'P_1',doc['issued'])
    if doc['seller'].get('place'):add(fa,'P_1M',doc['seller']['place'])
    add(fa,'P_2',doc['number']);add(fa,'P_6',doc['sold'])
    totals=groups(doc['items']);before=groups(doc.get('before_items',[]))
    for rate,nettag,vattag in [('23','P_13_1','P_14_1'),('8','P_13_2','P_14_2'),('5','P_13_3','P_14_3'),('0','P_13_6_1',None)]:
        if rate in totals or rate in before:
            values=[a-b for a,b in zip(totals.get(rate,[Decimal(0)]*2),before.get(rate,[Decimal(0)]*2))]
            add(fa,nettag,f'{values[0]:.2f}')
            if vattag:add(fa,vattag,f'{values[1]:.2f}')
    add(fa,'P_15',doc['totals']['gross'])
    ann=add(fa,'Adnotacje')
    for key in ('P_16','P_17','P_18','P_18A'):add(ann,key,'2')
    add(add(ann,'Zwolnienie'),'P_19N','1')
    add(add(ann,'NoweSrodkiTransportu'),'P_22N','1')
    add(ann,'P_23','2');add(add(ann,'PMarzy'),'P_PMarzyN','1')
    correction=doc.get('correction')
    add(fa,'RodzajFaktury','KOR' if correction else 'VAT')
    if correction:
        add(fa,'PrzyczynaKorekty',correction['reason'])
        original=add(fa,'DaneFaKorygowanej')
        add(original,'DataWystFaKorygowanej',correction['issued'])
        add(original,'NrFaKorygowanej',correction['number'])
        if correction.get('ksef_number'):
            add(original,'NrKSeF','1');add(original,'NrKSeFFaKorygowanej',correction['ksef_number'])
        else:add(original,'NrKSeFN','1')
    if doc.get('description'):
        note=add(fa,'DodatkowyOpis');add(note,'Klucz','Uwagi');add(note,'Wartosc',doc['description'])
    def row(item,index,before=False):
        node=add(fa,'FaWiersz');add(node,'NrWierszaFa',index)
        add(node,'P_7',item['name']);add(node,'P_8A',item.get('quantity_unit','szt'))
        add(node,'P_8B',item['quantity']);add(node,'P_9A' if 'price_netto' in item else 'P_9B',item.get('price_netto',item['price_brutto']))
        # Match the value to the calculation basis. The taxpayer application's
        # business validator rejects simultaneous net and gross line values,
        # even though the XSD permits both optional elements.
        add(node,'P_11' if 'price_netto' in item else 'P_11A',item['net'] if 'price_netto' in item else item['gross'])
        add(node,'P_11Vat',item['vat'])
        add(node,'P_12','0 KR' if item['tax_rate']=='0' else item['tax_rate'])
        if before:add(node,'StanPrzed','1')
    for index,item in enumerate(doc['items'],1):
        if correction:row(doc['before_items'][index-1],index,True)
        row(item,index)
    payment=add(fa,'Platnosc')
    paid=Decimal(doc.get('paid','0'));gross=Decimal(doc['totals']['gross'])
    # Unknown historical payment dates stay unknown; never invent a date in XML.
    if paid>0 and doc.get('paid_date') and gross>0:
        if paid>=gross:add(payment,'Zaplacono','1');add(payment,'DataZaplaty',doc['paid_date'])
        else:
            add(payment,'ZnacznikZaplatyCzesciowej','1');part=add(payment,'ZaplataCzesciowa')
            add(part,'KwotaZaplatyCzesciowej',f'{paid:.2f}');add(part,'DataZaplatyCzesciowej',doc['paid_date'])
    add(add(payment,'TerminPlatnosci'),'Termin',doc['due'])
    method=doc.get('payment','').strip()
    codes={'gotówka':'1','karta':'2','przelew':'6','blik':'7'}
    if method.lower() in codes:add(payment,'FormaPlatnosci',codes[method.lower()])
    elif method:add(payment,'PlatnoscInna','1');add(payment,'OpisPlatnosci',method)
    if doc['seller'].get('bank'):
        bank=add(payment,'RachunekBankowy');add(bank,'NrRB',doc['seller']['bank'].replace(' ',''))
        if doc['seller'].get('bank_name'):add(bank,'NazwaBanku',doc['seller']['bank_name'])
    if not schema().validate(root):
        error=schema().error_log.last_error
        raise XMLProblem('XML FA(3) nie przechodzi walidacji: '+error.message)
    return E.tostring(root,encoding='UTF-8',xml_declaration=True)

KSEF_PATTERN=r'\d{10}-\d{8}-[0-9A-F]{6}-?[0-9A-F]{6}-[0-9A-F]{2}'
def check_number(number,nip,issued=None):
    number=number.strip().upper()
    if not re.fullmatch(KSEF_PATTERN,number) or number[:10]!=re.sub(r'[\s-]','',nip):
        raise XMLProblem('Nieprawidłowy format numeru KSeF lub NIP sprzedawcy.')
    # KSeF date is receipt date and may differ from P_1; do not require equality.
    try:datetime.strptime(number[11:19],'%Y%m%d')
    except ValueError:raise XMLProblem('Nieprawidłowa data w numerze KSeF.')
    return number

def qr_url(xml,doc,env):
    host={'prod':'qr.ksef.mf.gov.pl','test':'qr-test.ksef.mf.gov.pl','demo':'qr-demo.ksef.mf.gov.pl'}[env]
    digest=base64.urlsafe_b64encode(hashlib.sha256(xml).digest()).decode().rstrip('=')
    date=datetime.strptime(doc['issued'],'%Y-%m-%d').strftime('%d-%m-%Y')
    nip=re.sub(r'[\s-]','',doc['seller']['nip'])
    return f'https://{host}/invoice/{nip}/{date}/{digest}'
