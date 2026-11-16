import os, re, json, html, time, sqlite3, hashlib
from typing import List
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TARAMA_SURESI = 1800

if not GEMINI_API_KEY:
    raise ValueError("HATA: GEMINI_API_KEY eksik! Settings > Secrets > Actions kismindan ekleyin.")

TR_SAAT = timezone(timedelta(hours=3))
SU_AN = datetime.now(TR_SAAT)
SEANS = "Sabah Seansi" if SU_AN.hour < 15 else "Aksam Seansi"

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

def web_sayfasi_guncelle(yeni_ilanlar: List[IlanKarti], ozet: str):
    veri_dosyasi = "jobs_data.json"
    mevcut_liste = []
    if os.path.exists(veri_dosyasi):
        try:
            with open(veri_dosyasi, "r", encoding="utf-8") as f:
                mevcut_liste = json.load(f)
        except Exception:
            mevcut_liste = []

    tarih_etiket = datetime.now(TR_SAAT).strftime("%d.%m.%Y %H:%M")
    for ilan in yeni_ilanlar:
        kayit = ilan.model_dump()
        kayit["eklenme_tarihi"] = f"{tarih_etiket} ({SEANS})"
        mevcut_liste.insert(0, kayit)

    # Son 100 ilani panelde tut
    mevcut_liste = mevcut_liste[:100]
    with open(veri_dosyasi, "w", encoding="utf-8") as f:
        json.dump(mevcut_liste, f, ensure_ascii=False, indent=2)

    kartlar_html = ""
    for idx, x in enumerate(mevcut_liste, 1):
        puan = x.get("uygunluk_puani", 7)
        rozet_renk = "#16a34a" if puan >= 8 else "#ca8a04"
        g_baslik = html.escape(x.get("baslik", ""))
        g_plat = html.escape(x.get("platform", ""))
        g_butce = html.escape(x.get("tahmini_butce_sure", ""))
        g_neden = html.escape(x.get("neden_uygun", ""))
        g_teklif = html.escape(x.get("teklif_metni", ""))
        g_url = html.escape(x.get("url", "#"))
        g_tarih = html.escape(x.get("eklenme_tarihi", ""))

        kartlar_html += f"""
        <div class="card" data-platform="{g_plat.lower()}">
            <div class="card-top">
                <div>
                    <span class="badge-plat">{g_plat}</span>
                    <span class="badge-score" style="background:{rozet_renk};">★ {puan}/10</span>
                </div>
                <span class="date-tag">{g_tarih}</span>
            </div>
            <h3>{g_baslik}</h3>
            <div class="budget">💰 {g_butce}</div>
            <p class="desc">{g_neden}</p>
            <div class="proposal-box">
                <div class="prop-header">
                    <span>Hazir Teklif Metni</span>
                    <button onclick="kopyala(this)" class="btn-copy">Kopyala</button>
                </div>
                <pre>{g_teklif}</pre>
            </div>
            <div class="card-actions">
                <a href="{g_url}" target="_blank" class="btn-apply">Ilana Git & Basvur →</a>
            </div>
        </div>
        """

    if not kartlar_html:
        kartlar_html = '<div class="empty">Henuz yeni ilan yakalanmadi. Bir sonraki otomatik taramada burasi dolacaktir.</div>'

    sayfa_html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Otonom Freelance Is Panosu</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
  .container {{ max-width: 900px; margin: auto; }}
  .header {{ background: linear-gradient(135deg, #1e293b, #334155); padding: 24px; border-radius: 16px; margin-bottom: 24px; border: 1px solid #475569; }}
  .header h1 {{ margin: 0 0 8px 0; font-size: 24px; color: #38bdf8; }}
  .header p {{ margin: 0; color: #cbd5e1; font-size: 14px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; margin-bottom: 18px; }}
  .card-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
  .badge-plat {{ background: #0284c7; color: #fff; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; text-transform: uppercase; }}
  .badge-score {{ color: #fff; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; margin-left: 6px; }}
  .date-tag {{ font-size: 12px; color: #94a3b8; }}
  h3 {{ margin: 8px 0 12px 0; font-size: 18px; color: #f1f5f9; }}
  .budget {{ background: #0f172a; padding: 8px 12px; border-radius: 8px; font-size: 13px; color: #38bdf8; margin-bottom: 12px; border-left: 3px solid #38bdf8; }}
  .desc {{ font-size: 14px; color: #cbd5e1; line-height: 1.5; margin-bottom: 14px; }}
  .proposal-box {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 12px; margin-bottom: 16px; }}
  .prop-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 12px; color: #94a3b8; }}
  pre {{ margin: 0; white-space: pre-wrap; font-family: monospace; font-size: 13px; color: #e2e8f0; }}
  .btn-copy {{ background: #334155; color: #fff; border: none; padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }}
  .btn-copy:hover {{ background: #475569; }}
  .card-actions {{ text-align: right; }}
  .btn-apply {{ background: #2563eb; color: #fff; text-decoration: none; padding: 10px 20px; border-radius: 8px; font-weight: 700; font-size: 14px; display: inline-block; }}
  .btn-apply:hover {{ background: #1d4ed8; }}
  .empty {{ text-align: center; padding: 40px; background: #1e293b; border-radius: 12px; color: #94a3b8; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Otonom CAD/CAM & Kalip Muhendisligi Is Panosu</h1>
    <p>Son Guncelleme: {tarih_etiket} ({SEANS}) | Toplam Arsivlenen Firsat: {len(mevcut_liste)}</p>
    <p style="margin-top:8px; color:#93c5fd;">{html.escape(ozet)}</p>
  </div>
  {kartlar_html}
</div>
<script>
function kopyala(btn) {{
  const metin = btn.parentElement.nextElementSibling.innerText;
  navigator.clipboard.writeText(metin);
  btn.innerText = "Kopyalandi!";
  setTimeout(() => btn.innerText = "Kopyala", 2000);
}}
</script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(sayfa_html)

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

    if not bulunanlar:
        web_sayfasi_guncelle([], "Son taramada kriterlerinize uyan yeni bir ilan tespit edilmedi.")
        return

    t_analiz = Task(description=f"Sadece profilime ({profil}) uyan yeni ilanlari sec ve puanla:\n\n" + "\n---\n".join(bulunanlar)[:25000], expected_output="Secilen ilanlar.", agent=filtre)
    t_rapor = Task(description="Secilen ilanlari RaporSemasi formatinda doldur ve teklif metinlerini yaz.", expected_output="JSON rapor.", output_pydantic=RaporSemasi, agent=raporcu)
    res = Crew(agents=[filtre, raporcu], tasks=[t_analiz, t_rapor]).kickoff()

    if res.pydantic and res.pydantic.ilanlar:
        web_sayfasi_guncelle(res.pydantic.ilanlar, res.pydantic.genel_ozet)
    else:
        web_sayfasi_guncelle([], "Tarama tamamlandi, yeni uygun firsat bulunamadi.")

if __name__ == "__main__":
    calistir()
