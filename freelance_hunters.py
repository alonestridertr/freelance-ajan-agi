import os, re, html, time, sqlite3, hashlib, smtplib
from typing import List
from pydantic import BaseModel
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_GONDEREN = os.environ.get("GMAIL_GONDEREN")
GMAIL_UYGULAMA_SIFRESI = os.environ.get("GMAIL_UYGULAMA_SIFRESI")
RAPOR_ALICI_EMAIL = os.environ.get("RAPOR_ALICI_EMAIL")
TARAMA_SURESI = 1800

if not all([GEMINI_API_KEY, GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI, RAPOR_ALICI_EMAIL]):
    raise ValueError("HATA: GitHub Secrets degiskenlerinden biri eksik!")

TR_SAAT = timezone(timedelta(hours=3))
SEANS = "Sabah Seansi" if datetime.now(TR_SAAT).hour < 15 else "Aksam Seansi"

class IlanKarti(BaseModel):
    baslik: str
    platform: str
    url: str
    uygunluk_puani: int
    tahmini_butce_sure: str
    neden_uygun: str
    teklif_metni: str

class RaporSemasi(BaseModel):
    genel_ozet: str
    ilanlar: List[IlanKarti]

client = genai.Client(api_key=GEMINI_API_KEY)

@tool("Canli Google Is Ilani Arama")
def google_ilan_ara(sorgu: str) -> str:
    """Google Search ile guncel freelance is ilanlarini ve URL linklerini bulur."""
    prompt = f"Su sorguyla Upwork, LinkedIn, Freelancer, Bionluk vb. sitelerdeki en guncel freelance ilanlarini baslik, butce ve tam URL linkiyle listele: {sorgu}"
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(tools=[{"google_search": {}}], temperature=0.1)
        )
        linkler = []
        if resp.candidates and resp.candidates[0].grounding_metadata:
            for c in (resp.candidates[0].grounding_metadata.grounding_chunks or []):
                if c.web and c.web.uri:
                    linkler.append(f"- {c.web.title}: {c.web.uri}")
        return f"{resp.text}\n\nKAYNAKLAR:\n" + "\n".join(linkler)
    except Exception as e:
        return f"Arama hatasi: {e}"

class IlanDB:
    def __init__(self, db="seen_jobs.db"):
        self.conn = sqlite3.connect(db)
        self.c = self.conn.cursor()
        self.c.execute("CREATE TABLE IF NOT EXISTS ilanlar (h TEXT PRIMARY KEY, url TEXT, tarih TEXT)")
        self.conn.commit()

    def yeni_mi(self, u: str) -> bool:
        h = hashlib.sha256(u.strip().lower().encode()).hexdigest()
        self.c.execute("SELECT 1 FROM ilanlar WHERE h=?", (h,))
        if self.c.fetchone() is None:
            self.c.execute("INSERT INTO ilanlar VALUES (?,?,?)", (h, u, str(datetime.now(TR_SAAT))))
            self.conn.commit()
            return True
        return False

    def filtrele(self, metin: str) -> str:
        urller = re.findall(r'https?://[^\s\)\]\>"]+', metin)
        yeniler = [u.split("?")[0].rstrip("/") for u in urller if self.yeni_mi(u.split("?")[0].rstrip("/"))]
        return ("\n".join(yeniler) + "\n\n" + metin) if yeniler else ""

def eposta_at(konu: str, html_icerik: str, duz_icerik: str):
    msg = MIMEMultipart("alternative")
    msg["From"], msg["To"], msg["Subject"] = GMAIL_GONDEREN, RAPOR_ALICI_EMAIL, konu
    msg.attach(MIMEText(duz_icerik, "plain", "utf-8"))
    msg.attach(MIMEText(html_icerik, "html", "utf-8"))
    s = smtplib.SMTP("smtp.gmail.com", 587)
    s.starttls()
    s.login(GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI.replace(" ", ""))
    s.send_message(msg)
    s.quit()

def html_olustur(data: RaporSemasi, tarih: str) -> str:
    kartlar = ""
    for i, x in enumerate(sorted(data.ilanlar, key=lambda a: a.uygunluk_puani, reverse=True), 1):
        renk = "#dcfce7" if x.uygunluk_puani >= 8 else "#fef9c3"
        kartlar += (
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:16px;">'
            f'<span style="background:#eff6ff;color:#1d4ed8;padding:3px 8px;border-radius:5px;font-size:12px;font-weight:700;">{html.escape(x.platform)}</span> '
            f'<span style="background:{renk};padding:3px 8px;border-radius:5px;font-size:12px;font-weight:700;">★ {x.uygunluk_puani}/10</span>'
            f'<h3 style="margin:10px 0;">{i}. {html.escape(x.baslik)}</h3>'
            f'<p style="font-size:13px;color:#334155;"><b>Butce/Sure:</b> {html.escape(x.tahmini_butce_sure)}</p>'
            f'<p style="font-size:14px;color:#475569;">{html.escape(x.neden_uygun)}</p>'
            f'<pre style="background:#f1f5f9;padding:10px;border-radius:6px;white-space:pre-wrap;font-size:13px;">{html.escape(x.teklif_metni)}</pre>'
            f'<a href="{html.escape(x.url)}" style="background:#2563eb;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none;display:inline-block;font-weight:700;">Ilana Git →</a></div>'
        )
    return f'<div style="max-width:650px;margin:auto;font-family:sans-serif;background:#f8fafc;padding:16px;"><h2>Freelance Raporu ({tarih} - {SEANS})</h2><p>{html.escape(data.genel_ozet)}</p>{kartlar}</div>'

SORGULAR = [
    'site:upwork.com/freelance-jobs ("SolidWorks" OR "Sheet Metal" OR "Die Design")',
    'site:linkedin.com/jobs ("SolidWorks" OR "Sheet Metal" OR "Tool and Die") freelance',
    'site:linkedin.com/posts ("looking for" OR "hiring") ("SolidWorks" OR "sheet metal" OR "Mastercam")',
    'site:freelancer.com/projects ("CNC" OR "Mastercam" OR "SolidWorks" OR "Sheet Metal")',
    '"freelance" ("sheet metal die design" OR "progressive die" OR "stamping die")',
    '"freelance" ("Mastercam" OR "5 axis CAM" OR "CNC programming")',
    'site:bionluk.com ("SolidWorks" OR "3D cizim" OR "kalip tasarim" OR "CNC")',
    '("tersine muhendislik" OR "sac kalip tasarimi" OR "progresif kalip") freelance'
]

def calistir():
    llm = LLM(model="gemini/gemini-2.5-flash", api_key=GEMINI_API_KEY)
    db = IlanDB()
    profil = "Sac Metal Kalip Tasarimi (Progresif, Transfer), SolidWorks, Logopress, Mastercam CNC CAM, Tersine Muhendislik."

    avci = Agent(role="Ilan Avcisi", goal="Guncel CAD/CAM ve kalip ilanlarini bulmak.", backstory="Veri madencisi.", tools=[google_ilan_ara], llm=llm, verbose=True)
    filtre = Agent(role="Kalip Muhendisi", goal="Profilime uyan yeni ilanlari secmek.", backstory=f"Uzmanlik: {profil}", llm=llm, verbose=True)
    raporcu = Agent(role="Teklif Yazari", goal="Secilen ilanlar icin rapor ve teklif hazirlamak.", backstory="Is gelistirme uzmani.", llm=llm, verbose=True)

    bas = time.time()
    bulunanlar = []
    while (time.time() - bas) < TARAMA_SURESI:
        for s in SORGULAR:
            if (time.time() - bas) >= TARAMA_SURESI:
                break
            t = Task(description=f"Google ile ara ve ilan basliklariyla tam URL linklerini listele: {s}", expected_output="Ilan listesi ve URLler.", agent=avci)
            try:
                sonuc = db.filtrele(str(Crew(agents=[avci], tasks=[t]).kickoff()))
                if sonuc:
                    bulunanlar.append(sonuc)
            except Exception as e:
                print(e)
            time.sleep(75)

    tarih = datetime.now(TR_SAAT).strftime("%d.%m.%Y")
    if not bulunanlar:
        eposta_at(f"Freelance Ajan ({tarih} - {SEANS}) - Yeni Ilan Yok", f"<p>{tarih} ({SEANS}) taramasinda yeni ilan bulunamadi.</p>", "Yeni ilan bulunamadi.")
        return

    t_analiz = Task(description=f"Sadece profilime ({profil}) uyan yeni ilanlari sec ve puanla:\n\n" + "\n---\n".join(bulunanlar)[:25000], expected_output="Secilen ilanlar.", agent=filtre)
    t_rapor = Task(description="Secilen ilanlari RaporSemasi formatinda doldur ve teklif metinlerini yaz.", expected_output="JSON rapor.", output_pydantic=RaporSemasi, agent=raporcu)
    res = Crew(agents=[filtre, raporcu], tasks=[t_analiz, t_rapor]).kickoff()

    if res.pydantic and res.pydantic.ilanlar:
        eposta_at(f"{len(res.pydantic.ilanlar)} Yeni Freelance Firsati ({tarih} - {SEANS})", html_olustur(res.pydantic, tarih), str(res.pydantic))
    else:
        eposta_at(f"Freelance Raporu ({tarih} - {SEANS})", f"<pre>{html.escape(str(res))}</pre>", str(res))

if __name__ == "__main__":
    calistir()
