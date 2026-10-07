"""ReserveGuard backend: HTTP + SQLite + packaged IANA timezone data."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, reset_tzpath

reset_tzpath(())
UTC = timezone.utc
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DEMO_FIXTURE = {
    "users": [],
    "restaurants": [
        {
            "id": "garden-room",
            "name": "The Garden Room",
            "timezone": "America/New_York",
            "slot_minutes": 30,
            "reservation_duration_minutes": 90,
            "cancellation_cutoff_minutes": 120,
            "opening_hours": [
                {"weekday": day, "opens": "12:00", "closes": "23:00"}
                for day in DAYS
            ],
            "tables": [
                {"id": "maple", "label": "Maple", "capacity": 2},
                {"id": "cedar", "label": "Cedar", "capacity": 4},
                {"id": "window", "label": "Window", "capacity": 6},
            ],
        },
        {
            "id": "harbor-hearth",
            "name": "Harbor & Hearth",
            "timezone": "Europe/London",
            "slot_minutes": 30,
            "reservation_duration_minutes": 120,
            "cancellation_cutoff_minutes": 180,
            "opening_hours": [
                {"weekday": day, "opens": "11:30", "closes": "22:30"}
                for day in DAYS
            ],
            "tables": [
                {"id": "quay", "label": "Quay", "capacity": 2},
                {"id": "fireside", "label": "Fireside", "capacity": 4},
                {"id": "harbor", "label": "Harbor", "capacity": 6},
            ],
        },
        {
            "id": "saffron-terrace",
            "name": "Saffron Terrace",
            "timezone": "Asia/Dubai",
            "slot_minutes": 30,
            "reservation_duration_minutes": 90,
            "cancellation_cutoff_minutes": 90,
            "opening_hours": [
                {"weekday": day, "opens": "12:30", "closes": "23:30"}
                for day in DAYS
            ],
            "tables": [
                {"id": "palm", "label": "Palm", "capacity": 2},
                {"id": "courtyard", "label": "Courtyard", "capacity": 4},
                {"id": "terrace", "label": "Terrace", "capacity": 8},
            ],
        },
        {
            "id": "sakura-table",
            "name": "Sakura Table",
            "timezone": "Asia/Tokyo",
            "slot_minutes": 30,
            "reservation_duration_minutes": 60,
            "cancellation_cutoff_minutes": 60,
            "opening_hours": [
                {"weekday": day, "opens": "11:00", "closes": "22:00"}
                for day in DAYS
            ],
            "tables": [
                {"id": "ume", "label": "Ume", "capacity": 2},
                {"id": "sakura", "label": "Sakura", "capacity": 4},
                {"id": "tatami", "label": "Tatami", "capacity": 6},
            ],
        },
    ],
    "reservations": [],
}

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,display_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id));
CREATE TABLE IF NOT EXISTS restaurants(id TEXT PRIMARY KEY,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,reference TEXT UNIQUE NOT NULL,user_id TEXT NOT NULL REFERENCES users(id),
 restaurant_id TEXT NOT NULL REFERENCES restaurants(id),table_id TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')),body TEXT NOT NULL,CHECK(start<end));
CREATE TABLE IF NOT EXISTS receipts(user_id TEXT NOT NULL REFERENCES users(id),method TEXT NOT NULL,path TEXT NOT NULL,
 key TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(user_id,method,path,key));
CREATE TRIGGER IF NOT EXISTS overlap_insert BEFORE INSERT ON bookings WHEN NEW.status='confirmed'
BEGIN SELECT RAISE(ABORT,'overlap') WHERE EXISTS(SELECT 1 FROM bookings WHERE status='confirmed'
 AND restaurant_id=NEW.restaurant_id AND table_id=NEW.table_id AND start<NEW.end AND NEW.start<end); END;
CREATE TRIGGER IF NOT EXISTS overlap_update BEFORE UPDATE ON bookings WHEN NEW.status='confirmed'
BEGIN SELECT RAISE(ABORT,'overlap') WHERE EXISTS(SELECT 1 FROM bookings WHERE id<>NEW.id AND status='confirmed'
 AND restaurant_id=NEW.restaurant_id AND table_id=NEW.table_id AND start<NEW.end AND NEW.start<end); END;
"""
TABLES = {'users':4,'tokens':2,'restaurants':2,'bookings':9,'receipts':6}


class Failure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def fail(status=422, code='validation_failed'):
    raise Failure(status, code)


def canonical(value):
    # JSON numeric equality: 4 and 4.0 are equal JSON numbers, but true != 1.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, list):
        value = [json.loads(canonical(x)) for x in value]
    elif isinstance(value, dict):
        value = {k:json.loads(canonical(v)) for k,v in value.items()}
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def field(body, name, kind=str, required=True):
    if name not in body:
        if required: fail()
        return None
    value = body[name]
    if type(value) is not kind:
        fail(400, 'malformed_request')
    return value


def ident(value):
    if not value or len(value)>64: fail()
    return value


def positive(value, zero=False):
    if type(value) is not int or value < (0 if zero else 1): fail()
    return value


def party(value):
    return positive(value)


def local(value):
    if type(value) is not str: fail(400,'malformed_request')
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}', value): fail()
    try: return datetime.strptime(value, '%Y-%m-%dT%H:%M')
    except ValueError: fail()


def resolve(wall, zone):
    choices = set()
    for fold in (0,1):
        aware = wall.replace(tzinfo=zone,fold=fold)
        utc = aware.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None)==wall: choices.add(utc)
    return min(choices) if choices else None


def fits_before_close(start,close,minutes):
    # Compare exact durations before constructing a potentially overflowing end.
    return close is not None and (close-start)//timedelta(microseconds=1)>=minutes*60_000_000


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return salt+':'+digest


def password_ok(password, stored):
    salt, digest = stored.split(':')
    actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return hmac.compare_digest(actual,digest)


class Store:
    def __init__(self,path):
        self.path=path
        self.write_lock=threading.Lock()
        with closing(self.connect()) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=4,isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @staticmethod
    def restaurant(conn, identifier):
        row=conn.execute('SELECT body FROM restaurants WHERE id=?',(identifier,)).fetchone()
        if not row: fail(404,'not_found')
        return json.loads(row[0])

    @staticmethod
    def owned(conn, reference, user):
        row=conn.execute('SELECT body FROM bookings WHERE reference=? AND user_id=?',(reference,user)).fetchone()
        if not row: fail(404,'not_found')
        return json.loads(row[0])

    @staticmethod
    def cutoff(record, restaurant):
        if record['status']=='cancelled': fail(409,'reservation_cancelled')
        remaining=datetime.fromisoformat(record['starts_at']).astimezone(UTC)-datetime.now(UTC)
        if remaining//timedelta(microseconds=1)<=restaurant['cancellation_cutoff_minutes']*60_000_000:
            fail(409,'cutoff_passed')

    @staticmethod
    def position(restaurant, body):
        tid=ident(field(body,'table_id'))
        table=next((t for t in restaurant['tables'] if t['id']==tid),None)
        if table is None: fail(404,'not_found')
        if 'party_size' not in body: fail()
        size=party(body['party_size'])
        if size>table['capacity']: fail(422,'party_exceeds_capacity')
        wall=local(field(body,'starts_at_local'))
        zone=ZoneInfo(restaurant['timezone'])
        start=resolve(wall,zone)
        if start is None: fail(422,'invalid_local_time')
        hours=[h for h in restaurant['opening_hours'] if h['weekday']==DAYS[wall.weekday()]]
        valid=None
        for hour in hours:
            opens=local(wall.date().isoformat()+'T'+hour['opens'])
            closes=local(wall.date().isoformat()+'T'+hour['closes'])
            close=resolve(closes,zone)
            if opens<=wall<closes and fits_before_close(start,close,restaurant['reservation_duration_minutes']):
                end=start+timedelta(minutes=restaurant['reservation_duration_minutes'])
                valid=(opens,end); break
        if valid is None: fail(422,'outside_opening_hours')
        opens,end=valid
        minutes=int((wall-opens).total_seconds()/60)
        if minutes % restaurant['slot_minutes']: fail(422,'not_on_slot_grid')
        return {'restaurant_id':restaurant['id'],'table_id':tid,'party_size':size,
                'starts_at_local':wall.isoformat(timespec='minutes'),
                'starts_at':start.astimezone(zone).isoformat(), 'ends_at':end.astimezone(zone).isoformat()}

    @staticmethod
    def insert(conn, record, user):
        start=int(datetime.fromisoformat(record['starts_at']).timestamp())
        end=int(datetime.fromisoformat(record['ends_at']).timestamp())
        conn.execute('INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?)',
          (record['reservation_id'],record['reference'],user,record['restaurant_id'],record['table_id'],start,end,record['status'],canonical(record)))

    @staticmethod
    def clear(conn):
        for table in ['receipts','bookings','tokens','restaurants','users']: conn.execute('DELETE FROM '+table)

    def reset(self,conn,fixture):
        users=field(fixture,'users',list); restaurants=field(fixture,'restaurants',list)
        reservations=field(fixture,'reservations',list)
        self.clear(conn)
        for user in users:
            if type(user) is not dict: fail(400,'malformed_request')
            uid=ident(field(user,'id')); email=field(user,'email'); secret=field(user,'password'); display=field(user,'display_name')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+',email): fail()
            conn.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,email,password_hash(secret),display))
        for item in restaurants:
            if type(item) is not dict: fail(400,'malformed_request')
            record={k:field(item,k) for k in ['id','name','timezone']}; ident(record['id'])
            try: ZoneInfo(record['timezone'])
            except (ValueError,KeyError): fail()
            for k in ['slot_minutes','reservation_duration_minutes','cancellation_cutoff_minutes']:
                record[k]=positive(field(item,k,int),k=='cancellation_cutoff_minutes')
            hours=field(item,'opening_hours',list); tables=field(item,'tables',list)
            record['opening_hours']=[]; record['tables']=[]
            for h in hours:
                if type(h) is not dict: fail(400,'malformed_request')
                values={k:field(h,k) for k in ['weekday','opens','closes']}
                if values['weekday'] not in DAYS: fail()
                for k in ['opens','closes']:
                    if not re.fullmatch('[0-9]{2}:[0-9]{2}',values[k]): fail()
                    local('2026-01-01T'+values[k])
                if values['opens']>=values['closes']: fail()
                record['opening_hours'].append(values)
            for t in tables:
                if type(t) is not dict: fail(400,'malformed_request')
                values={'id':ident(field(t,'id')),'label':field(t,'label'),'capacity':positive(field(t,'capacity',int))}
                if any(x['id']==values['id'] for x in record['tables']): fail()
                record['tables'].append(values)
            conn.execute('INSERT INTO restaurants VALUES(?,?)',(record['id'],canonical(record)))
        for seed in reservations:
            if type(seed) is not dict: fail(400,'malformed_request')
            restaurant=self.restaurant(conn,ident(field(seed,'restaurant_id')))
            record=self.position(restaurant,seed)
            record.update(reservation_id=ident(field(seed,'id')),reference=field(seed,'reference'),status='confirmed',
                          created_at=datetime.now(UTC).isoformat())
            if not re.fullmatch('[A-Z0-9]{6,12}',record['reference']): fail()
            self.insert(conn,record,ident(field(seed,'user_id')))

    def snapshot(self,conn):
        return {'track':'tablekeeper','format_version':1,
                'state':{table:conn.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall() for table in TABLES}}

    def restore(self,conn,body):
        if body.get('track')!='tablekeeper' or type(body.get('format_version')) is not int or body['format_version']!=1: fail()
        state=body.get('state')
        if type(state) is not dict or set(state)!=set(TABLES): fail()
        # Validate a complete snapshot in isolation before replacing destination.
        temp=sqlite3.connect(':memory:',isolation_level=None)
        try:
            temp.executescript(SCHEMA)
            for table,width in TABLES.items():
                rows=state[table]
                if type(rows) is not list: fail()
                for row in rows:
                    if type(row) is not list or len(row)!=width: fail()
                    for index,value in enumerate(row):
                        expected=int if table=='bookings' and index in (5,6) else str
                        if type(value) is not expected: fail()
                    temp.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')',row)
            self.validate_snapshot(temp)
            self.clear(conn)
            for table in TABLES:
                rows=state[table]
                for row in rows: conn.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')',row)
        except (Failure,sqlite3.Error,ValueError,KeyError,TypeError,OverflowError): fail()
        finally: temp.close()

    def validate_snapshot(self,conn):
        for uid,email,password,display in conn.execute('SELECT * FROM users'):
            ident(uid)
            if not re.fullmatch('[0-9a-f]{32}:[0-9a-f]{128}',password) or not re.fullmatch(r'[^\s@]+@[^\s@]+',email): fail()
        for rid,raw in conn.execute('SELECT * FROM restaurants'):
            r=json.loads(raw)
            if type(r) is not dict or r['id']!=rid: fail()
            for key in ['id','name','timezone']: field(r,key)
            ident(rid); ZoneInfo(r['timezone'])
            for key in ['slot_minutes','reservation_duration_minutes','cancellation_cutoff_minutes']: positive(r[key],key=='cancellation_cutoff_minutes')
            seen=set()
            for t in field(r,'tables',list):
                if type(t) is not dict: fail()
                tid=ident(field(t,'id')); field(t,'label'); positive(t['capacity'])
                if tid in seen: fail()
                seen.add(tid)
            for h in field(r,'opening_hours',list):
                if type(h) is not dict: fail()
                for key in ['weekday','opens','closes']: field(h,key)
                if h['weekday'] not in DAYS or h['opens']>=h['closes']: fail()
                local('2026-01-01T'+h['opens']); local('2026-01-01T'+h['closes'])
        for row in conn.execute('SELECT * FROM bookings'):
            record=json.loads(row[8]); ident(row[0]); ident(row[2])
            self.validate_record(conn,record)
            if [record['reservation_id'],record['reference'],record['restaurant_id'],record['table_id'],record['status']]!=[row[0],row[1],row[3],row[4],row[7]]: fail()
            if not re.fullmatch('[A-Z0-9]{6,12}',record['reference']): fail()
            if [int(datetime.fromisoformat(record['starts_at']).timestamp()),int(datetime.fromisoformat(record['ends_at']).timestamp())]!=list(row[5:7]): fail()
        for user,method,path,key,request,response in conn.execute('SELECT * FROM receipts'):
            if method!='POST' or path not in ['/reservations','/reservation-moves'] or not 1<=len(key)<=255: fail()
            parsed_request=json.loads(request); parsed_response=json.loads(response)
            if type(parsed_request) is not dict or type(parsed_response) is not dict: fail()
            if request!=canonical(parsed_request) or response!=canonical(parsed_response): fail()
            records=[parsed_response] if path=='/reservations' else field(parsed_response,'reservations',list)
            if not 1<=len(records)<=8 or (path=='/reservations' and len(records)!=1): fail()
            references=[]
            for record in records:
                self.validate_record(conn,record)
                current=self.owned(conn,record['reference'],user)
                if any(current[k]!=record[k] for k in ['reservation_id','restaurant_id','created_at']): fail()
                references.append(record['reference'])
            if len(set(references))!=len(references): fail()

    def validate_record(self,conn,record):
        if type(record) is not dict: fail()
        ident(field(record,'reservation_id'))
        if not re.fullmatch('[A-Z0-9]{6,12}',field(record,'reference')): fail()
        if field(record,'status') not in ['confirmed','cancelled']: fail()
        r=self.restaurant(conn,ident(field(record,'restaurant_id')))
        expected=self.position(r,record)
        if any(record[k]!=value for k,value in expected.items()): fail()
        if datetime.fromisoformat(field(record,'created_at')).tzinfo is None: fail()

    def call(self,method,path,query,headers,body):
        # Queue this single process's writers without SQLite busy-handler backoff.
        # BEGIN IMMEDIATE and database triggers still enforce atomicity/occupancy.
        with self.write_lock if method!='GET' else nullcontext():
            return self.transaction(method,path,query,headers,body)

    def transaction(self,method,path,query,headers,body):
        conn=self.connect()
        try:
            # A database write lock serializes every mutation, receipt and reset/import.
            # Read transactions give availability/export a consistent snapshot.
            conn.execute('BEGIN IMMEDIATE' if method!='GET' else 'BEGIN')
            result=self.dispatch(conn,method,path,query,headers,body)
            conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if 'overlap' in str(exc): fail(409,'table_unavailable')
            fail()
        except BaseException:
            conn.rollback(); raise
        finally: conn.close()

    def dispatch(self,conn,method,path,query,headers,body):
        if method=='GET' and path=='/health':
            conn.execute('SELECT 1'); return 200,{'status':'ok'}
        if method=='POST' and path=='/_test/reset': self.reset(conn,body); return 204,None
        if method=='GET' and path=='/_test/export': return 200,self.snapshot(conn)
        if method=='POST' and path=='/_test/import': self.restore(conn,body); return 204,None
        if method=='POST' and path in ['/auth/signup','/auth/login']:
            email=field(body,'email'); secret=field(body,'password')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+',email) or len(secret)<8: fail()
            if path.endswith('signup'):
                display=field(body,'display_name')
                if conn.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone(): fail(409,'email_taken')
                uid=uuid4().hex
                conn.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,email,password_hash(secret),display))
            else:
                row=conn.execute('SELECT id,password,display_name FROM users WHERE email=?',(email,)).fetchone()
                if not row or not password_ok(secret,row[1]): fail(401,'unauthenticated')
                uid,_,display=row
            token=secrets.token_urlsafe(32)
            conn.execute('INSERT INTO tokens VALUES(?,?)',(token,uid))
            return (201 if path.endswith('signup') else 200),{'user_id':uid,'display_name':display,'token':token}
        if method=='GET' and path=='/restaurants':
            return 200,{'restaurants':[{k:json.loads(raw)[k] for k in ['id','name','timezone']} for raw, in conn.execute('SELECT body FROM restaurants ORDER BY rowid')]}
        if method=='GET' and path.startswith('/restaurants/'):
            return 200,self.restaurant(conn,path[len('/restaurants/'):])
        if method=='GET' and path=='/availability':
            for name in ['restaurant_id','date','party_size']:
                if name not in query: fail()
            identifier=ident(query['restaurant_id'][0]); date=query['date'][0]; size=query['party_size'][0]
            if not re.fullmatch('[0-9]+',size): fail()
            size=party(int(size))
            if not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',date): fail()
            day=local(date+'T00:00'); r=self.restaurant(conn,identifier); zone=ZoneInfo(r['timezone']); slots=[]
            for h in r['opening_hours']:
                if h['weekday']!=DAYS[day.weekday()]: continue
                wall=local(date+'T'+h['opens']); close=resolve(local(date+'T'+h['closes']),zone)
                closing_wall=local(date+'T'+h['closes'])
                while wall<closing_wall:
                    start=resolve(wall,zone)
                    if start is not None and fits_before_close(start,close,r['reservation_duration_minutes']):
                        end=start+timedelta(minutes=r['reservation_duration_minutes'])
                        available=[t['id'] for t in r['tables'] if t['capacity']>=size and not conn.execute(
                            "SELECT 1 FROM bookings WHERE restaurant_id=? AND table_id=? AND status='confirmed' AND start<? AND ?<end",
                            (identifier,t['id'],int(end.timestamp()),int(start.timestamp()))).fetchone()]
                        slots.append({'starts_at_local':wall.isoformat(timespec='minutes'),'starts_at':start.astimezone(zone).isoformat(),'available_table_ids':available})
                    if r['slot_minutes']>=(closing_wall-wall)//timedelta(minutes=1): break
                    wall+=timedelta(minutes=r['slot_minutes'])
            return 200,{'restaurant_id':identifier,'date':date,'timezone':r['timezone'],'slots':slots}
        auth=headers.get('Authorization','')
        if not re.fullmatch(r'Bearer [^\s]+',auth): fail(401,'unauthenticated')
        row=conn.execute('SELECT user_id FROM tokens WHERE token=?',(auth[7:],)).fetchone()
        if not row: fail(401,'unauthenticated')
        user=row[0]
        key=None
        if method=='POST' and path in ['/reservations','/reservation-moves']:
            key=headers.get('Idempotency-Key','')
            if not key: fail(400,'missing_idempotency_key')
            if len(key)>255: fail()
            prior=conn.execute('SELECT request,response FROM receipts WHERE user_id=? AND method=? AND path=? AND key=?',(user,method,path,key)).fetchone()
            if prior:
                if prior[0]!=canonical(body): fail(409,'idempotency_key_reuse')
                return 200,json.loads(prior[1])
        if method=='GET' and path=='/reservations':
            return 200,{'reservations':[json.loads(raw) for raw, in conn.execute('SELECT body FROM bookings WHERE user_id=? ORDER BY start DESC,rowid',(user,))]}
        if method=='POST' and path=='/reservations':
            r=self.restaurant(conn,ident(field(body,'restaurant_id')))
            record=self.position(r,body)
            reference=secrets.token_hex(6).upper()
            while conn.execute('SELECT 1 FROM bookings WHERE reference=?',(reference,)).fetchone(): reference=secrets.token_hex(6).upper()
            record.update(reservation_id=uuid4().hex,reference=reference,status='confirmed',created_at=datetime.now(UTC).isoformat())
            self.insert(conn,record,user); result=record
        elif method=='POST' and path=='/reservation-moves':
            moves=body.get('moves')
            if type(moves) is not list or not 1<=len(moves)<=8: fail()
            refs=[]
            for move in moves:
                if type(move) is not dict or type(move.get('reference')) is not str: fail()
                ident(move['reference']); refs.append(move['reference'])
            if len(set(refs))!=len(refs): fail()
            changed=[]; restaurant_id=None
            for move in moves:
                current=self.owned(conn,move['reference'],user)
                if restaurant_id is not None and current['restaurant_id']!=restaurant_id: fail()
                restaurant_id=current['restaurant_id']; r=self.restaurant(conn,restaurant_id)
                self.cutoff(current,r)
                combined={**current,**{k:v for k,v in move.items() if k in ['table_id','starts_at_local','party_size']}}
                position=self.position(r,combined)
                changed.append({**current,**position})
            # Remove ALL old occupancy inside this transaction, then validate final
            # occupancy with the same database trigger. Swaps commit atomically.
            for ref in refs: conn.execute('DELETE FROM bookings WHERE reference=?',(ref,))
            for record in changed: self.insert(conn,record,user)
            result={'reservations':changed}
        elif path.startswith('/reservations/'):
            ref=path[len('/reservations/'):]; cancel=ref.endswith('/cancel')
            if cancel: ref=ref[:-7]
            current=self.owned(conn,ref,user)
            if method=='GET' and not cancel: return 200,current
            if method=='POST' and cancel:
                if current['status']=='cancelled': return 200,current
                self.cutoff(current,self.restaurant(conn,current['restaurant_id']))
                current['status']='cancelled'
                conn.execute('UPDATE bookings SET status=?,body=? WHERE reference=?',('cancelled',canonical(current),ref))
                return 200,current
            if method=='PATCH' and not cancel:
                r=self.restaurant(conn,current['restaurant_id']); self.cutoff(current,r)
                combined={**current,**{k:v for k,v in body.items() if k in ['table_id','starts_at_local','party_size']}}
                result={**current,**self.position(r,combined)}
                conn.execute('DELETE FROM bookings WHERE reference=?',(ref,)); self.insert(conn,result,user)
                return 200,result
            fail(404,'not_found')
        else: fail(404,'not_found')
        conn.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?)',(user,method,path,key,canonical(body),canonical(result)))
        return 201,result


class Handler(BaseHTTPRequestHandler):
    server_version='ReserveGuard'
    def log_message(self,*args): pass

    def handle_request(self):
        try:
            target=urlsplit(self.path); path=unquote(target.path)
            body={}
            if self.command in ['POST','PATCH']:
                length=self.headers.get('Content-Length')
                if length is not None:
                    try: size=int(length)
                    except ValueError: fail(400,'malformed_request')
                    if size<0: fail(400,'malformed_request')
                    raw=self.rfile.read(size)
                else: raw=b''
                if raw:
                    try: body=json.loads(raw,parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
                    except (ValueError,UnicodeError): fail(400,'malformed_request')
                elif not path.endswith('/cancel'): fail(400,'malformed_request')
                if type(body) is not dict: fail(400,'malformed_request')
            status,result=self.server.store.call(self.command,path,parse_qs(target.query,keep_blank_values=True),self.headers,body)
        except Failure as exc:
            status=exc.status; result={'error':{'code':exc.code,'message':exc.code.replace('_',' ')}}
        except (ValueError,OverflowError,KeyError,TypeError):
            status=422; result={'error':{'code':'validation_failed','message':'Invalid value'}}
        except sqlite3.Error:
            status=409; result={'error':{'code':'table_unavailable','message':'Request could not acquire occupancy'}}
        encoded=b'' if status==204 else canonical(result).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers(); self.wfile.write(encoded)

    do_GET=do_POST=do_PATCH=do_DELETE=do_PUT=do_OPTIONS=handle_request


class HTTPServer(ThreadingHTTPServer):
    request_queue_size=128
    daemon_threads=False

def seed_portfolio_demo(path):
    store = Store(path)
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        already_has_data = (
            conn.execute("SELECT 1 FROM restaurants LIMIT 1").fetchone()
            or conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            or conn.execute("SELECT 1 FROM bookings LIMIT 1").fetchone()
        )

        if already_has_data:
            conn.rollback()
            return

        store.reset(conn, DEMO_FIXTURE)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def server(path,host='0.0.0.0',port=8080):
    http=HTTPServer((host,port),Handler)
    http.store=Store(path)
    return http


if __name__=='__main__':
    db=os.environ.get('DATABASE_PATH','/tmp/reserveguard.sqlite')

    if os.environ.get('PORTFOLIO_DEMO') == '1':
        seed_portfolio_demo(db)

    server(db,port=int(os.environ.get('PORT','8080'))).serve_forever()