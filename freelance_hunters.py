import os
import re
import time
import sqlite3
import hashlib
import smtplib
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

# Türkiye Saati (UTC+3) ve Seans Belirleme
TR_SAAT_DILIMI = timezone(timedelta(hours=3))
su_an_tr = datetime.now(TR_SAAT_DILIMI)
SEANS_ADI = "Sabah Seansı" if su_an_tr.hour < 15 else "Akşam Seansı"

# ==============================================================================
# 2. GOOGLE AI STUDIO CANLI WEB ARAMA ARACI
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
# 3. TEKRAR ÖNLEYİCİ VERİTABANI (SQLITE DEDUPLICATION)
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
# 4. E-POSTA GÖNDERİM MODÜLÜ
# ==============================================================================
def eposta_gonder(konu: str, icerik_markdown: str):
    msg = MIMEMultipart()
    msg['From'] = GMAIL_GONDEREN
    msg['To'] = RAPOR_ALICI_EMAIL
    msg['Subject'] = konu

    msg.attach(MIMEText(icerik_markdown, 'plain', 'utf-8'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_GONDEREN, GMAIL_UYGULAMA_SIFRESI.replace(" ", ""))
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now(TR_SAAT_DILIMI).strftime('%H:%M:%S')}] Rapor e-posta olarak başarıyla gönderildi!")
    except Exception as e:
        print(f"E-posta gönderim hatası: {e}")

# ==============================================================================
# 5. UZMANLIK PROFİLİ VE AJANLAR
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
    '"freelance" ("sheet metal die design" OR "progressive die" OR "stamping die") project',
    '"freelance" ("Mastercam" OR "5 axis CAM" OR "CNC programming") remote',
    'site:bionluk.com ("SolidWorks" OR "3D çizim" OR "kalıp tasarım" OR "CNC")',
    '("tersine mühendislik" OR "sac kalıp tasarımı" OR "progresif kalıp") freelance proje'
]

def ana_calistirici():
    gemini_llm = LLM(
        model="gemini/gemini-2.5-flash",
        api_key=GEMINI_API_KEY
    )

    db = TekrarOnleyiciDB()

    avci_ajan = Agent(
        role='Kıdemli Freelance İlan Araştırmacısı',
        goal='Canlı Google Arama aracını kullanarak sadece yeni yayınlanmış CAD/CAM, CNC ve sac kalıp tasarımı ilanlarının doğrudan linklerini bulmak.',
        backstory='Google Dorking ve Upwork/LinkedIn ilan avcılığında uzman veri madencisi. Her ilanın tam URL adresini ve özetini eksiksiz raporlar.',
        tools=[canli_google_arama],
        llm=gemini_llm,
        verbose=True,
        allow_delegation=False
    )

    filtre_ajan = Agent(
        role='Teknik Proje Analisti ve Kalıp Mühendisi',
        goal='Sadece ilk kez bulunan ilanları incelemek, alakasız/düşük bütçeli işleri eleyip uzmanlık profiline uyanları seçmek.',
        backstory=f'20 yıllık sac kalıp ve CNC tecrübesine sahip baş mühendis. Kriterler:\n{UZMANLIK_PROFILI}',
        llm=gemini_llm,
        verbose=True,
        allow_delegation=False
    )

    rapor_ajan = Agent(
        role='İş Geliştirme ve Teklif Stratejisti',
        goal='Onaylanan yeni ilanlar için Türkçe özet, tahmini fiyat/süre analizi ve müşteriye gönderilecek hazır teklif metni hazırlamak.',
        backstory='Yüksek dönüşüm oranlı freelance teklifleri hazırlayan kıdemli iş geliştirme danışmanı.',
        llm=gemini_llm,
        verbose=True,
        allow_delegation=False
    )

    baslangic_zamani = time.time()
    yeni_bulunan_ham_veriler = []
    
    print(f"[{datetime.now(TR_SAAT_DILIMI).strftime('%H:%M:%S')}] {SEANS_ADI} - 30 Dakikalık Bulut Taraması Başladı...")

    while (time.time() - baslangic_zamani) < TARAMA_SURESI_SANIYE:
        for sorgu in ARAMA_KATEGORI_LISTESI:
            if (time.time() - baslangic_zamani) >= TARAMA_SURESI_SANIYE:
                break
                
            tarama_gorevi = Task(
                description=f"Canlı Google İş İlanı Arama aracını kullanarak şu sorgudaki en güncel freelance iş ilanlarını bul. Her ilanın BAŞLIĞINI, AÇIKLAMASINI ve TAM URL LİNKİNİ mutlaka yaz: {sorgu}",
                expected_output="İlan başlığı, kısa özet ve doğrudan URL bağlantıları listesi.",
                agent=avci_ajan
            )
            
            ekip = Crew(agents=[avci_ajan], tasks=[tarama_gorevi], process=Process.sequential)
            
            try:
                sonuc_metni = str(ekip.kickoff())
                filtrelenmis_yeni_veri = db.metni_filtrele(sonuc_metni)
                
                if filtrelenmis_yeni_veri:
                    yeni_bulunan_ham_veriler.append(filtrelenmis_yeni_veri)
                    print(f"[{datetime.now(TR_SAAT_DILIMI).strftime('%H:%M:%S')}] + Yeni benzersiz ilanlar yakalandı ve veritabanına işlendi.")
                else:
                    print(f"[{datetime.now(TR_SAAT_DILIMI).strftime('%H:%M:%S')}] - Yeni ilan bulunamadı veya tümü daha önce raporlanmış.")
            except Exception as e:
                print(f"Tarama hatası (devam ediliyor): {e}")
                
            time.sleep(75)

    tarih_str = datetime.now(TR_SAAT_DILIMI).strftime("%d.%m.%Y")
    
    if not yeni_bulunan_ham_veriler:
        bos_rapor = f"{tarih_str} ({SEANS_ADI}) tarihli 30 dakikalık bulut taraması tamamlandı.\n\nSon taramadan bu yana kriterlerinize uyan yeni bir freelance iş ilanı tespit edilmedi."
        eposta_gonder(f"Freelance Ajan ({tarih_str} - {SEANS_ADI}) - Yeni İlan Yok", bos_rapor)
        return

    ham_veri_metni = "\n\n---\n\n".join(yeni_bulunan_ham_veriler)

    analiz_gorevi = Task(
        description=f"Aşağıdaki SADECE YENİ BULUNMUŞ ilan verilerini incele. Uzmanlık profiline ({UZMANLIK_PROFILI}) uymayanları ele, uygun olanları 10 üzerinden puanla:\n\n{ham_veri_metni[:25000]}",
        expected_output="Puanlanmış ve doğrulanmış yeni iş ilanları listesi.",
        agent=filtre_ajan
    )

    rapor_gorevi = Task(
        description="Seçilen yeni iş ilanları için net bir e-posta raporu yaz. Her ilan için: 1) Başlık ve Doğrudan Link, 2) Tahmini Bütçe ve Süre, 3) Neden Uygun Olduğu, 4) Müşteriye Gönderilecek Hazır Teklif Metni (İngilizce ilanlara İngilizce, Türkçe ilanlara Türkçe) yer alsın.",
        expected_output="E-posta ile gönderilmeye hazır temiz metin raporu.",
        agent=rapor_ajan
    )

    rapor_ekibi = Crew(agents=[filtre_ajan, rapor_ajan], tasks=[analiz_gorevi, rapor_gorevi], process=Process.sequential)
    nihai_rapor = str(rapor_ekibi.kickoff())

    eposta_gonder(f"Yeni Freelance Fırsatları ({tarih_str} - {SEANS_ADI})", nihai_rapor)

if __name__ == "__main__":
    ana_calistirici()
