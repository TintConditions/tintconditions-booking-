"""Tint Conditions owned booking server. Python 3.11+; no external scheduling provider.
Deploy behind HTTPS; configure Square/Twilio separately. See README.
"""
import os, sqlite3, secrets, hashlib, hmac, json, base64, datetime as dt
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from typing import Optional
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
import requests
from dotenv import load_dotenv
load_dotenv()
ROOT=Path(__file__).parent
DB=os.getenv('DATABASE_PATH',str(ROOT/'tint_conditions.sqlite3'))
TZ=ZoneInfo(os.getenv('BUSINESS_TZ','America/Chicago'))
APP_URL=os.getenv('APP_BASE_URL','http://localhost:8000').rstrip('/')
ADMIN_USER=os.getenv('ADMIN_USERNAME','owner')
ADMIN_PASS=os.getenv('ADMIN_PASSWORD','')
DEPOSIT=int(os.getenv('DEPOSIT_AMOUNT_CENTS','5000'))
app=FastAPI(title='Tint Conditions Booking', docs_url=None, redoc_url=None)
basic=HTTPBasic()
SERVICES={
 'full':('All sides & rear',180), 'front':('Front two doors',60), 'windshield':('Full windshield',90),
 'brow':('Windshield brow',30),'sunroof':('Standard sunroof',45),'panoramic':('Panoramic sunroof',75)
}
FILMS={'entry':'Entry Level','standard':'Standard Ceramic','premium':'Premium Ceramic'}
PRICES={'entry':{'full':250,'front':100,'windshield':150,'brow':50,'sunroof':50,'panoramic':100},
'standard':{'full':340,'front':160,'windshield':200,'brow':60,'sunroof':80,'panoramic':160},
'premium':{'full':600,'front':200,'windshield':300,'brow':60,'sunroof':100,'panoramic':200}}

def utcnow(): return dt.datetime.now(dt.timezone.utc)
def iso(x):return x.isoformat()
@contextmanager
def conn():
 c=sqlite3.connect(DB,timeout=20,isolation_level=None)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('PRAGMA busy_timeout=10000')
 try: yield c
 finally:c.close()
def init():
 Path(DB).parent.mkdir(parents=True,exist_ok=True)
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,access_token TEXT NOT NULL,name TEXT NOT NULL,email TEXT NOT NULL,phone TEXT NOT NULL,vehicle TEXT NOT NULL,film TEXT NOT NULL,services TEXT NOT NULL,shade TEXT,notes TEXT,starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,total_cents INTEGER NOT NULL,deposit_cents INTEGER NOT NULL,promo_code TEXT,gift_code TEXT,status TEXT NOT NULL,hold_expires TEXT,checkout_id TEXT,created_at TEXT NOT NULL,reminder_sent INTEGER DEFAULT 0,sms_opt_in INTEGER DEFAULT 0);
 CREATE INDEX IF NOT EXISTS bookings_dates ON bookings(starts_at,ends_at,status);
 CREATE TABLE IF NOT EXISTS blocks(id INTEGER PRIMARY KEY AUTOINCREMENT,starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,reason TEXT);
 CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY,kind TEXT NOT NULL,value INTEGER NOT NULL,remaining_cents INTEGER,active INTEGER NOT NULL DEFAULT 1,used_count INTEGER NOT NULL DEFAULT 0,max_uses INTEGER);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,booking_id TEXT,kind TEXT,created_at TEXT,details TEXT);
 ''')
  defaults={'hours':{'0':['08:30','18:00'],'1':['08:30','18:00'],'2':['08:30','18:00'],'3':['08:30','18:00'],'4':['08:30','18:00']},'buffer_minutes':15,'slot_step_minutes':30,'lead_hours':12,'book_ahead_days':60,'prices':PRICES,'deposit_cents':DEPOSIT}
  for k,v in defaults.items():c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))
init()
def setting(c,key):return json.loads(c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()['value'])
def admin(auth:HTTPBasicCredentials=Depends(basic)):
 if not ADMIN_PASS or not (secrets.compare_digest(auth.username,ADMIN_USER) and secrets.compare_digest(auth.password,ADMIN_PASS)):
  raise HTTPException(401,'Owner credentials required',headers={'WWW-Authenticate':'Basic realm="Tint Conditions Owner"'})
 return True

def localtime(s):
 try:
  v=dt.datetime.fromisoformat(s)
  if v.tzinfo is not None: raise ValueError()
  return v.replace(tzinfo=TZ)
 except Exception:raise HTTPException(422,'Invalid local date/time')
def overlap(a,b,c,d): return a<d and c<b
def cleanup(c):
 c.execute("UPDATE bookings SET status='expired' WHERE status='pending' AND hold_expires < ?",(iso(utcnow()),))
def conflicts(c,start,end,exclude=None):
 cleanup(c)
 bs=c.execute("SELECT id,starts_at,ends_at FROM bookings WHERE status IN ('pending','confirmed')").fetchall()
 for b in bs:
  if b['id']!=exclude and overlap(start,end,dt.datetime.fromisoformat(b['starts_at']),dt.datetime.fromisoformat(b['ends_at'])):return True
 for b in c.execute('SELECT starts_at,ends_at FROM blocks'):
  if overlap(start,end,dt.datetime.fromisoformat(b['starts_at']),dt.datetime.fromisoformat(b['ends_at'])):return True
 return False

def compute(c,film,services,code='',gift=''):
 if film not in FILMS or not services or len(set(services))!=len(services) or any(s not in SERVICES for s in services):raise HTTPException(422,'Invalid film/services')
 prices=setting(c,'prices')
 total=sum(int(prices[film][s])*100 for s in services)
 duration=sum(SERVICES[s][1] for s in services)+int(setting(c,'buffer_minutes'))
 discount=0; gift_used=0
 if code:
  row=c.execute('SELECT * FROM codes WHERE code=? AND active=1',(code.upper().strip(),)).fetchone()
  if not row or row['kind'] not in ('percent','fixed') or (row['max_uses'] is not None and row['used_count']>=row['max_uses']):raise HTTPException(422,'Promo code is invalid')
  discount=min(total, total*row['value']//100 if row['kind']=='percent' else row['value'])
 if gift:
  row=c.execute("SELECT * FROM codes WHERE code=? AND active=1 AND kind='gift'",(gift.upper().strip(),)).fetchone()
  if not row or not row['remaining_cents']:raise HTTPException(422,'Gift certificate is invalid or empty')
  gift_used=min(total-discount,row['remaining_cents'])
 balance=total-discount-gift_used
 deposit=min(balance,int(setting(c,'deposit_cents')))
 return dict(total_cents=total,discount_cents=discount,gift_applied_cents=gift_used,balance_cents=balance,deposit_cents=deposit,duration_minutes=duration)
class Quote(BaseModel):
 film:str;services:list[str];promo_code:str='';gift_code:str=''
@app.post('/api/quote')
def quote(x:Quote):
 with conn() as c:return compute(c,x.film,x.services,x.promo_code,x.gift_code)
@app.get('/api/config')
def config():
 with conn() as c:return {'business':'Tint Conditions Window Tinting','films':FILMS,'services':{k:{'name':v[0],'minutes':v[1]} for k,v in SERVICES.items()},'prices':setting(c,'prices'),'deposit_cents':setting(c,'deposit_cents'),'timezone':str(TZ)}
@app.get('/api/slots')
def slots(date:str,duration:int=90):
 try:d=dt.date.fromisoformat(date)
 except:raise HTTPException(422,'Invalid date')
 if duration<15 or duration>720:raise HTTPException(422,'Invalid duration')
 with conn() as c:
  hours=setting(c,'hours').get(str(d.weekday()))
  if not hours:return {'slots':[]}
  begin=localtime(f'{d}T{hours[0]}'); stop=localtime(f'{d}T{hours[1]}')
  step=dt.timedelta(minutes=int(setting(c,'slot_step_minutes')))
  lead=utcnow()+dt.timedelta(hours=int(setting(c,'lead_hours')))
  if d>utcnow().astimezone(TZ).date()+dt.timedelta(days=int(setting(c,'book_ahead_days'))):return {'slots':[]}
  result=[];t=begin
  while t+dt.timedelta(minutes=duration)<=stop:
   end=t+dt.timedelta(minutes=duration)
   if t.astimezone(dt.timezone.utc)>lead and not conflicts(c,t,end):result.append(t.strftime('%H:%M'))
   t+=step
  return {'slots':result}
class BookingIn(Quote):
 name:str=Field(min_length=2,max_length=120);email:str=Field(min_length=3,max_length=254);phone:str=Field(min_length=7,max_length=30)
 vehicle:str=Field(min_length=2,max_length=180);date:str;time:str;shade:str='';notes:str='';sms_opt_in:bool=False

def log(c,id,kind,details=''):
 c.execute('INSERT INTO events(booking_id,kind,created_at,details) VALUES(?,?,?,?)',(id,kind,iso(utcnow()),details))
def square_request(method,path,payload=None):
 token=os.getenv('SQUARE_ACCESS_TOKEN','')
 location=os.getenv('SQUARE_LOCATION_ID','')
 if not token or not location:raise HTTPException(503,'Square credentials are not configured; required deposits cannot be collected')
 root='https://connect.squareupsandbox.com' if os.getenv('SQUARE_ENVIRONMENT','sandbox')=='sandbox' else 'https://connect.squareup.com'
 headers={'Authorization':'Bearer '+token,'Square-Version':'2026-09-16','Content-Type':'application/json'}
 try:
  r=requests.request(method,root+'/v2/'+path,headers=headers,json=payload,timeout=20)
 except requests.RequestException:raise HTTPException(502,'Square is temporarily unavailable')
 if not r.ok:raise HTTPException(502,'Square payment service rejected the request; check account configuration')
 return r.json()

def square_checkout(booking_id,amount_cents,manage_url):
 data=square_request('POST','online-checkout/payment-links',{
  'idempotency_key':booking_id,
  'quick_pay':{'name':'Tint Conditions appointment deposit '+booking_id,
               'price_money':{'amount':amount_cents,'currency':'USD'},
               'location_id':os.getenv('SQUARE_LOCATION_ID')},
  'checkout_options':{'redirect_url':manage_url+'&payment=return'},
  'payment_note':'Booking '+booking_id})
 link=data.get('payment_link') or {}
 if not link.get('order_id') or not link.get('url'):raise HTTPException(502,'Square did not provide a valid checkout link')
 return link
def release_pending(id):
 with conn() as c:c.execute("UPDATE bookings SET status='cancelled' WHERE id=? AND status='pending'",(id,))
@app.post('/api/book')
def book(x:BookingIn):
 if '@' not in x.email:raise HTTPException(422,'Email is invalid')
 with conn() as c:
  estimate=compute(c,x.film,x.services,x.promo_code,x.gift_code)
  start=localtime(x.date+'T'+x.time)
  end=start+dt.timedelta(minutes=estimate['duration_minutes'])
  if start.astimezone(dt.timezone.utc)<utcnow()+dt.timedelta(hours=int(setting(c,'lead_hours'))):raise HTTPException(409,'Please choose a later appointment')
  hours=setting(c,'hours').get(str(start.weekday()))
  if not hours or start<localtime(x.date+'T'+hours[0]) or end>localtime(x.date+'T'+hours[1]):raise HTTPException(409,'Outside business hours')
  if start.date()>utcnow().astimezone(TZ).date()+dt.timedelta(days=int(setting(c,'book_ahead_days'))):raise HTTPException(409,'Too far in advance')
  c.execute('BEGIN IMMEDIATE')
  if conflicts(c,start,end):c.execute('ROLLBACK');raise HTTPException(409,'That time has been taken; please choose another')
  id=secrets.token_hex(10);token=secrets.token_urlsafe(24)
  expiry=iso(utcnow()+dt.timedelta(minutes=35))
  c.execute('''INSERT INTO bookings(id,access_token,name,email,phone,vehicle,film,services,shade,notes,starts_at,ends_at,total_cents,deposit_cents,promo_code,gift_code,status,hold_expires,created_at,sms_opt_in)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(id,token,x.name,x.email,x.phone,x.vehicle,x.film,json.dumps(x.services),x.shade,x.notes,iso(start),iso(end),estimate['balance_cents'],estimate['deposit_cents'],x.promo_code.upper(),x.gift_code.upper(),'pending',expiry,iso(utcnow()),int(x.sms_opt_in)))
  log(c,id,'hold_created');c.execute('COMMIT')
 manage=APP_URL+'/manage.html?id='+id+'&token='+token
 if estimate['deposit_cents']==0:
  # Fully covered gift card / discount; commit gift balance under lock
  confirm(id,'no_payment_required')
  return {'status':'confirmed','manage_url':manage}
 try:
  link=square_checkout(id,estimate['deposit_cents'],manage)
 except Exception:
  release_pending(id);raise
 with conn() as c:c.execute('UPDATE bookings SET checkout_id=? WHERE id=?',(link['order_id'],id))
 return {'status':'pending','checkout_url':link['url'],'manage_url':manage}

def confirm(id,reason):
 with conn() as c:
  c.execute('BEGIN IMMEDIATE')
  b=c.execute('SELECT * FROM bookings WHERE id=?',(id,)).fetchone()
  if not b or b['status']=='confirmed':c.execute('COMMIT');return
  if b['status'] not in ('pending','expired'):
   c.execute('COMMIT');return
  # Square may confirm a payment after expiration. Flag for manual handling rather than double book.
  start=dt.datetime.fromisoformat(b['starts_at']);end=dt.datetime.fromisoformat(b['ends_at'])
  if conflicts(c,start,end,exclude=id):
   c.execute("UPDATE bookings SET status='payment_conflict' WHERE id=?",(id,));log(c,id,'payment_conflict','Refund or manually resolve');c.execute('COMMIT');return
  for code in [b['promo_code'],b['gift_code']]:
   if not code:continue
   r=c.execute('SELECT * FROM codes WHERE code=?',(code,)).fetchone()
   if not r:continue
   if r['kind']=='gift':
    estimate=compute(c,b['film'],json.loads(b['services']),b['promo_code'],'')
    used=max(0,estimate['balance_cents']-b['total_cents'])
    c.execute('UPDATE codes SET remaining_cents=MAX(0,remaining_cents-?),used_count=used_count+1 WHERE code=?',(used,code))
   else:c.execute('UPDATE codes SET used_count=used_count+1 WHERE code=?',(code,))
  c.execute("UPDATE bookings SET status='confirmed',hold_expires=NULL WHERE id=?",(id,));log(c,id,'confirmed',reason);c.execute('COMMIT')
@app.post('/api/square/webhook')
async def square_webhook(request:Request):
 raw=await request.body()
 secret=os.getenv('SQUARE_WEBHOOK_SIGNATURE_KEY','')
 url=os.getenv('SQUARE_WEBHOOK_URL','')
 signature=request.headers.get('x-square-hmacsha256-signature','')
 if not secret or not url:raise HTTPException(503,'Square webhook is not configured')
 expected=base64.b64encode(hmac.new(secret.encode(),url.encode()+raw,hashlib.sha256).digest()).decode()
 if not hmac.compare_digest(expected,signature):raise HTTPException(403,'Invalid webhook signature')
 try:event=json.loads(raw)
 except ValueError:raise HTTPException(400,'Invalid JSON')
 if event.get('type') not in ('payment.created','payment.updated'):return {'received':True}
 payment_obj=((event.get('data') or {}).get('object') or {}).get('payment') or {}
 payment_id=payment_obj.get('id')
 if not payment_id:return {'received':True}
 # Fetch a trusted payment from Square instead of relying solely on webhook contents.
 payment=square_request('GET','payments/'+payment_id).get('payment') or {}
 if payment.get('status')!='COMPLETED':return {'received':True}
 order_id=payment.get('order_id')
 amount=(payment.get('amount_money') or {}).get('amount')
 currency=(payment.get('amount_money') or {}).get('currency')
 if not order_id or currency!='USD' or payment.get('location_id')!=os.getenv('SQUARE_LOCATION_ID'):
  return {'received':True}
 with conn() as c:
  b=c.execute('SELECT id,deposit_cents FROM bookings WHERE checkout_id=?',(order_id,)).fetchone()
 if b and b['deposit_cents']==amount:confirm(b['id'],'square_verified_payment:'+payment_id)
 return {'received':True}

@app.get('/api/booking/{id}')
def booking(id:str,token:str):
 with conn() as c:
  b=c.execute('SELECT * FROM bookings WHERE id=? AND access_token=?',(id,token)).fetchone()
  if not b:raise HTTPException(404,'Booking not found')
  return {'id':b['id'],'name':b['name'],'vehicle':b['vehicle'],'film':FILMS[b['film']],'services':json.loads(b['services']),'starts_at':b['starts_at'],'status':b['status'],'total_cents':b['total_cents'],'deposit_cents':b['deposit_cents']}
class Reschedule(BaseModel):token:str;date:str;time:str
@app.post('/api/booking/{id}/reschedule')
def reschedule(id:str,x:Reschedule):
 with conn() as c:
  c.execute('BEGIN IMMEDIATE')
  b=c.execute('SELECT * FROM bookings WHERE id=? AND access_token=?',(id,x.token)).fetchone()
  if not b or b['status']!='confirmed':raise HTTPException(403,'Invalid booking or status')
  if dt.datetime.fromisoformat(b['starts_at']).astimezone(dt.timezone.utc)<utcnow()+dt.timedelta(hours=24):raise HTTPException(409,'Please contact the shop to reschedule within 24 hours')
  estimate=compute(c,b['film'],json.loads(b['services']))
  start=localtime(x.date+'T'+x.time);end=start+dt.timedelta(minutes=estimate['duration_minutes'])
  hours=setting(c,'hours').get(str(start.weekday()))
  if not hours or start<localtime(x.date+'T'+hours[0]) or end>localtime(x.date+'T'+hours[1]) or start.astimezone(dt.timezone.utc)<utcnow()+dt.timedelta(hours=24) or start.date()>utcnow().astimezone(TZ).date()+dt.timedelta(days=int(setting(c,'book_ahead_days'))):raise HTTPException(409,'Invalid new appointment time')
  if conflicts(c,start,end,exclude=id):raise HTTPException(409,'Time unavailable')
  c.execute('UPDATE bookings SET starts_at=?,ends_at=?,reminder_sent=0 WHERE id=?',(iso(start),iso(end),id));log(c,id,'rescheduled');c.execute('COMMIT')
 return {'ok':True}
@app.get('/api/admin/overview',dependencies=[Depends(admin)])
def overview():
 with conn() as c:
  cleanup(c)
  rows=[dict(x) for x in c.execute('SELECT id,name,phone,email,vehicle,film,services,starts_at,ends_at,total_cents,deposit_cents,status FROM bookings ORDER BY starts_at DESC LIMIT 250')]
  for r in rows:r['services']=json.loads(r['services'])
  return {'bookings':rows,'blocks':[dict(x) for x in c.execute('SELECT * FROM blocks ORDER BY starts_at')],'hours':setting(c,'hours'),'prices':setting(c,'prices'),'deposit_cents':setting(c,'deposit_cents'),'codes':[dict(x) for x in c.execute('SELECT * FROM codes ORDER BY code')]}
class Block(BaseModel):start:str;end:str;reason:str=''
@app.post('/api/admin/blocks',dependencies=[Depends(admin)])
def add_block(x:Block):
 a=localtime(x.start);b=localtime(x.end)
 if a>=b:raise HTTPException(422,'End must be after start')
 with conn() as c:
  c.execute('INSERT INTO blocks(starts_at,ends_at,reason) VALUES(?,?,?)',(iso(a),iso(b),x.reason))
 return {'ok':True}
@app.delete('/api/admin/blocks/{id}',dependencies=[Depends(admin)])
def delete_block(id:int):
 with conn() as c:c.execute('DELETE FROM blocks WHERE id=?',(id,))
 return {'ok':True}
class Settings(BaseModel):hours:dict[str,list[str]];prices:dict[str,dict[str,int]];deposit_cents:int=Field(ge=0,le=100000)
@app.put('/api/admin/settings',dependencies=[Depends(admin)])
def settings(x:Settings):
 for d,hh in x.hours.items():
  if d not in map(str,range(7)) or len(hh)!=2 or not all(len(v)==5 and v[2]==':' for v in hh) or hh[0]>=hh[1]:raise HTTPException(422,'Invalid hours')
 for film in FILMS:
  if film not in x.prices or any(s not in x.prices[film] or not 0<=x.prices[film][s]<=10000 for s in SERVICES):raise HTTPException(422,'Invalid prices')
 with conn() as c:
  for key,val in [('hours',x.hours),('prices',x.prices),('deposit_cents',x.deposit_cents)]:c.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(val),key))
 return {'ok':True}
class CodeIn(BaseModel):code:str;kind:str;value:int=Field(ge=0);max_uses:Optional[int]=None
@app.post('/api/admin/codes',dependencies=[Depends(admin)])
def add_code(x:CodeIn):
 code=x.code.upper().strip()
 if not code.isalnum() or len(code)>30 or x.kind not in ('percent','fixed','gift') or (x.kind=='percent' and x.value>100):raise HTTPException(422,'Invalid code')
 with conn() as c:
  try:c.execute('INSERT INTO codes(code,kind,value,remaining_cents,max_uses) VALUES(?,?,?,?,?)',(code,x.kind,x.value,x.value if x.kind=='gift' else None,x.max_uses))
  except sqlite3.IntegrityError:raise HTTPException(409,'Code exists')
 return {'ok':True}
@app.delete('/api/admin/codes/{code}',dependencies=[Depends(admin)])
def deactivate_code(code:str):
 with conn() as c:c.execute('UPDATE codes SET active=0 WHERE code=?',(code.upper(),))
 return {'ok':True}
class StatusChange(BaseModel):status:str
@app.post('/api/admin/bookings/{id}/status',dependencies=[Depends(admin)])
def change_status(id:str,x:StatusChange):
 if x.status not in ('cancelled','completed','confirmed'):raise HTTPException(422,'Invalid status')
 with conn() as c:
  b=c.execute('SELECT status FROM bookings WHERE id=?',(id,)).fetchone()
  if not b:raise HTTPException(404,'Not found')
  if x.status=='confirmed' and b['status']!='confirmed':raise HTTPException(409,'Bookings require verified deposit payment')
  c.execute('UPDATE bookings SET status=? WHERE id=?',(x.status,id));log(c,id,'owner_status',x.status)
 return {'ok':True}

def sms_send(to,body):
 sid=os.getenv('TWILIO_ACCOUNT_SID');token=os.getenv('TWILIO_AUTH_TOKEN');fromnum=os.getenv('TWILIO_FROM_NUMBER')
 if not (sid and token and fromnum):return False
 r=requests.post(f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',data={'From':fromnum,'To':to,'Body':body},auth=(sid,token),timeout=20)
 return r.ok
@app.post('/api/jobs/reminders')
def reminders(x_reminder_secret:str=Header(default='')):
 if not os.getenv('REMINDER_SECRET') or not secrets.compare_digest(x_reminder_secret,os.getenv('REMINDER_SECRET')):raise HTTPException(403,'Forbidden')
 sent=0
 with conn() as c:
  bookings=c.execute("SELECT * FROM bookings WHERE status='confirmed' AND sms_opt_in=1 AND reminder_sent=0").fetchall()
  for b in bookings:
   hours=(dt.datetime.fromisoformat(b['starts_at']).astimezone(dt.timezone.utc)-utcnow()).total_seconds()/3600
   if 23<=hours<=25:
    when=dt.datetime.fromisoformat(b['starts_at']).strftime('%a %b %d at %I:%M %p')
    message=f"Tint Conditions reminder: your tint appointment is {when}. Manage: {APP_URL}/manage.html?id={b['id']}&token={b['access_token']}. Reply STOP to opt out."
    if sms_send(b['phone'],message):c.execute('UPDATE bookings SET reminder_sent=1 WHERE id=?',(b['id'],));sent+=1
 return {'sent':sent}
@app.get('/')
def home():return FileResponse(ROOT/'static/index.html')
@app.get('/manage.html')
def manage():return FileResponse(ROOT/'static/manage.html')
@app.get('/owner')
def owner(_:bool=Depends(admin)):return FileResponse(ROOT/'static/owner.html')
from fastapi.staticfiles import StaticFiles
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
