"""Real HTTP tests against isolated SQLite stores; no mocked DB successes."""
import concurrent.futures
import copy
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from service import server

FIXTURE={'users':[{'id':'ada','email':'ada@example.com','password':'correct horse','display_name':'Ada'},
                  {'id':'bob','email':'bob@example.com','password':'correct horse','display_name':'Bob'}],
 'restaurants':[{'id':'r','name':'Test','timezone':'Europe/Berlin','slot_minutes':30,
 'reservation_duration_minutes':90,'cancellation_cutoff_minutes':120,
 'opening_hours':[{'weekday':day,'opens':'00:00','closes':'23:30'} for day in ['mon','tue','wed','thu','fri','sat','sun']],
 'tables':[{'id':'a','label':'A','capacity':2},{'id':'b','label':'B','capacity':4},{'id':'c','label':'C','capacity':6}]}],
 'reservations':[]}


class Stage1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=os.environ.get('STAGE1_URL')
        if cls.base:
            cls.http=None
            return
        cls.tmp=tempfile.TemporaryDirectory()
        cls.http=server(str(Path(cls.tmp.name)/'state.sqlite'),'127.0.0.1',0)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True); cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        if cls.http is None: return
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join(); cls.tmp.cleanup()

    def call(self,method,path,body=None,token=None,key=None,raw=None):
        headers={'Content-Type':'application/json'}
        if token: headers['Authorization']='Bearer '+token
        if key is not None: headers['Idempotency-Key']=key
        data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req=urllib.request.Request(self.base+path,data=data,headers=headers,method=method)
        try: response=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as exc: response=exc
        with response:
            payload=response.read(); status=response.status
            self.assertEqual(response.headers['Content-Type'],'application/json; charset=utf-8')
            return status,json.loads(payload) if payload else None

    def setUp(self):
        self.assertEqual(self.call('POST','/_test/reset',FIXTURE),(204,None))
        self.token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        self.bob=self.call('POST','/auth/login',{'email':'bob@example.com','password':'correct horse'})[1]['token']

    def booking(self,table='a',when='2035-09-24T19:00',size=2):
        return {'restaurant_id':'r','table_id':table,'starts_at_local':when,'party_size':size}

    def create(self,table='a',when='2035-09-24T19:00',key='key',token=None):
        status,body=self.call('POST','/reservations',self.booking(table,when),token or self.token,key)
        self.assertEqual(status,201,body); return body

    def error(self,result,status,code):
        self.assertEqual(result[0],status,result[1]); self.assertEqual(result[1]['error']['code'],code)
        self.assertIsInstance(result[1]['error']['message'],str)

    def test_health_public_catalog_and_auth(self):
        self.assertEqual(self.call('GET','/health'),(200,{'status':'ok'}))
        self.assertEqual(self.call('GET','/restaurants')[1]['restaurants'],[{'id':'r','name':'Test','timezone':'Europe/Berlin'}])
        self.assertEqual(self.call('GET','/restaurants/r')[1],FIXTURE['restaurants'][0])
        self.error(self.call('GET','/reservations'),401,'unauthenticated')
        self.error(self.call('POST','/auth/login',{'email':'ada@example.com','password':'wrong password'}),401,'unauthenticated')
        signup={'email':'new@example.com','password':'long password','display_name':'New','unknown':True}
        first=self.call('POST','/auth/signup',signup); self.assertEqual(first[0],201)
        self.error(self.call('POST','/auth/signup',signup),409,'email_taken')
        self.assertEqual(self.call('GET','/reservations',token=first[1]['token']),(200,{'reservations':[]}))
        stored=next(row[2] for row in self.call('GET','/_test/export')[1]['state']['users'] if row[1]=='new@example.com')
        self.assertNotEqual(stored,signup['password'])
        self.assertRegex(stored,r'^[0-9a-f]{32}:[0-9a-f]{128}$')

    def test_errors_types_missing_and_unknowns(self):
        self.error(self.call('POST','/reservations',raw=b'{',token=self.token,key='k'),400,'malformed_request')
        self.error(self.call('POST','/reservations',[],self.token,'k'),400,'malformed_request')
        self.error(self.call('POST','/reservations',self.booking(),self.token),400,'missing_idempotency_key')
        self.error(self.call('POST','/reservations',self.booking(),self.token,'x'*256),422,'validation_failed')
        for value in [True,False,'2',0,-1,1.5,None]:
            body={**self.booking(),'party_size':value}
            self.error(self.call('POST','/reservations',body,self.token,'k'),422,'validation_failed')
        self.error(self.call('POST','/reservations',{**self.booking(),'table_id':3},self.token,'k'),400,'malformed_request')
        self.error(self.call('POST','/reservations',{**self.booking(),'starts_at_local':'2035-09-24T19:00Z'},self.token,'k'),422,'validation_failed')
        self.error(self.call('POST','/reservations',{**self.booking(),'table_id':'x'*65},self.token,'k'),422,'validation_failed')
        for value in ['1e9','4.0','%2B4','0','-1']:
            self.error(self.call('GET','/availability?restaurant_id=r&date=2035-09-24&party_size='+value),422,'validation_failed')
        self.error(self.call('GET','/availability?restaurant_id=r'),422,'validation_failed')
        self.assertEqual(self.call('POST','/reservations',{**self.booking(),'extra':[]},self.token,'k')[0],201)

    def test_availability_overlap_adjacent_and_order(self):
        original=self.create()
        self.error(self.call('POST','/reservations',self.booking(when='2035-09-24T19:30'),self.token,'other'),409,'table_unavailable')
        adjacent=self.create(when='2035-09-24T20:30',key='adjacent')
        rows=self.call('GET','/reservations',token=self.token)[1]['reservations']
        self.assertEqual([r['reference'] for r in rows],[adjacent['reference'],original['reference']])
        avail=self.call('GET','/availability?restaurant_id=r&date=2035-09-24&party_size=2&ignored=yes')[1]
        slot=next(s for s in avail['slots'] if s['starts_at_local'].endswith('T19:00'))
        self.assertEqual(slot['available_table_ids'],['b','c'])
        self.error(self.call('GET','/reservations/'+original['reference'],token=self.bob),404,'not_found')
        self.error(self.call('POST','/reservations/'+original['reference']+'/cancel',{},self.bob),404,'not_found')
        self.error(self.call('PATCH','/reservations/'+original['reference'],{'party_size':1},self.bob),404,'not_found')
        self.error(self.call('POST','/reservations',self.booking(when='2035-09-24T19:01'),self.token,'bad'),422,'not_on_slot_grid')
        self.error(self.call('POST','/reservations',self.booking(when='2035-09-24T23:00'),self.token,'bad'),422,'outside_opening_hours')
        self.error(self.call('POST','/reservations',{**self.booking(),'party_size':3},self.token,'bad'),422,'party_exceeds_capacity')

    def test_idempotency_order_failure_reuse_scope_and_historical(self):
        original=self.create()
        self.assertEqual(self.call('POST','/reservations',dict(reversed(list(self.booking().items()))),self.token,'key'),(200,original))
        self.error(self.call('POST','/reservations',{'party_size':False},self.token,'key'),409,'idempotency_key_reuse')
        self.error(self.call('POST','/reservations',self.booking(),self.token,'failed'),409,'table_unavailable')
        self.assertEqual(self.call('POST','/reservations',self.booking('b'),self.token,'failed')[0],201)
        self.assertEqual(self.call('POST','/reservations',self.booking('c'),self.bob,'key')[0],201)
        self.assertEqual(self.call('POST','/reservations/'+original['reference']+'/cancel',{},self.token)[0],200)
        self.assertEqual(self.call('POST','/reservations',self.booking(),self.token,'key'),(200,original))
        move={'moves':[{'reference':original['reference']}]}
        self.error(self.call('POST','/reservation-moves',move,self.token,'key'),409,'reservation_cancelled')

    def test_cancellation_amendment_and_cutoff(self):
        original=self.create(); ref=original['reference']; blocker=self.create('b',key='block')
        self.error(self.call('PATCH','/reservations/'+ref,{'table_id':'b'},self.token),409,'table_unavailable')
        self.assertEqual(self.call('GET','/reservations/'+ref,token=self.token)[1],original)
        amended=self.call('PATCH','/reservations/'+ref,{'table_id':'c','unknown':'ignored'},self.token)
        self.assertEqual(amended[0],200); self.assertEqual(amended[1]['reservation_id'],original['reservation_id'])
        cancelled=self.call('POST','/reservations/'+ref+'/cancel',None,self.token)
        self.assertEqual(cancelled[1]['status'],'cancelled')
        self.assertEqual(self.call('POST','/reservations/'+ref+'/cancel',{},self.token),cancelled)
        self.error(self.call('PATCH','/reservations/'+ref,{},self.token),409,'reservation_cancelled')
        past=self.create(when='2020-01-01T19:00',key='past')
        self.error(self.call('POST','/reservations/'+past['reference']+'/cancel',{},self.token),409,'cutoff_passed')
        self.error(self.call('PATCH','/reservations/'+past['reference'],{'party_size':False},self.token),409,'cutoff_passed')

    def test_atomic_swap_failure_noop_and_receipt(self):
        a=self.create('a'); b=self.create('b',key='b')
        moves={'moves':[{'reference':a['reference'],'table_id':'b'},{'reference':b['reference'],'table_id':'a'}]}
        status,result=self.call('POST','/reservation-moves',moves,self.token,'key'); self.assertEqual(status,201)
        self.assertEqual([x['table_id'] for x in result['reservations']],['b','a'])
        self.assertEqual(self.call('POST','/reservation-moves',moves,self.token,'key'),(200,result))
        bad={'moves':[{'reference':a['reference'],'table_id':'c'},{'reference':b['reference'],'table_id':'c'}]}
        self.error(self.call('POST','/reservation-moves',bad,self.token,'retry'),409,'table_unavailable')
        self.assertEqual(self.call('GET','/reservations/'+a['reference'],token=self.token)[1],result['reservations'][0])
        self.assertEqual(self.call('POST','/reservation-moves',{'moves':[{'reference':a['reference']}]},self.token,'retry'),(201,{'reservations':[result['reservations'][0]]}))
        self.error(self.call('POST','/reservation-moves',{'moves':[{'reference':a['reference']},{'reference':a['reference']}]},self.token,'dup'),422,'validation_failed')
        self.error(self.call('POST','/reservation-moves',{'moves':[{'reference':a['reference']}]},self.bob,'other'),404,'not_found')

    def test_dst_gaps_folds_and_absolute_duration(self):
        for zone,gap,fold,offset,ending in [('Europe/Berlin','2026-03-29T02:30','2026-10-25T02:30','+02:00','03:00:00+01:00'),
                                           ('America/New_York','2026-03-08T02:30','2026-11-01T01:30','-04:00','02:00:00-05:00')]:
            fixture=copy.deepcopy(FIXTURE); fixture['restaurants'][0]['timezone']=zone
            self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
            token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
            self.error(self.call('POST','/reservations',self.booking(when=gap),token,'gap'),422,'invalid_local_time')
            slots=self.call('GET',f'/availability?restaurant_id=r&date={gap[:10]}&party_size=2')[1]['slots']
            self.assertFalse(any(s['starts_at_local']==gap for s in slots))
            status,record=self.call('POST','/reservations',self.booking(when=fold),token,'fold')
            self.assertEqual(status,201); self.assertTrue(record['starts_at'].endswith(offset)); self.assertTrue(record['ends_at'].endswith(ending))
            self.assertEqual((datetime.fromisoformat(record['ends_at']).astimezone(timezone.utc)-datetime.fromisoformat(record['starts_at']).astimezone(timezone.utc)).total_seconds(),5400)
            slots=self.call('GET',f'/availability?restaurant_id=r&date={fold[:10]}&party_size=2')[1]['slots']
            self.assertEqual(sum(s['starts_at_local']==fold for s in slots),1)

    def test_export_import_tokens_passwords_receipts_and_replacement(self):
        a=self.create(); b=self.create('b',key='b')
        cancelled=self.create('c',key='cancelled')
        self.call('POST','/reservations/'+cancelled['reference']+'/cancel',{},self.token)
        self.error(self.call('POST','/reservations',self.booking(),self.token,'failed'),409,'table_unavailable')
        move={'moves':[{'reference':a['reference'],'table_id':'b'},{'reference':b['reference'],'table_id':'a'}]}
        receipt=self.call('POST','/reservation-moves',move,self.token,'key')[1]
        exported=self.call('GET','/_test/export')[1]
        self.assertEqual(exported['track'],'tablekeeper'); self.assertEqual(exported['format_version'],1)
        self.call('POST','/reservations/'+a['reference']+'/cancel',{},self.token)
        self.call('POST','/_test/reset',{'users':[],'restaurants':[],'reservations':[]})
        self.error(self.call('GET','/reservations',token=self.token),401,'unauthenticated')
        for _ in range(2): self.assertEqual(self.call('POST','/_test/import',exported),(204,None))
        self.assertEqual(self.call('POST','/reservations',self.booking(),self.token,'key'),(200,a))
        self.assertEqual(self.call('POST','/reservation-moves',move,self.token,'key'),(200,receipt))
        self.assertEqual(self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[0],200)
        self.assertEqual(len(self.call('GET','/reservations',token=self.token)[1]['reservations']),3)
        self.assertEqual(self.call('GET','/reservations/'+cancelled['reference'],token=self.token)[1]['status'],'cancelled')
        self.assertEqual(self.call('POST','/reservations',self.booking('c'),self.token,'failed')[0],201)
        before=self.call('GET','/_test/export')[1]
        for bad in [{}, {**exported,'track':'wrong'}, {**exported,'format_version':2}, {**exported,'state':{}}, {**exported,'state':{'bad':[]}}]:
            self.error(self.call('POST','/_test/import',bad),422,'validation_failed')
            self.assertEqual(self.call('GET','/_test/export')[1],before)

    def test_reset_seed_and_invalid_reset_atomicity(self):
        seeded=copy.deepcopy(FIXTURE); seeded['reservations']=[{**self.booking(),'id':'seeded','reference':'BOOK01','user_id':'ada'}]
        self.assertEqual(self.call('POST','/_test/reset',seeded),(204,None))
        self.error(self.call('GET','/reservations',token=self.token),401,'unauthenticated')
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        record=self.call('GET','/reservations/BOOK01',token=token)[1]; self.assertEqual(record['reservation_id'],'seeded')
        invalid=copy.deepcopy(seeded); invalid['users'][0]['id']='x'*65
        self.error(self.call('POST','/_test/reset',invalid),422,'validation_failed')
        self.assertEqual(self.call('GET','/reservations/BOOK01',token=token)[1],record)

    def test_export_import_separate_process_and_destination_replacement(self):
        original=self.create()
        moves={'moves':[{'reference':original['reference'],'table_id':'b'}]}
        receipt=self.call('POST','/reservation-moves',moves,self.token,'key')[1]
        exported=self.call('GET','/_test/export')[1]
        source_base=self.base
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as destination:
            env={**os.environ,'PORT':str(port),'DATABASE_PATH':str(Path(destination)/'other.sqlite')}
            process=subprocess.Popen(
    [sys.executable, str(Path(__file__).resolve().parents[1] / "backend" / "service.py")],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                self.base='http://127.0.0.1:'+str(port)
                for _ in range(100):
                    try:
                        if self.call('GET','/health')[0]==200: break
                    except (OSError,urllib.error.URLError): time.sleep(0.05)
                else: self.fail('Separate service process did not become healthy')
                destination_user=self.call('POST','/auth/signup',{'email':'destination@example.com','password':'destination only','display_name':'Destination'})[1]
                self.assertEqual(self.call('POST','/_test/import',exported),(204,None))
                self.assertEqual(self.call('GET','/_test/export')[1],exported)
                self.error(self.call('GET','/reservations',token=destination_user['token']),401,'unauthenticated')
                self.assertEqual(self.call('POST','/reservations',self.booking(),self.token,'key'),(200,original))
                self.assertEqual(self.call('POST','/reservation-moves',moves,self.token,'key'),(200,receipt))
                self.assertEqual(self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[0],200)
                self.base=source_base
                self.assertEqual(self.call('POST','/reservations/'+original['reference']+'/cancel',{},self.token)[0],200)
                self.base='http://127.0.0.1:'+str(port)
                self.assertEqual(self.call('GET','/reservations/'+original['reference'],token=self.token)[1]['status'],'confirmed')
                self.assertEqual(self.call('POST','/_test/reset',{'users':[],'restaurants':[],'reservations':[]}),(204,None))
                self.error(self.call('GET','/reservations',token=self.token),401,'unauthenticated')
            finally:
                self.base=source_base
                process.terminate(); process.communicate(timeout=5)

    def test_invalid_internal_import_is_atomic(self):
        self.create(); before=self.call('GET','/_test/export')[1]
        mutations=[]
        bad=copy.deepcopy(before); bad['state']['users'][0][2]='plaintext'; mutations.append(bad)
        bad=copy.deepcopy(before); bad['state']['tokens'][0][1]='missing-user'; mutations.append(bad)
        bad=copy.deepcopy(before); bad['state']['restaurants'][0][1]='{}'; mutations.append(bad)
        bad=copy.deepcopy(before); bad['state']['receipts'][0][5]='{}'; mutations.append(bad)
        for change in [{'timezone':'Missing/Zone'},{'name':2},{'tables':[{'id':'a','label':7,'capacity':2}]},{'opening_hours':[{'weekday':'mon','opens':1,'closes':'23:00'}]}]:
            bad=copy.deepcopy(before); r=json.loads(bad['state']['restaurants'][0][1]); r.update(change)
            bad['state']['restaurants'][0][1]=json.dumps(r); mutations.append(bad)
        bad=copy.deepcopy(before); row=bad['state']['bookings'][0].copy(); row[0]='other'; row[1]='SECOND'
        bad['state']['bookings'].append(row); mutations.append(bad)
        for bad in mutations:
            self.error(self.call('POST','/_test/import',bad),422,'validation_failed')
            self.assertEqual(self.call('GET','/_test/export')[1],before)
        self.error(self.call('POST','/_test/import',raw=b'{'),400,'malformed_request')

    def test_database_triggers_reject_direct_insert_and_update(self):
        if self.http is None: self.skipTest('Direct SQL check only runs with an owned local store')
        a=self.create(); b=self.create('b',key='second')
        with closing(self.http.store.connect()) as conn:
            row=list(conn.execute('SELECT * FROM bookings WHERE reference=?',(a['reference'],)).fetchone())
            row[0]='direct'; row[1]='DIRECT'
            with self.assertRaisesRegex(sqlite3.IntegrityError,'overlap'):
                conn.execute('INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?)',row)
            with self.assertRaisesRegex(sqlite3.IntegrityError,'overlap'):
                conn.execute('UPDATE bookings SET table_id=? WHERE reference=?',('a',b['reference']))
        self.assertEqual(self.call('GET','/reservations/'+b['reference'],token=self.token)[1],b)

    def test_closed_days_empty_tables_and_immediate_release(self):
        self.create()
        self.create('b',key='second'); self.create('c',key='third')
        slots=self.call('GET','/availability?restaurant_id=r&date=2035-09-24&party_size=2')[1]['slots']
        self.assertEqual(next(s for s in slots if s['starts_at_local'].endswith('T19:00'))['available_table_ids'],[])
        records=self.call('GET','/reservations',token=self.token)[1]['reservations']
        self.call('POST','/reservations/'+records[0]['reference']+'/cancel',{},self.token)
        slots=self.call('GET','/availability?restaurant_id=r&date=2035-09-24&party_size=2')[1]['slots']
        self.assertEqual(next(s for s in slots if s['starts_at_local'].endswith('T19:00'))['available_table_ids'],[records[0]['table_id']])
        fixture=copy.deepcopy(FIXTURE); fixture['restaurants'][0]['opening_hours']=[]
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        self.assertEqual(self.call('GET','/availability?restaurant_id=r&date=2035-09-24&party_size=2')[1]['slots'],[])

    def test_moves_validation_precedence_and_current_cutoff(self):
        a=self.create(); self.create('b',key='block')
        before=self.call('GET','/_test/export')[1]
        self.error(self.call('POST','/reservation-moves',{'moves':[{'reference':a['reference'],'table_id':'b'},{'reference':'UNKNOWN'}]},self.token,'new'),404,'not_found')
        self.assertEqual(self.call('GET','/_test/export')[1],before)
        for moves in [[],[None],[{'reference':True}],[{'reference':'UNKNOWN'}]*9]:
            self.error(self.call('POST','/reservation-moves',{'moves':moves},self.token,'new'),422,'validation_failed')
        fixture=copy.deepcopy(FIXTURE)
        fixture['restaurants'][0]['cancellation_cutoff_minutes']=3000
        tomorrow=(datetime.now(timezone.utc)+timedelta(days=1)).strftime('%Y-%m-%d')+'T12:00'
        fixture['reservations']=[{**self.booking(when=tomorrow),'id':'near','reference':'NEAR01','user_id':'ada'}]
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        for method,path,body in [('PATCH','/reservations/NEAR01',{'starts_at_local':'2035-09-24T19:00'}),('POST','/reservation-moves',{'moves':[{'reference':'NEAR01','table_id':'unknown'}]})]:
            self.error(self.call(method,path,body,token,'cutoff'),409,'cutoff_passed')

    def test_concurrent_move_and_create_preserve_atomic_occupancy(self):
        a=self.create(); barrier=threading.Barrier(2)
        def move():
            barrier.wait(); return self.call('POST','/reservation-moves',{'moves':[{'reference':a['reference'],'table_id':'b'}]},self.token,'move')
        def create():
            barrier.wait(); return self.call('POST','/reservations',self.booking('b'),self.token,'create')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f=pool.submit(move); g=pool.submit(create); moved,created=f.result(),g.result()
        self.assertEqual(sorted([moved[0],created[0]]),[201,409])
        self.error(moved if moved[0]==409 else created,409,'table_unavailable')
        current=self.call('GET','/reservations/'+a['reference'],token=self.token)[1]
        self.assertEqual(current['table_id'],'b' if moved[0]==201 else 'a')

    def test_cross_restaurant_tables_moves_and_missing_resources(self):
        fixture=copy.deepcopy(FIXTURE); other=copy.deepcopy(fixture['restaurants'][0])
        other['id']='other'; other['tables']=[{'id':'foreign','label':'Foreign','capacity':4}]
        fixture['restaurants'].append(other)
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        self.token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        a=self.create()
        status,b=self.call('POST','/reservations',{**self.booking('foreign'),'restaurant_id':'other'},self.token,'second')
        self.assertEqual(status,201)
        self.error(self.call('POST','/reservations',self.booking('foreign'),self.token,'foreign'),404,'not_found')
        self.error(self.call('POST','/reservation-moves',{'moves':[{'reference':a['reference']},{'reference':b['reference']}]},self.token,'batch'),422,'validation_failed')
        self.assertEqual(self.call('GET','/reservations/'+a['reference'],token=self.token)[1],a)
        self.error(self.call('GET','/restaurants/unknown'),404,'not_found')
        self.error(self.call('GET','/availability?restaurant_id=unknown&date=2035-09-24&party_size=2'),404,'not_found')
        self.error(self.call('GET','/reservations',token='bad token'),401,'unauthenticated')

    def test_early_calendar_year_preserves_four_digit_local_date(self):
        fixture=copy.deepcopy(FIXTURE); fixture['restaurants'][0]['timezone']='UTC'
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        when='0001-01-01T19:00'
        status,record=self.call('POST','/reservations',self.booking(when=when),token,'past')
        self.assertEqual(status,201); self.assertEqual(record['starts_at_local'],when)
        self.assertEqual(record['starts_at'],'0001-01-01T19:00:00+00:00')
        slots=self.call('GET','/availability?restaurant_id=r&date=0001-01-01&party_size=2')[1]['slots']
        self.assertTrue(all(s['starts_at_local'].startswith('0001-01-01T') for s in slots))

    def test_last_calendar_date_availability_and_late_booking(self):
        fixture=copy.deepcopy(FIXTURE); fixture['restaurants'][0]['timezone']='UTC'
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        status,record=self.call('POST','/reservations',self.booking(when='9999-12-31T22:00'),token,'last')
        self.assertEqual(status,201); self.assertEqual(record['ends_at'],'9999-12-31T23:30:00+00:00')
        status,available=self.call('GET','/availability?restaurant_id=r&date=9999-12-31&party_size=2')
        self.assertEqual(status,200,available)
        slots=available['slots']; self.assertEqual(len(slots),45)
        self.assertEqual(slots[0]['starts_at_local'],'9999-12-31T00:00')
        self.assertEqual(slots[-1]['starts_at_local'],'9999-12-31T22:00')
        self.assertEqual(slots[-1]['available_table_ids'],['b','c'])
        self.error(self.call('POST','/reservations',self.booking(when='9999-12-31T23:00'),token,'late'),422,'outside_opening_hours')
        self.assertEqual(self.call('GET','/reservations/'+record['reference'],token=token)[1],record)

    def test_first_calendar_date_cutoff_on_cancel_amend_and_moves(self):
        fixture=copy.deepcopy(FIXTURE); fixture['restaurants'][0]['timezone']='UTC'
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        status,record=self.call('POST','/reservations',self.booking(when='0001-01-01T00:00'),token,'first')
        self.assertEqual(status,201)
        before=self.call('GET','/_test/export')[1]; ref=record['reference']
        for method,path,body in [('POST','/reservations/'+ref+'/cancel',{}),('PATCH','/reservations/'+ref,{'party_size':1}),
                                 ('POST','/reservation-moves',{'moves':[{'reference':ref}]})]:
            self.error(self.call(method,path,body,token,'boundary-move'),409,'cutoff_passed')
            self.assertEqual(self.call('GET','/_test/export')[1],before)

    def test_large_configuration_minutes_do_not_overflow_calendar(self):
        fixture=copy.deepcopy(FIXTURE); r=fixture['restaurants'][0]
        r.update(timezone='UTC',slot_minutes=10**30,cancellation_cutoff_minutes=10**30)
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        status,available=self.call('GET','/availability?restaurant_id=r&date=9999-12-31&party_size=2')
        self.assertEqual(status,200,available); self.assertEqual(len(available['slots']),1)
        status,record=self.call('POST','/reservations',self.booking(when='9999-12-31T00:00'),token,'large')
        self.assertEqual(status,201)
        self.error(self.call('POST','/reservations/'+record['reference']+'/cancel',{},token),409,'cutoff_passed')
        r['reservation_duration_minutes']=10**30
        self.assertEqual(self.call('POST','/_test/reset',fixture)[0],204)
        token=self.call('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        self.assertEqual(self.call('GET','/availability?restaurant_id=r&date=9999-12-31&party_size=2')[1]['slots'],[])
        self.error(self.call('POST','/reservations',self.booking(when='9999-12-31T00:00'),token,'too-long'),422,'outside_opening_hours')

    def test_fifty_distinct_and_identical_key_contenders(self):
        for same in [False,True]:
            if same: self.setUp()
            barrier=threading.Barrier(50)
            def contender(i):
                barrier.wait(timeout=4)
                return self.call('POST','/reservations',self.booking(),self.token,'same' if same else str(i))
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool: results=list(pool.map(contender,range(50)))
            self.assertEqual(sum(status==201 for status,_ in results),1)
            if same:
                self.assertEqual(sum(status==200 for status,_ in results),49)
                self.assertTrue(all(body==results[0][1] for _,body in results))
            else:
                self.assertEqual(sum(status==409 and body['error']['code']=='table_unavailable' for status,body in results),49)
            self.assertEqual(len(self.call('GET','/reservations',token=self.token)[1]['reservations']),1)


if __name__=='__main__': unittest.main()
