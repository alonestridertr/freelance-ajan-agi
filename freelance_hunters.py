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
# 1. GITHUB SECRETS ORTAM DEĞİŞKENLERİ
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_GONDEREN = os.environ.get("GMAIL_GONDEREN")
GMAIL_UYGULAMA_SIFRESI = os.environ.get("GMAIL_UYGULAMA_SIFRESI")
RAPOR_ALICI_EMAIL = os.environ.get("RAPOR_ALICI_EMAIL")
TARAMA_SURESI_SANIYE = 1800  # 30 Dakika = 1800 saniye

if not all([GEMINI_API_KEY, GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI, RAPOR_ALICI_EMAIL]):
    raise ValueError("HATA: GitHub Secrets değişkenlerinden biri eksik! Lütfen depo ayarlarını kontrol edin.")

TR_SAAT_DILIMI = timezone(timedelta(hours=3))
su_an_tr = datetime.now(TR_SAAT_DILIMI)
SEANS_ADI = "Sabah Seansı" if su_an_tr.hour < 15 else "Akşam Seansı"

# ==============================================================================
# 2. YAPILANDIRILMIŞ RAPOR ŞEMASI (HTML KARTLAR İÇİN)
# ==============================================================================
class FreelanceIlanKarti(BaseModel):
    baslik: str = Field(description="İlanın tam başlığı")
    platform: str = Field(description="Yayınlandığı platform (örn: Upwork, LinkedIn, Bionluk)")
    url: str = Field(description="İlanın doğrudan başvuru URL linki")
    uygunluk_puani: int = Field(description="1 ile 10 arasında teknik uygunluk puanı")
    tahmini_butce_sure: str = Field(description="Tahmini bütçe ve teslim süresi (örn: $600 - $1,000 | 5-7 Gün)")
    neden_uygun: str = Field(description="Bu ilanın neden seçildiğini anlatan 2 cümlelik teknik özet")
    teklif_metni: str = Field(description="Müşteriye gönderilecek ikna edici hazır teklif metni (İngilizce ilanlara İngilizce, Türkçe ilanlara Türkçe)")

class GunlukRaporSemasi(BaseModel):
    genel_ozet: str = Field(description="Bu seanstaki fırsatların 2 cümlelik yönetici özeti")
    ilanlar: List[FreelanceIlanKarti] = Field(description="Onaylanan en iyi freelance ilanları listesi")

# ==============================================================================
# 3. GOOGLE AI STUDIO CANLI WEB ARAMA ARACI
# ==============================================================================
genai_client = genai.Client(api_key=GEMINI_API_KEY)

@tool("Canlı Google İş İlanı Arama")
def canli_google_arama(arama_sorgusu: str) -> str:
    """Google Search altyapısını kullanarak internetteki en güncel freelance iş ilanlarını ve doğrudan URL linklerini bulur."""
    prompt = f"""
    Aşağıdaki arama sorgusunu kullanarak internetteki (Upwork, LinkedIn, Freelancer, Fiverr, Bionluk vb.) 
    EN GÜNCEL (tercihen son 24 saatte yayınlanmış) freelance proje ve iş ilanlarını bul.
    Her ilanın:
    1) Tam Başlığını
    2) Yayınlandığı Platformu
    3) Bütçe/Süre bilgisini (varsa)
    4) Kısa teknik özetini
    5) DOĞRUDAN URL BAĞLANTISINI
    eksiksiz listele. Sadece gerçek ve aktif ilanları getir.
    
    Arama Sorgusu: {arama_sorgusu}
    """
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
        return f"{response.text}\n\nDOĞRULANMIŞ KAYNAK LİNKLERİ:\n{ek_linkler}"
    except Exception as e:
        return f"Arama sırasında hata oluştu: {e}"

# ==============================================================================
# 4. TEKRAR ÖNLEYİCİ VERİTABANI (SQLITE DEDUPLICATION)
# ==============================================================================
class TekrarOnleyiciDB:
    def __init__(self, db_yolu="seen_jobs.db"):
        self.conn = sqlite3.connect(db_yolu)
        self.cursor = self.conn.cursor()
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS bulunan_ilanlar (
                ilan_hash TEXT PRIMARY KEY,
                url TEXT,
                bulunma_tarihi TEXT
            )
        """)
        self.conn.commit()

    def _hash_olustur(self, metin: str) -> str:
        temiz = metin.strip().lower()
        return hashlib.sha256(temiz.encode('utf-8')).hexdigest()

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
            temiz_url = url.split('?')[0].rstrip('/')
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
            
        return "YENİ TESPİT EDİLEN İLAN LİNKLERİ:\n" + "\n".join(yeni_urller) + f"\n\nDETAYLAR:\n{ham_cikti}"

# ==============================================================================
# 5. ŞIK HTML E-POSTA ŞABLON OLUŞTURUCU VE GÖNDERİCİ
# ==============================================================================
def html_rapor_olustur(tarih_str: str, seans: str, rapor_verisi: GunlukRaporSemasi) -> str:
    kartlar_html = ""
    sirali_ilanlar = sorted(rapor_verisi.ilanlar, key=lambda x: x.uygunluk_puani, reverse=True)
    
    for idx, ilan in enumerate(sirali_ilanlar, 1):
        if ilan.uygunluk_puani >= 8:
            rozet_bg = "#dcfce7"
            rozet_renk = "#166534"
            rozet_etiket = f"★ {ilan.uygunluk_puani}/10 Yüksek Uyum"
        elif ilan.uygunluk_puani >= 6:
            rozet_bg = "#fef9c3"
            rozet_renk = "#854d0e"
            rozet_etiket = f"● {ilan.uygunluk_puani}/10 Orta Uyum"
        else:
            rozet_bg = "#f1f5f9"
            rozet_renk = "#475569"
            rozet_etiket = f"○ {ilan.uygunluk_puani}/10 Alternatif"

        guvenli_baslik = html.escape(ilan.baslik)
        guvenli_platform = html.escape(ilan.platform)
        guvenli_butce = html.escape(ilan.tahmini_butce_sure)
        guvenli_neden = html.escape(ilan.neden_uygun)
        guvenli_teklif = html.escape(ilan.teklif_metni).replace("\n", "<br>")
        guvenli_url = html.escape(ilan.url)

        kartlar_html += f"""
        <div style="background-color:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:22px; margin-bottom:22px; box-shadow:0 2px 6px rgba(0,0,0,0.04);">
            <div style="margin-bottom:12px;">
                <span style="display:inline-block; background-color:#eff6ff; color:#1d4ed8; font-size:12px; font-weight:700; padding:4px 10px; border-radius:6px; text-transform:uppercase; letter-spacing:0.5px; margin-right:8px;">
                    {guvenli_platform}
                </span>
                <span style="display:inline-block; background-color:{rozet_bg}; color:{rozet_renk}; font-size:12px; font-weight:700; padding:4px 10px; border-radius:6px;">
                    {rozet_etiket}
                </span>
            </div>

            <h2 style="margin:0 0 10px 0; font-size:18px; color:#0f172a; line-height:1.4;">
                {idx}. {guvenli_baslik}
            </h2>

            <div style="background-color:#f8fafc; border-left:3px solid #3b82f6; padding:10px 14px; margin-bottom:14px; font-size:13px; color:#334155;">
                <strong>💰 Tahmini Bütçe & Süre:</strong> {guvenli_butce}
            </div>

            <p style="margin:0 0 16px 0; font-size:14px; color:#475569; line-height:1.6;">
                <strong style="color:#1e293b;">Teknik Değerlendirme:</strong> {guvenli_neden}
            </p>

            <div style="margin-bottom:18px;">
                <div style="font-size:12px; font-weight:700; color:#64748b; text-transform:uppercase; margin-bottom:6px; letter-spacing:0.5px;">
                    📋 Kopyala & Gönder Teklif Taslağı (Metne tıklayıp seçebilirsiniz):
                </div>
                <div style="background-color:#f1f5f9; border:1px dashed #94a3b8; border-radius:8px; padding:14px; font-family:'Courier New', Courier, monospace; font-size:13px; color:#1e293b; line-height:1.5; user-select:all; -webkit-user-select:all;">
                    {guvenli_teklif}
                </div>
            </div>

            <div style="text-align:right;">
                <a href="{guvenli_url}" target="_blank" style="display:inline-block; background-color:#2563eb; color:#ffffff; text-decoration:none; font-weight:700; font-size:14px; padding:11px 22px; border-radius:8px;">
                    İlana Git & Başvur →
                </a>
            </div>
        </div>
        """

    tam_html = f"""
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0; padding:0; background-color:#f1f5f9; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
        <div style="max-width:680px; margin:24px auto; padding:0 16px;">
            <div style="background:linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color:#ffffff; padding:28px 24px; border-radius:14px; margin-bottom:20px;">
                <div style="font-size:12px; color:#93c5fd; font-weight:700; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px;">
                    Otonom CAD/CAM & Kalıp Mühendisliği İlan Ağı
                </div>
                <h1 style="margin:0 0 8px 0; font-size:22px; font-weight:800;">
                    Yeni Freelance Fırsatları Raporu
                </h1>
                <div style="font-size:13px; color:#cbd5e1;">
                    📅 {tarih_str} &nbsp;|&nbsp; 🕒 {seans} &nbsp;|&nbsp; 🎯 {len(sirali_ilanlar)} Yeni Fırsat
                </div>
            </div>

            <div style="background-color:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:18px 22px; margin-bottom:22px;">
                <div style="font-size:13px; font-weight:700; color:#0f172a; margin-bottom:6px;">📌 Seans Özeti</div>
                <p style="margin:0; font-size:14px; color:#475569; line-height:1.5;">
                    {html.escape(rapor_verisi.genel_ozet)}
                </p>
            </div>

            {kartlar_html}

            <div style="text-align:center; padding:16px; font-size:12px; color:#94a3b8;">
                Bu rapor Google AI Studio (Gemini 2.5 Flash) otonom ajan ağı tarafından oluşturulmuştur.<br>
                Daha önce raporlanan ilanlar SQLite veritabanı ile otomatik filtrelenmiştir.
            </div>
        </div>
    </body>
    </html>
    """
    return tam_html

def eposta_gonder_html(konu: str, html_icerik: str, duz_metin_yedek: str):
    msg = MIMEMultipart("alternative")
    msg['From'] = GMAIL_GONDEREN
    msg['To'] = RAPOR_ALICI_EMAIL
    msg['Subject'] = konu

    msg.attach(MIMEText(duz_metin_yedek, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_icerik, 'html', 'utf-8'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI.replace(" ", ""))
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now(TR_SAAT_DILIMI).strftime('%H:%M:%S')}] HTML Rapor e-posta olarak başarıyla gönderildi!")
    except Exception as e:
        print(f"E-posta gönderim hatası: {e}")

# ==============================================================================
# 6. UZMANLIK PROFİLİ VE AJANLAR
# ==============================================================================
UZMANLIK_PROFILI = """
- Ana Uzmanlık: Sac Metal Kalıp Tasarımı (Progresif, Transfer, Kesme/Bükme Kalıpları)
- Yazılımlar: SolidWorks, Logopress, Mastercam (CNC Freze/Torna CAM), Blender
- Ek Yetkinlikler: Tersine Mühendislik (3D Tarama datası katılaştırma), Özel Makine/Aparat Tasarımı, 3D Baskı Prototipleme
- Hedef Pazar: Küresel (Upwork, LinkedIn, Freelancer, Fiverr, GrabCAD) ve Yerel (Bionluk, proje bazlı ilanlar)
"""

ARAMA_KATEGORI_LISTESI = [
    'site:upwork.com/freelance-jobs ("SolidWorks" OR "Sheet Metal" OR "Die Design")',
    'site:linkedin.com/jobs ("SolidWorks" OR "Sheet Metal" OR "Tool and Die") ("contract" OR "freelance" OR "remote")',
    'site:linkedin.com/posts ("looking for" OR "hiring") ("SolidWorks" OR "sheet metal" OR "Mastercam") freelance',
    'site:freelancer.com/projects ("CNC" OR "Mastercam" OR "SolidWorks" OR "Sheet Metal")',
    '"freelance" ("sheet metal die design" OR "progressive die
