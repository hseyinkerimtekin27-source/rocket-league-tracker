"""
tracker_app.py
---------------
Rocket League İstatistik Takipçisi — Arka Plan Uygulaması

Ne yapar:
- Windows başlangıcında (veya elle çalıştırınca) sistem tepsisine (system tray)
  yerleşir, hiçbir pencere açmadan arkada bekler.
- Rocket League'in replay klasörünü izler. Sen bir maç bitirip "Save Replay"e
  bastığında (veya oyun içinde Select/Back'e basarsan) oyun bir .replay
  dosyası kaydeder; bu dosya oluşur oluşmaz uygulama onu okur, senin
  istatistiklerini (gol, asist, kurtarış, galibiyet/mağlubiyet, MVP) otomatik
  çıkarır ve yerel bir veritabanına kaydeder.
- Tepsi simgesine tıklayınca özet istatistik penceresi açılır.
- Elle veri girmene gerek YOK — tek yapman gereken ilk çalıştırmada
  Epic/Steam kullanıcı adını girmek (hangi oyuncunun "sen" olduğunu
  anlaması için).

Rank hakkında not: Replay dosyaları rankını içermiyor (Psyonix bu bilgiyi
oraya yazmıyor). Bu yüzden rank alanı, sadece rank atladığında tepsi
menüsünden tek tıkla güncellenebilir bir alan olarak bırakıldı — maç başına
değil, sadece rank değiştiğinde dokunman yeterli.

Klasör tespiti hakkında not: Rocket League iki farklı yerde replay
saklayabiliyor — Steam sürümü "Demos" klasörünü, Epic Games Launcher sürümü
"DemosEpic" klasörünü kullanıyor. İkisi de diskte var olabilir (biri boş
olsa bile), bu yüzden sadece "klasör var mı" diye bakmak yetmiyor —
içinde gerçekten .replay dosyası olan klasörü seçiyoruz.
"""

import os
import sys
import json
import time
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import pystray
from PIL import Image, ImageDraw
from plyer import notification

from replay_parser import parse_replay_header, find_user_result

APP_NAME = "RL Stats Tracker"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RLStatsTracker")
DB_PATH = os.path.join(APP_DIR, "stats.db")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

_RL_BASE = os.path.join(
    os.path.expanduser("~"), "Documents", "My Games", "Rocket League", "TAGame"
)
# Steam surumu "Demos" klasorunu kullanir, Epic Games Launcher surumu "DemosEpic" kullanir.
CANDIDATE_REPLAY_DIRS = [
    os.path.join(_RL_BASE, "Demos"),
    os.path.join(_RL_BASE, "DemosEpic"),
]


def _en_yeni_replay_zamani(path):
    """Bir klasordeki en yeni .replay dosyasinin degisim zamanini dondurur (yoksa -1)."""
    en_yeni = -1
    try:
        for fn in os.listdir(path):
            if fn.lower().endswith(".replay"):
                m = os.path.getmtime(os.path.join(path, fn))
                if m > en_yeni:
                    en_yeni = m
    except OSError:
        pass
    return en_yeni


def detect_replay_dir():
    """Var olan replay klasorlerinden, icinde EN YENI .replay dosyasi olani secer
    (Steam klasoru bos da olsa var olabildigi icin, sadece varligina degil
    icerigine bakmak gerekiyor)."""
    existing = [p for p in CANDIDATE_REPLAY_DIRS if os.path.isdir(p)]
    if not existing:
        return CANDIDATE_REPLAY_DIRS[0]
    existing.sort(key=_en_yeni_replay_zamani, reverse=True)
    return existing[0]


DEFAULT_REPLAY_DIR = detect_replay_dir()


# ---------------------------------------------------------------- config ---
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # config.json bozulmus (elle duzenlerken gorunmez karakter vb.
            # karismis olabilir) - sifirdan basliyoruz, mevcut istatistikler
            # (stats.db) buna dokunulmadigi icin kaybolmaz.
            return {}
    return {}


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def ensure_setup():
    cfg = load_config()
    changed = False
    if "player_name" not in cfg:
        root = tk.Tk()
        root.withdraw()
        name = simpledialog.askstring(
            APP_NAME,
            "Rocket League'de görünen oyuncu adın (Epic/Steam görünen adın) nedir?\n"
            "Bu, hangi oyuncunun sen olduğunu anlamamız için gerekli, tek seferlik.",
        )
        root.destroy()
        if not name:
            sys.exit("Oyuncu adı girilmedi, uygulama kapatılıyor.")
        cfg["player_name"] = name.strip()
        changed = True

    if "replay_dir" not in cfg or not os.path.isdir(cfg.get("replay_dir", "")):
        cfg["replay_dir"] = detect_replay_dir()
        changed = True
    else:
        # Kayitli klasor gecerli bir klasor ama icinde hic .replay yoksa, ve
        # baska bir aday klasorde gercekten dosya varsa, muhtemelen yanlis
        # platform klasoru secilmis demektir (ornegin Steam'in bos "Demos"
        # klasoru diskte var oldugu icin secilmis olabilir) - otomatik duzelt.
        mevcut_bos = _en_yeni_replay_zamani(cfg["replay_dir"]) < 0
        if mevcut_bos:
            daha_iyi = detect_replay_dir()
            if daha_iyi != cfg["replay_dir"] and _en_yeni_replay_zamani(daha_iyi) >= 0:
                cfg["replay_dir"] = daha_iyi
                changed = True

    if "current_rank" not in cfg:
        cfg["current_rank"] = "Sırasız"
        changed = True
    if changed:
        save_config(cfg)
    return cfg


# ------------------------------------------------------------- database ---
def init_db():
    os.makedirs(APP_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            replay_file TEXT UNIQUE,
            date TEXT,
            result TEXT,
            goals INTEGER,
            assists INTEGER,
            saves INTEGER,
            shots INTEGER,
            mvp INTEGER,
            map_name TEXT,
            playlist TEXT,
            rank TEXT,
            team_score INTEGER,
            opp_score INTEGER,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def save_match(conn, replay_file, info, rank):
    try:
        conn.execute("""
            INSERT INTO matches
            (replay_file, date, result, goals, assists, saves, shots, mvp,
             map_name, playlist, rank, team_score, opp_score, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            replay_file, info["date"], info["result"], info["goals"],
            info["assists"], info["saves"], info["shots"], int(info["mvp"]),
            info["map"], info["playlist"], rank,
            info["team_score"], info["opp_team_score"],
            datetime.now().isoformat(),
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Bu replay zaten daha önce kaydedilmiş
        return False


def get_stats(conn):
    cur = conn.execute("SELECT result, goals, assists, saves, mvp FROM matches")
    rows = cur.fetchall()
    total = len(rows)
    wins = sum(1 for r in rows if r[0] == "win")
    losses = total - wins
    goals = sum(r[1] for r in rows)
    assists = sum(r[2] for r in rows)
    saves = sum(r[3] for r in rows)
    mvps = sum(r[4] for r in rows)
    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": (wins / total * 100) if total else 0,
        "avg_goals": (goals / total) if total else 0,
        "avg_assists": (assists / total) if total else 0,
        "avg_saves": (saves / total) if total else 0,
        "mvps": mvps,
        "mvp_rate": (mvps / total * 100) if total else 0,
    }


# ----------------------------------------------------------- watcher ------
class ReplayHandler(FileSystemEventHandler):
    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg

    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(".replay"):
            return
        # Replay dosyası oyun tarafından yazılırken kilitli olabilir,
        # tam yazılmasını beklemek için kısa bir gecikme veriyoruz.
        threading.Timer(3.0, self._process, args=(event.src_path,)).start()

    def _process(self, path):
        try:
            stats = parse_replay_header(path)
            info = find_user_result(stats, self.cfg["player_name"])
            if info is None:
                return  # bu replay'de oyuncu adı eşleşmedi (izleyici/başka lobi)
            saved = save_match(self.conn, os.path.basename(path), info, self.cfg.get("current_rank", "Sırasız"))
            if saved:
                self._notify(info)
        except Exception as e:
            print(f"[RL Tracker] Replay okunamadı ({path}): {e}")

    def _notify(self, info):
        sonuc = "Galibiyet 🏆" if info["result"] == "win" else "Mağlubiyet"
        mvp_txt = " · MVP! ⭐" if info["mvp"] else ""
        try:
            notification.notify(
                title=f"Maç kaydedildi — {sonuc}",
                message=f"{info['goals']} gol · {info['assists']} asist · {info['saves']} kurtarış{mvp_txt}",
                app_name=APP_NAME,
                timeout=6,
            )
        except Exception:
            pass  # bildirim başarısız olsa bile veri zaten kaydedildi


# ------------------------------------------------------------- tray UI ----
def make_icon_image():
    img = Image.new("RGBA", (64, 64), (10, 13, 20, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(61, 169, 252, 255))
    d.ellipse((20, 20, 44, 44), fill=(10, 13, 20, 255))
    return img


def open_stats_window(root, conn):
    win = tk.Toplevel(root)
    win.title(APP_NAME)
    win.geometry("380x360")
    win.configure(bg="#0A0D14")
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(300, lambda: win.attributes("-topmost", False))

    s = get_stats(conn)

    def row(parent, label, value, color="#EDF1F7"):
        f = tk.Frame(parent, bg="#0A0D14")
        f.pack(fill="x", pady=4, padx=16)
        tk.Label(f, text=label, fg="#7E8AA0", bg="#0A0D14", font=("Segoe UI", 10)).pack(side="left")
        tk.Label(f, text=value, fg=color, bg="#0A0D14", font=("Segoe UI", 12, "bold")).pack(side="right")

    tk.Label(win, text="Rocket League İstatistiklerin", fg="#3DA9FC", bg="#0A0D14",
             font=("Segoe UI", 14, "bold")).pack(pady=(18, 10))

    row(win, "Toplam Maç", s["total"])
    row(win, "Galibiyet / Mağlubiyet", f"{s['wins']} - {s['losses']}")
    row(win, "Galibiyet Oranı", f"%{s['win_rate']:.1f}", "#35D07F")
    row(win, "Gol Ortalaması", f"{s['avg_goals']:.2f}")
    row(win, "Asist Ortalaması", f"{s['avg_assists']:.2f}")
    row(win, "Kurtarış Ortalaması", f"{s['avg_saves']:.2f}")
    row(win, "MVP Sayısı", s["mvps"], "#F2C94C")
    row(win, "MVP Oranı", f"%{s['mvp_rate']:.1f}", "#F2C94C")

    if s["total"] == 0:
        tk.Label(win, text="Henüz kaydedilmiş maç yok.\nBir online maç oynayıp bitirdiğinde\nburası otomatik dolacak.",
                 fg="#F2C94C", bg="#0A0D14", font=("Segoe UI", 9), justify="center").pack(pady=10)

    tk.Label(win, text="Bu pencere her açılışta güncel verilerle yenilenir.",
             fg="#5A6478", bg="#0A0D14", font=("Segoe UI", 8)).pack(side="bottom", pady=10)


def update_rank(root, cfg):
    new_rank = simpledialog.askstring(
        APP_NAME, f"Güncel rankın nedir? (şu an: {cfg.get('current_rank', 'Sırasız')})",
        parent=root,
    )
    if new_rank:
        cfg["current_rank"] = new_rank.strip()
        save_config(cfg)


def export_json(root, conn):
    cur = conn.execute("""
        SELECT date, result, goals, assists, saves, mvp, rank, map_name, playlist
        FROM matches ORDER BY date ASC
    """)
    rows = cur.fetchall()
    data = [{
        "date": r[0], "result": r[1], "goals": r[2], "assists": r[3],
        "saves": r[4], "mvp": bool(r[5]), "rank": r[6],
        "map": r[7], "playlist": r[8],
    } for r in rows]
    out_path = os.path.join(os.path.expanduser("~"), "Desktop", "rl_stats_export.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    messagebox.showinfo(APP_NAME, f"Dışa aktarıldı:\n{out_path}", parent=root)


def debug_show_names(root, cfg):
    """En yeni .replay dosyasini parse edip icindeki oyuncu isimlerini,
    config'teki player_name ile birebir (gizli karakterler dahil, repr()
    ile) karsilastirmali gosterir. Isim eslesmemesi sorununu teshis etmek
    icindir."""
    replay_dir = cfg["replay_dir"]
    try:
        dosyalar = [f for f in os.listdir(replay_dir) if f.lower().endswith(".replay")]
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Klasör okunamadı:\n{e}", parent=root)
        return
    if not dosyalar:
        messagebox.showinfo(APP_NAME, "Klasörde .replay dosyası yok.", parent=root)
        return

    # En yeni dosyayi sec
    dosyalar_full = [os.path.join(replay_dir, f) for f in dosyalar]
    en_yeni = max(dosyalar_full, key=os.path.getmtime)

    try:
        stats = parse_replay_header(en_yeni)
    except Exception as e:
        messagebox.showerror(APP_NAME, f"{os.path.basename(en_yeni)} okunamadı:\n{e}", parent=root)
        return

    isimler = [p["name"] for p in stats["players"]]
    metin = f"Dosya: {os.path.basename(en_yeni)}\n\n"
    metin += f"config.json'daki player_name:\n  {cfg['player_name']!r}\n\n"
    metin += f"Bu replay'de bulunan oyuncu isimleri ({len(isimler)} kişi):\n"
    for isim in isimler:
        esit = "  ✓ EŞLEŞTİ" if isim.strip().lower() == cfg["player_name"].strip().lower() else ""
        metin += f"  {isim!r}{esit}\n"
    if not isimler:
        metin += "  (Hiç oyuncu bulunamadı — PlayerStats verisi boş ya da farklı adla saklanıyor olabilir)\n"
    messagebox.showinfo(APP_NAME, metin, parent=root)


def scan_all_replays(root, conn, cfg):
    """Klasordeki TUM .replay dosyalarini (yeni/eski fark etmeksizin) tarar,
    henuz veritabaninda olmayanlari islemeye calisir, ve sonucu (kac tanesi
    basarili, kac tanesi hatali, hata mesajlari dahil) bir pencerede gosterir.
    Boylece watcher baslamadan once zaten klasorde duran eski dosyalar da
    kaydedilir, ve parse hatalari sessizce kaybolmaz."""
    replay_dir = cfg["replay_dir"]
    try:
        dosyalar = [f for f in os.listdir(replay_dir) if f.lower().endswith(".replay")]
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Klasör okunamadı:\n{replay_dir}\n\n{e}", parent=root)
        return

    if not dosyalar:
        messagebox.showinfo(APP_NAME, f"{replay_dir}\n\nBu klasörde hiç .replay dosyası yok.", parent=root)
        return

    basarili = 0
    zaten_kayitli = 0
    hatalar = []
    isim_uyusmadi = 0

    for fn in dosyalar:
        full_path = os.path.join(replay_dir, fn)
        try:
            stats = parse_replay_header(full_path)
            info = find_user_result(stats, cfg["player_name"])
            if info is None:
                isim_uyusmadi += 1
                continue
            saved = save_match(conn, fn, info, cfg.get("current_rank", "Sırasız"))
            if saved:
                basarili += 1
            else:
                zaten_kayitli += 1
        except Exception as e:
            hatalar.append(f"{fn}: {e}")

    ozet = (
        f"Taranan dosya: {len(dosyalar)}\n"
        f"Yeni kaydedilen: {basarili}\n"
        f"Zaten kayıtlıydı: {zaten_kayitli}\n"
        f"İsim eşleşmedi (\"{cfg['player_name']}\" bu replay'de yok): {isim_uyusmadi}\n"
        f"Hata (okunamadı): {len(hatalar)}"
    )
    if hatalar:
        ornekler = "\n".join(hatalar[:5])
        ozet += f"\n\nİlk birkaç hata:\n{ornekler}"
    messagebox.showinfo(APP_NAME, ozet, parent=root)


def show_replay_dir(root, cfg):
    dosya_sayisi = 0
    try:
        dosya_sayisi = sum(1 for fn in os.listdir(cfg["replay_dir"]) if fn.lower().endswith(".replay"))
    except OSError:
        pass
    messagebox.showinfo(
        APP_NAME,
        f"İzlenen klasör:\n{cfg['replay_dir']}\n\n"
        f"Bu klasörde şu an {dosya_sayisi} adet .replay dosyası var.\n"
        f"Oyuncu adı: {cfg['player_name']}",
        parent=root,
    )


# ------------------------------------------------------------------ main --
def main():
    cfg = ensure_setup()
    conn = init_db()

    if not os.path.isdir(cfg["replay_dir"]):
        messagebox.showwarning(
            APP_NAME,
            f"Replay klasörü bulunamadı:\n{cfg['replay_dir']}\n\n"
            "Rocket League'i en az bir kez açıp bir online maç oynadığından emin ol,"
            " klasör ilk maçtan sonra otomatik oluşur."
        )

    # ÖNEMLİ: tkinter'ın kendi olay döngüsü (mainloop) ANA thread'de çalışmalı.
    # pystray menü tıklamaları farklı bir thread'den geldiği için, pencere
    # açma isteklerini root.after(...) ile ana thread'e "kuyruğa" alıyoruz.
    root = tk.Tk()
    root.withdraw()  # ana pencereyi hiç göstermiyoruz, sadece olay döngüsü için var

    observer = Observer()
    handler = ReplayHandler(conn, cfg)
    if os.path.isdir(cfg["replay_dir"]):
        observer.schedule(handler, cfg["replay_dir"], recursive=False)
        observer.start()

    def on_open(icon, item):
        root.after(0, lambda: open_stats_window(root, conn))

    def on_rank(icon, item):
        root.after(0, lambda: update_rank(root, cfg))

    def on_export(icon, item):
        root.after(0, lambda: export_json(root, conn))

    def on_show_dir(icon, item):
        root.after(0, lambda: show_replay_dir(root, cfg))

    def on_scan(icon, item):
        root.after(0, lambda: scan_all_replays(root, conn, cfg))

    def on_debug_names(icon, item):
        root.after(0, lambda: debug_show_names(root, cfg))

    def on_quit(icon, item):
        observer.stop()
        icon.stop()
        root.after(0, root.quit)

    menu = pystray.Menu(
        pystray.MenuItem("İstatistikleri Görüntüle", on_open, default=True),
        pystray.MenuItem("Tüm Replay'leri Tara", on_scan),
        pystray.MenuItem("İsimleri Karşılaştır (Debug)", on_debug_names),
        pystray.MenuItem("Rankımı Güncelle", on_rank),
        pystray.MenuItem("Verileri Dışa Aktar (JSON)", on_export),
        pystray.MenuItem("İzlenen Klasörü Göster", on_show_dir),
        pystray.MenuItem("Çıkış", on_quit),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, menu)

    # pystray, kendi olay döngüsünü ayrı bir arka plan thread'inde çalıştırır.
    threading.Thread(target=icon.run, daemon=True).start()

    # Ana thread'i tkinter'a bırakıyoruz — tüm pencere açma istekleri
    # buradan (root.after ile) işlenecek.
    root.mainloop()


if __name__ == "__main__":
    main()
