"""
tracker_app.py
---------------
Rocket League İstatistik Takipçisi — Arka Plan Uygulaması

Ne yapar:
- Windows başlangıcında (veya elle çalıştırınca) sistem tepsisine (system tray)
  yerleşir, hiçbir pencere açmadan arkada bekler.
- Rocket League'in replay klasörünü izler. Her maç bittiğinde oyun otomatik
  olarak bir .replay dosyası kaydeder; bu dosya oluşur oluşmaz uygulama onu
  okur, senin istatistiklerini (gol, asist, kurtarış, galibiyet/mağlubiyet,
  MVP) otomatik çıkarır ve yerel bir veritabanına kaydeder.
- Tepsi simgesine tıklayınca özet istatistik penceresi açılır.
- Elle veri girmene gerek YOK — tek yapman gereken ilk çalıştırmada
  Epic/Steam kullanıcı adını girmek (hangi oyuncunun "sen" olduğunu
  anlaması için).

Rank hakkında not: Replay dosyaları rankını içermiyor (Psyonix bu bilgiyi
oraya yazmıyor). Bu yüzden rank alanı, sadece rank atladığında tepsi
menüsünden tek tıkla güncellenebilir bir alan olarak bırakıldı — maç başına
değil, sadece rank değiştiğinde dokunman yeterli.
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


def detect_replay_dir():
    """Var olan replay klasorunu otomatik bulur (Steam ya da Epic)."""
    for path in CANDIDATE_REPLAY_DIRS:
        if os.path.isdir(path):
            return path
    # Hicbiri henuz olusmadiysa (oyun hic acilmamis / hic online mac oynanmamis),
    # varsayilan olarak ilkini dondur; kullanici en az bir mac oynadiginda
    # bir sonraki acilista dogru klasor otomatik bulunur.
    return CANDIDATE_REPLAY_DIRS[0]


DEFAULT_REPLAY_DIR = detect_replay_dir()


# ---------------------------------------------------------------- config ---
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
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


def open_stats_window(conn):
    win = tk.Tk()
    win.title(APP_NAME)
    win.geometry("380x360")
    win.configure(bg="#0A0D14")

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

    tk.Label(win, text="Bu pencere her açılışta güncel verilerle yenilenir.",
             fg="#5A6478", bg="#0A0D14", font=("Segoe UI", 8)).pack(side="bottom", pady=10)

    win.mainloop()


def update_rank(cfg):
    root = tk.Tk()
    root.withdraw()
    new_rank = simpledialog.askstring(
        APP_NAME, f"Güncel rankın nedir? (şu an: {cfg.get('current_rank', 'Sırasız')})"
    )
    root.destroy()
    if new_rank:
        cfg["current_rank"] = new_rank.strip()
        save_config(cfg)


def export_json(conn):
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
    messagebox.showinfo(APP_NAME, f"Dışa aktarıldı:\n{out_path}")


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

    observer = Observer()
    handler = ReplayHandler(conn, cfg)
    if os.path.isdir(cfg["replay_dir"]):
        observer.schedule(handler, cfg["replay_dir"], recursive=False)
        observer.start()

    def on_open(icon, item):
        threading.Thread(target=open_stats_window, args=(conn,), daemon=True).start()

    def on_rank(icon, item):
        update_rank(cfg)

    def on_export(icon, item):
        export_json(conn)

    def on_quit(icon, item):
        observer.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("İstatistikleri Görüntüle", on_open, default=True),
        pystray.MenuItem("Rankımı Güncelle", on_rank),
        pystray.MenuItem("Verileri Dışa Aktar (JSON)", on_export),
        pystray.MenuItem("Çıkış", on_quit),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, menu)
    icon.run()


if __name__ == "__main__":
    main()
