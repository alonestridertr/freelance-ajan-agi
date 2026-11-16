import os
import re
import html
import time
import sqlite3
import hashlib
import smtplib
from typing import List
from pydantic import BaseModel, Field
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool

# ==============================================================================
# 1. GITHUB SECRETS ORTAM DEGISKENLERI
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_GONDEREN = os.environ.get("GMAIL_GONDEREN")
GMAIL_UYGULAMA_SIFRESI = os.environ.get("GMAIL_UYGULAMA_SIFRESI")
RAPOR_ALICI_EMAIL = os.environ.get("RAPOR_ALICI_EMAIL")
TARAMA_SURESI_SANIYE = 1800

if not all([GEMINI_API_KEY, GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI, RAPOR_ALICI_EMAIL]):
    raise ValueError("HATA: GitHub Secrets degiskenlerinden biri eksik!")

TR_SAAT_DILIMI = timezone(timedelta(hours=3))
su_an_tr = datetime.now(TR_SAAT_DILIMI)
SEANS_ADI = "Sabah Seansi" if su_an_tr.hour < 15 else "Aksam Seansi"

# ==============================================================================
# 2. YAPILANDIRILMIS RAPOR SEMASI
# ==============================================================================
class FreelanceIlanKarti(BaseModel):
    baslik: str = Field(description="Ilanin tam basligi")
    platform: str = Field(description="Yayinlandigi platform")
    url: str = Field(description="Ilanin dogrudan basvuru URL linki")
    uygunluk_puani: int = Field(description="1 ile 10 arasi uygunluk puani")
    tahmini_butce_sure: str = Field(description="Tahmini butce ve teslim suresi")
    neden_uygun: str = Field(description="Teknik uygunluk ozeti")
    teklif_metni: str = Field(description="Musteriye gonderilecek hazir teklif metni")

class GunlukRaporSemasi(BaseModel):
    genel_ozet: str = Field(description="Bu seanstaki firsatlarin ozeti")
    ilanlar: List[FreelanceIlanKarti] = Field(description="Onaylanan ilanlar listesi")

# ==============================================================================
# 3. GOOGLE AI STUDIO CANLI WEB ARAMA ARACI
# ==============================================================================
genai_client = genai.Client(api_key=GEMINI_API_KEY)

@tool("Canli Google Is Ilani Arama")
def canli_google_arama(arama_sorgusu: str) -> str:
    """Google Search altyapisini kullanarak guncel freelance is ilanlarini bulur."""
    prompt = (
        "Asagidaki arama sorgusunu kullanarak internetteki (Upwork, LinkedIn, Freelancer, Fiverr, Bionluk vb.) "
        "EN GUNCEL freelance proje ve is ilanlarini bul. Her ilanin Tam Basligini, Platformunu, "
        "Butce/Sure bilgisini, Kisa teknik ozetini ve DOGRUDAN URL BAGLANTISINI listele.\n"
        f"Arama Sorgusu: {arama_sorgusu}"
    )
    try:
        response = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.1
            )
        )
        kaynak_urller = []
        if response.candidates and response.candidates[0].grounding_metadata:
            chunks = response.candidates[0].grounding_metadata.grounding_chunks or []
            for chunk in chunks:
                if chunk.web and chunk.web.uri:
                    kaynak_urller.append(f"- {chunk.web.title}: {chunk.web.uri}")
        ek_linkler = "\n".join(kaynak_urller)
        return f"{response.text}\n\nDOGRULANMIS KAYNAK LINKLERI:\n{ek_linkler}"
    except Exception as e:
        return f"Arama sirasinda hata olustu: {e}"

# ==============================================================================
# 4. TEKRAR ONLEYICI VERITABANI
# ==============================================================================
class TekrarOnleyiciDB:
    def __init__(self, db_yolu="seen_jobs.db"):
        self.conn = sqlite3.connect(db_yolu)
        self.cursor = self.conn.cursor()
        self.cursor.execute(
            "CREATE TABLE IF NOT EXISTS bulunan_ilanlar (ilan_hash TEXT PRIMARY KEY, url TEXT, bulunma_tarihi TEXT)"
        )
        self.conn.commit()

    def _hash_olustur(self, metin: str) -> str:
        return hashlib.sha256(metin.strip().lower().encode("utf-8")).hexdigest()

    def yeni_mi(self, url_veya_baslik: str) -> bool:
        h = self._hash_olustur(url_veya_baslik)
        self.cursor.execute("SELECT 1 FROM bulunan_ilanlar WHERE ilan_hash = ?", (h,))
        return self.cursor.fetchone() is None

    def kaydet(self, url_veya_baslik: str):
        h = self._hash_olustur(url_veya_baslik)
        tarih = datetime.now(TR_SAAT_DILIMI).strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            "INSERT OR IGNORE INTO bulunan_ilanlar (ilan_hash, url, bulunma_tarihi) VALUES (?, ?, ?)",
            (h, url_veya_baslik, tarih)
        )
        self.conn.commit()

    def metni_filtrele(self, ham_cikti: str) -> str:
        bulunan_urller = re.findall(r'https?://[^\s\)\]\>"]+', ham_cikti)
        yeni_urller = []
        for url in bulunan_urller:
            temiz_url = url.split("?")[0].rstrip("/")
            if self.yeni_mi(temiz_url):
                self.kaydet(temiz_url)
                yeni_urller.append(temiz_url)
        if not bulunan_urller:
            if self.yeni_mi(ham_cikti[:200]):
                self.kaydet(ham_cikti[:200])
                return ham_cikti
            return ""
        if not yeni_urller:
            return ""
        return "YENI TESPIT EDILEN ILAN LINKLERI:\n" + "\n".join(yeni_urller) + f"\n\nDETAYLAR:\n{ham_cikti}"

# ==============================================================================
# 5. HTML E-POSTA SABLON OLUSTURUCU VE GONDERICI
# ==============================================================================
def html_rapor_olustur(tarih_str: str, seans: str, rapor_verisi: GunlukRaporSemasi) -> str:
    kartlar_html = ""
    sirali_ilanlar = sorted(rapor_verisi.ilanlar, key=lambda x: x.uygunluk_puani, reverse=True)

    for idx, ilan in enumerate(sirali_ilanlar, 1):
        if ilan.uygunluk_puani >= 8:
            rozet_bg, rozet_renk, rozet_etiket = "#dcfce7", "#166534", f"★ {ilan.uygunluk_puani}/10 Yuksek Uyum"
        elif ilan.uygunluk_puani >= 6:
            rozet_bg, rozet_renk, rozet_etiket = "#fef9c3", "#854d0e", f"● {ilan.uygunluk_puani}/10 Orta Uyum"
        else:
            rozet_bg, rozet_renk, rozet_etiket = "#f1f5f9", "#475569", f"○ {ilan.uygunluk_puani}/10 Alternatif"

        g_baslik = html.escape(ilan.baslik)
        g_platform = html.escape(ilan.platform)
        g_butce = html.escape(ilan.tahmini_butce_sure)
        g_neden = html.escape(ilan.neden_uygun)
        g_teklif = html.escape(ilan.teklif_metni).replace("\n", "<br>")
        g_url = html.escape(ilan.url)

        kartlar_html += (
            f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-bottom:20px;">'
            f'<div style="margin-bottom:10px;">'
            f'<span style="background:#eff6ff;color:#1d4ed8;font-size:12px;font-weight:700;padding:4px 10px;border-radius:6px;margin-right:8px;">{g_platform}</span>'
            f'<span style="background:{rozet_bg};color:{rozet_renk};font-size:12px;font-weight:700;padding:4px 10px;border-radius:6px;">{rozet_etiket}</span>'
            f'</div>'
            f'<h2 style="margin:0 0 10px 0;font-size:18px;color:#0f172a;">{idx}. {g_baslik}</h2>'
            f'<div style="background:#f8fafc;border-left:3px solid #3b82f6;padding:10px 14px;margin-bottom:14px;font-size:13px;color:#334155;">'
            f'<strong>Tahmini Butce & Sure:</strong> {g_butce}</div>'
            f'<p style="margin:0 0 14px 0;font-size:14px;color:#475569;"><strong>Teknik Degerlendirme:</strong> {g_neden}</p>'
            f'<div style="background:#f1f5f9;border:1px dashed #94a3b8;border-radius:8px;padding:12px;font-family:monospace;font-size:13px;color:#1e293b;margin-bottom:16px;">{g_teklif}</div>'
            f'<div style="text-align:right;"><a href="{g_url}" target="_blank" style="background:#2563eb;color:#fff;text-decoration:none;font-weight:700;font-size:14px;padding:10px 20px;border-radius:8px;display:inline-block;">Ilana Git & Basvur →</a></div>'
            f'</div>'
        )

    return (
        f'<!DOCTYPE html><html lang="tr"><body style="margin:0;padding:0;background:#f1f5f9;font-family:sans-serif;">'
        f'<div style="max-width:680px;margin:24px auto;padding:0 16px;">'
        f'<div style="background:#0f172a;color:#fff;padding:24px;border-radius:14px;margin-bottom:20px;">'
        f'<h1 style="margin:0 0 8px 0;font-size:22px;">Yeni Freelance Firsatlari Raporu</h1>'
        f'<div style="font-size:13px;color:#cbd5e1;">{tarih_str} | {seans} | {len(sirali_ilanlar)} Yeni Firsat</div></div>'
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px 20px;margin-bottom:20px;">'
        f'<p style="margin:0;font-size:14px;color:#475569;">{html.escape(rapor_verisi.genel_ozet)}</p></div>'
        f'{kartlar_html}</div></body></html>'
    )

def eposta_gonder_html(konu: str, html_icerik: str, duz_metin_yedek: str):
    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_GONDEREN
    msg["To"] = RAPOR_ALICI_EMAIL
    msg["Subject"] = konu
    msg.attach(MIMEText(duz_metin_yedek, "plain", "utf-8"))
    msg.attach(MIMEText(html_icerik, "html", "utf-8"))
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI.replace(" ", ""))
        server.send_message(msg)
        server.quit()
        print("HTML Rapor basariyla gonderildi!")
    except Exception as e:
        print(f"E-posta gonderim hatasi: {e}")

# ==============================================================================
# 6. UZMANLIK PROFILI VE AJANLAR
# ==============================================================================
UZMANLIK_PROFILI = (
    "Ana Uzmanlik: Sac Metal Kalip Tasarimi (Progresif, Transfer, Kesme/Bukme Kaliplari). "
    "Yazilimlar: SolidWorks, Logopress, Mastercam (CNC Freze/Torna CAM), Blender. "
    "Ek Yetkinlikler: Tersine Muhendislik, Ozel Makine/Aparat Tasarimi, 3D Baski Prototipleme."
)

ARAMA_KATEGORI_LISTESI = [
    'site:upwork.com/freelance-jobs ("SolidWorks" OR "Sheet Metal" OR "Die Design")',
    'site:linkedin.com/jobs ("SolidWorks" OR "Sheet Metal" OR "Tool and Die") ("contract" OR "freelance")',
    'site:linkedin.com/posts ("looking for" OR "hiring") ("SolidWorks" OR "sheet metal" OR "Mastercam")',
    'site:freelancer.com/projects ("CNC" OR "Mastercam" OR "SolidWorks" OR "Sheet Metal")',
    '"freelance" ("sheet metal die design" OR "progressive die" OR "stamping die") project',
    '"freelance" ("Mastercam" OR "5 axis CAM" OR "CNC programming") remote',
    'site:bionluk.com ("SolidWorks" OR "3D cizim" OR "kalip tasarim" OR "CNC")',
    '("tersine muhendislik" OR "sac kalip tasarimi" OR "progresif kalip") freelance proje'
]

def ana_calistirici():
    gemini_llm = LLM(model="gemini/gemini-2.5-flash", api_key=GEMINI_API_KEY)
    db = TekrarOnleyiciDB()

    avci_ajan = Agent(
        role="Kidemli Freelance Ilan Arast
