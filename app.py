import os, json, uuid, subprocess
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from openai import OpenAI

BASE=Path(__file__).parent
OUT=BASE/"outputs"; TMP=BASE/"tmp"
OUT.mkdir(exist_ok=True); TMP.mkdir(exist_ok=True)
app=FastAPI(title="ClipViral AI PRO")
app.mount("/outputs",StaticFiles(directory=str(OUT)),name="outputs")

def run(cmd):
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if p.returncode: raise RuntimeError(p.stderr[-1200:])
    return p.stdout.strip()

def valid_yt(url):
    try: return (urlparse(url).hostname or "").lower() in {"youtube.com","www.youtube.com","m.youtube.com","youtu.be","www.youtu.be"}
    except: return False

def stime(sec):
    ms=max(0,int(sec*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

@app.get("/")
def home(): return FileResponse(BASE/"static"/"index.html")

@app.get("/api/health")
def health(): return {"ok":True,"name":"ClipViral AI PRO"}

@app.get("/api/youtube")
def youtube(url:str):
    if not valid_yt(url): raise HTTPException(400,"Link YouTube tidak valid.")
    import urllib.request
    req=urllib.request.Request("https://www.youtube.com/oembed?url="+url+"&format=json",headers={"User-Agent":"ClipViral/1.0"})
    try:
        with urllib.request.urlopen(req,timeout=10) as r: d=json.loads(r.read())
        return {"title":d.get("title"),"author":d.get("author_name"),"thumbnail":d.get("thumbnail_url")}
    except: raise HTTPException(400,"Metadata video tidak tersedia atau video tidak publik.")

@app.post("/api/create")
async def create(file:UploadFile=File(...),count:int=Form(5)):
    key=os.getenv("OPENAI_API_KEY")
    if not key: raise HTTPException(503,"Admin belum memasang OPENAI_API_KEY pada server.")
    if count<1 or count>10: raise HTTPException(400,"Jumlah klip harus 1 sampai 10.")
    ext=Path(file.filename or "video.mp4").suffix.lower()
    if ext not in {".mp4",".mov",".m4v",".webm",".mkv"}: raise HTTPException(400,"Format video tidak didukung.")
    jid=uuid.uuid4().hex[:10]; src=TMP/f"{jid}{ext}"; aud=TMP/f"{jid}.mp3"
    try:
        size=0
        with src.open("wb") as f:
            while True:
                b=await file.read(1024*1024)
                if not b: break
                size+=len(b)
                if size>500*1024*1024: raise HTTPException(413,"Maksimum video 500 MB.")
                f.write(b)
        run(["ffmpeg","-y","-i",str(src),"-vn","-ac","1","-ar","16000","-b:a","48k",str(aud)])
        if aud.stat().st_size>24*1024*1024: raise HTTPException(413,"Audio terlalu panjang. Gunakan video lebih pendek.")
        client=OpenAI(api_key=key)
        with aud.open("rb") as f:
            tr=client.audio.transcriptions.create(model="whisper-1",file=f,response_format="verbose_json",timestamp_granularities=["segment"])
        segs=[{"start":float(s.start),"end":float(s.end),"text":str(s.text).strip()} for s in (tr.segments or []) if str(s.text).strip()]
        if not segs: raise HTTPException(422,"Ucapan tidak terdeteksi.")
        transcript="\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs)
        prompt=("Pilih maksimal "+str(count)+" bagian paling menarik untuk video pendek. Durasi 15-60 detik jika memungkinkan. "
                "Gunakan timestamp yang ada, jangan mengarang. Prioritaskan hook kuat, informasi, kejutan, emosi atau punchline. "
                "Kembalikan JSON dengan bentuk {\"clips\":[{\"start\":1.2,\"end\":28.0,\"title\":\"judul\",\"hook\":\"hook\",\"reason\":\"alasan\"}]}. "
                "Transkrip:\n"+transcript[:18000])
        ans=client.chat.completions.create(model="gpt-4o-mini",
            messages=[{"role":"system","content":"Kamu editor video pendek. Jawab JSON valid saja."},{"role":"user","content":prompt}],
            response_format={"type":"json_object"},temperature=.4)
        clips=json.loads(ans.choices[0].message.content).get("clips",[])
        meta=json.loads(run(["ffprobe","-v","quiet","-print_format","json","-show_format",str(src)]))
        duration=float(meta["format"]["duration"]); results=[]
        for i,c in enumerate(clips[:count],1):
            start=max(0,min(float(c.get("start",0)),duration))
            end=min(max(start+3,float(c.get("end",start+25))),duration,start+65)
            if end<=start: continue
            name=f"{jid}_{i}"; mp4=OUT/f"{name}.mp4"; srt=OUT/f"{name}.srt"
            cues=[]; n=1
            for seg in segs:
                a=max(start,seg["start"]); b=min(end,seg["end"])
                if b>a: cues.append(f"{n}\n{stime(a-start)} --> {stime(b-start)}\n{seg['text']}\n"); n+=1
            srt.write_text("\n".join(cues),encoding="utf-8")
            vf=f"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1,subtitles={srt}:force_style='FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=65'"
            run(["ffmpeg","-y","-ss",str(start),"-i",str(src),"-t",str(end-start),"-vf",vf,
                 "-c:v","libx264","-preset","veryfast","-crf","24","-c:a","aac","-b:a","128k","-movflags","+faststart",str(mp4)])
            results.append({"title":str(c.get("title","Clip "+str(i))),"hook":str(c.get("hook","")),
                "reason":str(c.get("reason","")),"start":round(start,1),"end":round(end,1),
                "mp4":f"/outputs/{name}.mp4","srt":f"/outputs/{name}.srt"})
        return {"clips":results}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,"Proses gagal: "+str(e)[:700])
    finally:
        await file.close()
        src.unlink(missing_ok=True); aud.unlink(missing_ok=True)
