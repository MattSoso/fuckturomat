"""Offline access to previously imported data. No Fakturownia API or credentials."""
import json
import re
import urllib.parse
CONNECT=None

class ImportProblem(Exception):pass

def normalize_domain(value):
    value = str(value).strip().lower()
    if '://' not in value:
        value = 'https://' + value
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'https' or url.username or url.password or url.query or url.fragment
            or url.path not in ('','/') or url.port is not None
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.fakturownia\.pl', url.hostname or '')
            or url.hostname == 'app.fakturownia.pl'):
        raise ImportProblem('Podaj adres swojego konta, np. creativecrafts.fakturownia.pl. Bez ścieżki i tokenu.')
    return url.hostname

def clean(data):
    # API token, public share tokens and credential fields must not enter backups.
    if isinstance(data,dict):
        return {k:clean(v) for k,v in data.items() if not re.search(r'token|password|secret|authorization',k,re.I)}
    if isinstance(data,list):
        return [clean(v) for v in data]
    return data

def encode(data):
    return json.dumps(clean(data),ensure_ascii=False,default=str)

def init(connect):
    global CONNECT
    CONNECT=connect
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS ft_clients(source TEXT NOT NULL,id INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS ft_invoices(source TEXT NOT NULL,id INTEGER NOT NULL,payload TEXT NOT NULL,pdf BLOB,attachments BLOB,
            pdf_sha256 TEXT,attachment_state TEXT NOT NULL DEFAULT 'not_requested',updated TEXT NOT NULL,PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS ft_runs(id INTEGER PRIMARY KEY,source TEXT NOT NULL,state TEXT NOT NULL,report TEXT NOT NULL);
        ''')
        # A stopped process cannot truthfully report a running import after restart.
        c.execute("UPDATE ft_runs SET state='interrupted' WHERE state='running'")

def record(source,iid):
    source=normalize_domain(source)
    with CONNECT() as c:
        row=c.execute('SELECT * FROM ft_invoices WHERE source=? AND id=?',(source,int(iid))).fetchone()
    if not row:
        raise ImportProblem('Nie znaleziono dokumentu w archiwum.')
    return dict(row)

def add_to_backup(z,c):
    for row in c.execute('SELECT * FROM ft_clients'):
        z.writestr(f"fakturownia/{row['source']}/kontrahenci/{row['id']}.json",row['payload'])
    manifest=[]
    for row in c.execute('SELECT * FROM ft_invoices'):
        stem=f"fakturownia/{row['source']}/dokumenty/{row['id']}"
        z.writestr(stem+'.json',row['payload'])
        if row['pdf']:
            z.writestr(stem+'.pdf',row['pdf'])
        if row['attachments']:
            z.writestr(stem+'-zalaczniki.zip',row['attachments'])
        manifest.append({'source':row['source'],'id':row['id'],'number':json.loads(row['payload']).get('number'),
                         'pdf_sha256':row['pdf_sha256'],'attachment_state':row['attachment_state']})
    z.writestr('fakturownia/manifest.json',encode(manifest))
    for row in c.execute('SELECT * FROM ft_runs'):
        z.writestr(f"fakturownia/raport-importu-{row['id']}.json",encode(dict(row)))
