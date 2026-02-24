import os,json,time,base64,requests,threading
from datetime import datetime,timezone
from flask import Flask,request,jsonify
app=Flask(__name__)
GITHUB_TOKEN=os.environ.get('GITHUB_TOKEN','')
GITHUB_REPO=os.environ.get('GITHUB_REPO','jw0808-blip/trading-bot')
DISCORD_WEBHOOK=os.environ.get('DISCORD_WEBHOOK_AI_LOGS','')
SECRET=os.environ.get('LOGGER_SECRET','traderjoes2024')
API=f'https://api.github.com/repos/{GITHUB_REPO}/contents/conversations.md'
GHH={'Authorization':f'token {GITHUB_TOKEN}','Accept':'application/vnd.github.v3+json'}
def get_log():
    try:
        r=requests.get(API,headers=GHH,timeout=10)
        if r.status_code==200:
            d=r.json();return base64.b64decode(d['content']).decode(),d['sha']
        return '# TraderJoes Log\n\n---\n\n',None
    except Exception as e:
        print(f'[GH GET] {e}');return None,None
def append_gh(entry):
    if not GITHUB_TOKEN:print('[GH] no token');return
    cur,sha=get_log()
    if cur is None:return
    ts=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    p={'message':f'Log {ts}','content':base64.b64encode((cur+entry).encode()).decode()}
    if sha:p['sha']=sha
    try:
        r=requests.put(API,headers=GHH,json=p,timeout=15)
        print(f'[GH] {"OK" if r.status_code in(200,201) else "ERR "+str(r.status_code)}')
    except Exception as e:print(f'[GH PUT] {e}')
def post_disc(msg):
    if not DISCORD_WEBHOOK:return
    try:
        for c in [msg[i:i+1900] for i in range(0,len(msg),1900)]:
            requests.post(DISCORD_WEBHOOK,json={'content':c},timeout=10);time.sleep(0.3)
    except Exception as e:print(f'[Discord] {e}')
def log_event(src,content,author='System'):
    ts=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    md=f'\n## {src} - {ts}\n**Author:** {author}\n\n{content}\n\n---\n'
    disc=f'[{src}] {ts} | {author}\n{content[:500]}'
    t1=threading.Thread(target=append_gh,args=(md,));t2=threading.Thread(target=post_disc,args=(disc,))
    t1.start();t2.start();t1.join();t2.join()
@app.route('/health')
def health():return jsonify({'status':'ok','github':bool(GITHUB_TOKEN),'discord':bool(DISCORD_WEBHOOK)})
@app.route('/log',methods=['POST'])
def log_ep():
    d=request.get_json(force=True,silent=True) or {}
    if d.get('secret')!=SECRET:return jsonify({'error':'Unauthorized'}),401
    c=d.get('content','').strip()
    if not c:return jsonify({'error':'content required'}),400
    log_event(d.get('source','Manual'),c,d.get('author','Unknown'));return jsonify({'status':'logged'})
@app.route('/log/claude',methods=['POST'])
def log_cl():
    d=request.get_json(force=True,silent=True) or {}
    if d.get('secret')!=SECRET:return jsonify({'error':'Unauthorized'}),401
    msgs=d.get('messages',[]) or [{'role':'user','content':d.get('content','')}]
    fmt='\n\n'.join(f"**{m.get('role','?').upper()}:** {m.get('content','')}" for m in msgs)
    log_event('Claude',fmt,d.get('author','TraderJoe'));return jsonify({'status':'logged','count':len(msgs)})
@app.route('/log/bot',methods=['POST'])
def log_bt():
    d=request.get_json(force=True,silent=True) or {}
    if d.get('secret')!=SECRET:return jsonify({'error':'Unauthorized'}),401
    log_event('Sub-Bot',f"Bot:{d.get('bot_name','?')} Action:{d.get('action','')} Details:{d.get('details','')}",d.get('bot_name','Bot'));return jsonify({'status':'logged'})
def heartbeat():
    time.sleep(60)
    while True:
        log_event('Heartbeat',f'AI Logger running. {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}','ai-logger');time.sleep(1800)
if __name__=='__main__':
    print(f'AI Logger | GH:{"OK" if GITHUB_TOKEN else "MISSING"} | Discord:{"OK" if DISCORD_WEBHOOK else "MISSING"}')
    threading.Thread(target=heartbeat,daemon=True).start()
    log_event('Startup','AI Logger started. POST /log /log/claude /log/bot GET /health','ai-logger')
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5001)))
