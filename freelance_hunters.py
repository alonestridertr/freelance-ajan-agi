import os, re, json, html, time, sqlite3, hashlib
from typing import List
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from crewai import Agent, Task, Crew, LLM

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TARAMA_SURESI = 900  # 15 Dakika (20 sorgunun tamamina kesintisiz ulasmak icin ideal sure)

if not GEMINI_API_KEY:
    raise ValueError("HATA: GEMINI_API_KEY eksik!")

TR_SAAT = timezone(timedelta(hours=3))
SU_AN = datetime.now(TR_SAAT)
SEANS = "Sabah Seansi" if SU_AN.hour < 15 else "Aksam Seansi"

TARAMA_GUNLUGU = []

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

def dogrudan_google_ara(sorgu: str) -> str:
    """CrewAI arac katmanini bypass ederek dogrudan Google Search Grounding ile canli sitelere girer."""
    prompt = (
        f"Google Search kullanarak internetteki su sorguyla ilgili EN GUNCEL freelance is ilanlarini, "
        f"proje taleplerini ve acik pozisyonlari bul:\nSorgu: {sorgu}\n\n"
        f"Buldugun her ilanin tam basligini, platformunu, butce/sure bilgisini ve URL adresini yaz."
    )
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1
            )
        )
        dogrulanmis_kaynaklar = []
        if resp.candidates and resp.candidates[0].grounding_metadata:
            chunks = resp.candidates[0].grounding_metadata.grounding_chunks or []
            for c in chunks:
                if c.web and c.web.uri:
                    dogrulanmis_kaynaklar.append({
                        "baslik": c.web.title or "Web Kaynagi",
                        "url": c.web.uri
                    })

        ham_metin = resp.text or ""
        TARAMA_GUNLUGU.append({
            "saat": datetime.now(TR_SAAT).strftime("%H:%M:%S"),
            "sorgu": sorgu,
            "ulasilan_site_sayisi": len(dogrulanmis_kaynaklar),
            "siteler": dogrulanmis_kaynaklar[:10],
            "ham_ozet": ham_metin[:450] + ("..." if len(ham_metin) > 450 else "")
        })

        link_listesi = "\n".join([f"- {k['baslik']}: {k['url']}" for k in dogrulanmis_kaynaklar])
        return f"SORGU: {sorgu}\nBULGULAR:\n{ham_metin}\n\nDOGRULANMIS LINKLER:\n{link_listesi}"
    except Exception as e:
        hata_str = str(e)
        TARAMA_GUNLUGU.append({
            "saat": datetime.now(TR_SAAT).strftime("%H:%M:%S"),
            "sorgu": sorgu,
            "ulasilan_site_sayisi": 0,
            "siteler": [],
            "ham_ozet": f"API/Arama Hatasi: {hata_str[:300]}"
        })
        return ""

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
        if not metin:
            return ""
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

    mevcut_liste = mevcut_liste[:150]
    with open(veri_dosyasi, "w", encoding="utf-8") as f:
        json.dump(mevcut_liste, f, ensure_ascii=False, indent=2)

    toplam_sorgu = len(TARAMA_GUNLUGU)
    toplam_ulasilan_site = sum(g["ulasilan_site_sayisi"] for g in TARAMA_GUNLUGU)
    gunluk_satirlari = ""
    for g in TARAMA_GUNLUGU:
        site_linkleri = "".join([
            f'<li><a href="{html.escape(s["url"])}" target="_blank" style="color:#38bdf8;text-decoration:none;">{html.escape(s["baslik"])}</a></li>'
            for s in g["siteler"]
        ])
        if not site_linkleri:
            site_linkleri = '<li style="color:#94a3b8;">Bu sorguda dogrudan sayfa eslesmesi donmedi.</li>'

        gunluk_satirlari += f"""
        <div style="background:#0f172a;border:1px solid #334155;border-radius:10px;padding:12px;margin-bottom:10px;">
            <div style="display:flex;justify-content:space-between;font-size:12px;color:#94a3b8;margin-bottom:6px;">
                <span>🕒 {html.escape(g['saat'])} | <b>Sorgu:</b> {html.escape(g['sorgu'])}</span>
                <span style="color:#4ade80;font-weight:700;">✓ {g['ulasilan_site_sayisi']} Siteye Ulasildi</span>
            </div>
            <ul style="margin:6px 0;padding-left:18px;font-size:12px;line-height:1.5;">{site_linkleri}</ul>
            <div style="font-size:12px;color:#cbd5e1;background:#1e293b;padding:8px;border-radius:6px;margin-top:6px;">
                <b>Ham Bulgu Ozeti:</b> {html.escape(g['ham_ozet'])}
            </div>
        </div>
        """

    kartlar_html = ""
    platformlar = set()
    for x in mevcut_liste:
        puan = x.get("uygunluk_puani", 7)
        rozet_renk = "#16a34a" if puan >= 8 else "#ca8a04"
        g_baslik = html.escape(x.get("baslik", ""))
        g_plat = html.escape(x.get("platform", "Diger"))
        platformlar.add(g_plat)
        g_butce = html.escape(x.get("tahmini_butce_sure", ""))
        g_neden = html.escape(x.get("neden_uygun", ""))
        g_teklif = html.escape(x.get("teklif_metni", ""))
        g_url = html.escape(x.get("url", "#"))
        g_tarih = html.escape(x.get("eklenme_tarihi", ""))
        kart_id = hashlib.md5(g_url.encode()).hexdigest()[:10]

        kartlar_html += f"""
        <div class="card" id="card-{kart_id}" data-platform="{g_plat.lower()}">
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
                <button onclick="basvurulduIsaretle('{kart_id}')" class="btn-done" id="btn-{kart_id}">✓ Basvuruldu Isaretle</button>
                <a href="{g_url}" target="_blank" class="btn-apply">Ilana Git & Basvur →</a>
            </div>
        </div>
        """

    if not kartlar_html:
        kartlar_html = '<div class="empty">Henuz yeni ilan yakalanmadi. Yukaridaki Canli Tarama Gunlugunden ajanlarin hangi sitelere girdigini inceleyebilirsiniz.</div>'

    filtre_butonlari = '<button onclick="filtrele(\'hepsi\', this)" class="filter-btn active">Tumu</button>'
    for p in sorted(platformlar):
        filtre_butonlari += f'<button onclick="filtrele(\'{p.lower()}\', this)" class="filter-btn">{p}</button>'

    sayfa_html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Otonom Freelance Is Panosu</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
  .container {{ max-width: 920px; margin: auto; }}
  .header {{ background: linear-gradient(135deg, #1e293b, #334155); padding: 24px; border-radius: 16px; margin-bottom: 18px; border: 1px solid #475569; }}
  .header h1 {{ margin: 0 0 8px 0; font-size: 23px; color: #38bdf8; }}
  .header p {{ margin: 0; color: #cbd5e1; font-size: 14px; }}
  .stats {{ display: flex; gap: 12px; margin-top: 14px; flex-wrap: wrap; }}
  .stat-box {{ background: #0f172a; border: 1px solid #334155; padding: 8px 14px; border-radius: 8px; font-size: 13px; color: #e2e8f0; }}
  details.audit-log {{ background: #1e293b; border: 1px solid #38bdf8; border-radius: 12px; padding: 14px 18px; margin-bottom: 20px; }}
  details.audit-log summary {{ cursor: pointer; font-weight: 700; color: #38bdf8; font-size: 15px; }}
  .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }}
  .filter-btn {{ background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 6px 14px; border-radius: 20px; cursor: pointer; font-size: 13px; }}
  .filter-btn.active, .filter-btn:hover {{ background: #2563eb; color: #fff; border-color: #2563eb; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; margin-bottom: 18px; transition: opacity 0.2s; }}
  .card.applied {{ opacity: 0.45; border-color: #16a34a; }}
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
  .card-actions {{ display: flex; justify-content: space-between; align-items: center; }}
  .btn-done {{ background: #334155; color: #e2e8f0; border: none; padding: 9px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; }}
  .btn-apply {{ background: #2563eb; color: #fff; text-decoration: none; padding: 10px 20px; border-radius: 8px; font-weight: 700; font-size: 14px; }}
  .empty {{ text-align: center; padding: 40px; background: #1e293b; border-radius: 12px; color: #94a3b8; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Otonom CAD/CAM & Kalip Muhendisligi Is Panosu</h1>
    <p>Son Guncelleme: {tarih_etiket} ({SEANS})</p>
    <div class="stats">
      <div class="stat-box">🔍 Yapilan Sorgu: <b>{toplam_sorgu}</b></div>
      <div class="stat-box">🌐 Girilen/Dogrulanan Sayfa: <b>{toplam_ulasilan_site}</b></div>
      <div class="stat-box">🎯 Arsivlenen Firsat: <b>{len(mevcut_liste)}</b></div>
    </div>
    <p style="margin-top:12px; color:#93c5fd;">{html.escape(ozet)}</p>
  </div>

  <details class="audit-log" open>
    <summary>📡 Canli Tarama Gunlugu: Ajanlar Hangi Sitelere Girdi & Ne Buldu? ({toplam_sorgu} Sorgu / {toplam_ulasilan_site} Dogrulanmis Link)</summary>
    <div style="margin-top:12px; max-height:420px; overflow-y:auto;">
      {gunluk_satirlari}
    </div>
  </details>

  <div class="filters">{filtre_butonlari}</div>
  {kartlar_html}
</div>
<script>
function kopyala(btn) {{
  const metin = btn.parentElement.nextElementSibling.innerText;
  navigator.clipboard.writeText(metin);
  btn.innerText = "Kopyalandi!";
  setTimeout(() => btn.innerText = "Kopyala", 2000);
}}
function filtrele(plat, btn) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(c => {{
    c.style.display = (plat === 'hepsi' || c.dataset.platform.includes(plat)) ? 'block' : 'none';
  }});
}}
function basvurulduIsaretle(id) {{
  const card = document.getElementById('card-' + id);
  const btn = document.getElementById('btn-' + id);
  const durum = card.classList.toggle('applied');
  localStorage.setItem('applied_' + id, durum ? '1' : '0');
  btn.innerText = durum ? '✓ Basvuruldu (Geri Al)' : '✓ Basvuruldu Isaretle';
}}
window.onload = () => {{
  document.querySelectorAll('.card').forEach(c => {{
    const id = c.id.replace('card-', '');
    if (localStorage.getItem('applied_' + id) === '1') {{
      c.classList.add('applied');
      const btn = document.getElementById('btn-' + id);
      if (btn) btn.innerText = '✓ Basvuruldu (Geri Al)';
    }}
  }});
}};
</script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(sayfa_html)

SORGULAR = [
    'site:upwork.com/freelance-jobs ("SolidWorks" OR "Sheet Metal" OR "Die Design" OR "Progressive Die")',
    'site:upwork.com/freelance-jobs ("Mastercam" OR "CNC Programming" OR "Reverse Engineering" OR "Tooling")',
    'site:cadcrowd.com ("SolidWorks" OR "Sheet Metal" OR "Die Design" OR "Reverse Engineering" OR "CNC")',
    'site:linkedin.com/jobs ("SolidWorks" OR "Sheet Metal" OR "Tool and Die" OR "Mastercam") ("contract" OR "freelance" OR "remote")',
    'site:linkedin.com/posts ("looking for" OR "hiring" OR "need") ("SolidWorks" OR "sheet metal" OR "progressive die" OR "Mastercam")',
    'site:freelancer.com/projects ("CNC" OR "Mastercam" OR "SolidWorks" OR "Sheet Metal" OR "Stamping")',
    'site:guru.com/d/jobs ("SolidWorks" OR "Sheet Metal" OR "CAD" OR "CNC" OR "Tooling")',
    'site:peopleperhour.com/freelance-jobs ("SolidWorks" OR "CAD" OR "Sheet Metal" OR "3D Modeling")',
    'site:grabcad.com ("challenge" OR "job" OR "freelance" OR "sheet metal" OR "tooling")',
    'site:reddit.com ("forhire" OR "hiring" OR "looking for") ("SolidWorks" OR "sheet metal" OR "CNC" OR "CAD designer")',
    '"freelance" ("sheet metal die design" OR "progressive die" OR "stamping die" OR "transfer die")',
    '"freelance" ("Logopress" OR "3D QuickPress" OR "sheet metal tooling" OR "forming die")',
    '"freelance" ("Mastercam" OR "5 axis CAM" OR "CNC programming" OR "G-code")',
    '"freelance" ("reverse engineering" OR "scan to CAD" OR "STEP file" OR "STL to SolidWorks")',
    'site:bionluk.com ("SolidWorks" OR "3D cizim" OR "kalip tasarim" OR "CNC" OR "sac metal")',
    '("sac kalip tasarimi" OR "progresif kalip" OR "kesme kalibi" OR "bukme kalibi") freelance proje'
]

def calistir():
    db = IlanDB()
    profil = "Sac Metal Kalip Tasarimi (Progresif, Transfer, Kesme/Bukme), SolidWorks, Logopress, Mastercam CNC CAM, Aparat/Fikstur ve Tersine Muhendislik."

    bulunanlar = []
    print(f"[{datetime.now(TR_SAAT).strftime('%H:%M:%S')}] Dogrudan Google Search Grounding taramasi basladi...")

    for s in SORGULAR:
        ham_sonuc = dogrudan_google_ara(s)
        filtrelenmis = db.filtrele(ham_sonuc)
        if filtrelenmis:
            bulunanlar.append(filtrelenmis)
        time.sleep(12)  # 16 sorgu x 12 sn = ~3.5 dakikada tam tarama tamamlanir

    if not bulunanlar:
        web_sayfasi_guncelle([], "Tum sorgular tamamlandi. Yukaridaki Canli Tarama Gunlugunden girilen siteleri ve ham bulgulari inceleyebilirsiniz.")
        return

    llm = LLM(model="gemini/gemini-2.5-flash", api_key=GEMINI_API_KEY)
    filtre = Agent(role="Bas Kalip ve Uretim Muhendisi", goal="Profilime uyan gercek ve aktif ilanlari secmek.", backstory=f"Uzmanlik: {profil}", llm=llm, verbose=True)
    raporcu = Agent(role="Kidemli Teklif Stratejisti", goal="Secilen ilanlar icin yuksek donusumlu teknik teklif metinleri hazirlamak.", backstory="Is gelistirme uzmani.", llm=llm, verbose=True)

    t_analiz = Task(
        description=f"Asagidaki canli Google Search bulgularini incele. Sadece profilime ({profil}) uyan gercek is firsatlarini sec, tam URL'lerini koru ve 10 uzerinden puanla:\n\n" + "\n---\n".join(bulunanlar)[:35000],
        expected_output="Secilen ilanlar ve URL listesi.",
        agent=filtre
    )
    t_rapor = Task(
        description="Secilen ilanlari RaporSemasi formatinda doldur ve teklif metinlerini yaz (Ingilizce ilanlara Ingilizce, Turkce ilanlara Turkce).",
        expected_output="JSON rapor.",
        output_pydantic=RaporSemasi,
        agent=raporcu
    )
    res = Crew(agents=[filtre, raporcu], tasks=[t_analiz, t_rapor]).kickoff()

    if res.pydantic and res.pydantic.ilanlar:
        web_sayfasi_guncelle(res.pydantic.ilanlar, res.pydantic.genel_ozet)
    else:
        web_sayfasi_guncelle([], "Tarama tamamlandi. Yukaridaki Canli Tarama Gunlugunden detaylari inceleyebilirsiniz.")

if __name__ == "__main__":
    calistir()
