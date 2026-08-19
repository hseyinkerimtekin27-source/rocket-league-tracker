"""
replay_parser.py
-----------------
Rocket League .replay dosyalarının HEADER kısmını (sıkıştırılmamış, belgelenmiş
binary format) saf Python ile okuyup maç istatistiklerini çıkarır.

Not: Replay dosyasının "network stream" kısmı (top pozisyonu, boost alma anları vb.)
sıkıştırılmış ve çok daha karmaşıktır; onu parse etmiyoruz çünkü ihtiyacımız yok.
Header kısmı zaten skor, gol/asist/kurtarış, oyuncu isimleri, harita, playlist gibi
maç sonu istatistiklerinin tamamını düz metin/property olarak içeriyor.

ÖNEMLİ (v2 düzeltmesi): Rocket League replay formatında her property'nin
("None" olmayan her anahtar) ardından tipi VE değerinin kaç byte tuttuğu
(8 byte'lık bir "size" alanı) geliyor. İlk sürüm sadece bilinen birkaç tipi
(Bool/Int/Float/Str/Name/Array...) elle parse ediyor, StructProperty gibi
(Vector, Rotator, UniqueNetId, MatchGuid vb. içeren) tipleri hiç tanımıyordu
ve gerçek maç dosyalarında bu tiple karşılaşınca çöküyordu.

Artık YAKLAŞIM ŞÖYLE: bilinen tipleri hâlâ anlamlı şekilde parse ediyoruz,
ama HER property için okuma bittikten sonra, dosyanın imlecini "bu property
nerede başladı + bildirilen size" konumuna zorla taşıyoruz (seek). Böylece
içeriğini tam anlamadığımız (StructProperty gibi) ya da ileride karşımıza
çıkabilecek HİÇ TANIMADIĞIMIZ bir property tipi bile artık parser'ı
çökertmiyor — sadece o alanın verisini önemsemeden atlayıp bir sonraki
property'ye doğru byte'tan devam ediyoruz.
"""

import struct
import io


class ReplayParseError(Exception):
    pass


def _read_u32(f):
    return struct.unpack("<I", f.read(4))[0]


def _read_i32(f):
    return struct.unpack("<i", f.read(4))[0]


def _read_u64(f):
    return struct.unpack("<Q", f.read(8))[0]


def _read_float(f):
    return struct.unpack("<f", f.read(4))[0]


def _read_string(f):
    """RL strings: int32 length (negative => UTF-16LE), null terminated."""
    length = _read_i32(f)
    if length == 0:
        return ""
    if length < 0:
        # UTF-16, length is negative char count (including null terminator)
        n = -length
        raw = f.read(n * 2)
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    else:
        raw = f.read(length)
        return raw.decode("ascii", errors="replace").rstrip("\x00")


def _read_property_value(f, prop_type, size):
    """Bir property'nin degerini okur. Tipi ne olursa olsun, fonksiyon
    bittiginde dosya imleci HER ZAMAN bu degerin basladigi yer + size
    konumunda olacak sekilde garanti edilir (finally bloguyla) - boylece
    icerigi tam anlamasak, hatta hic taniMASAk bile bir sonraki property
    her zaman doğru byte'tan okunmaya baslar."""
    start = f.tell()
    value = None
    try:
        if prop_type == "BoolProperty":
            value = struct.unpack("<B", f.read(1))[0] != 0
        elif prop_type == "ByteProperty":
            k = _read_string(f)
            v = _read_string(f)
            value = {"key": k, "value": v}
        elif prop_type == "IntProperty":
            value = _read_i32(f)
        elif prop_type == "FloatProperty":
            value = _read_float(f)
        elif prop_type in ("QWordProperty", "Int64Property", "UInt64Property"):
            value = _read_u64(f)
        elif prop_type == "StrProperty":
            value = _read_string(f)
        elif prop_type == "NameProperty":
            value = _read_string(f)
        elif prop_type == "ArrayProperty":
            count = _read_i32(f)
            arr = []
            for _ in range(count):
                try:
                    arr.append(_read_property_dict(f))
                except (struct.error, MemoryError, OSError, UnicodeDecodeError):
                    # Bir eleman bozuksa, o ana kadar toplananlarla yetin -
                    # tum listeyi kaybetmek yerine kismi veri daha iyi.
                    break
            value = arr
        elif prop_type == "StructProperty":
            # Struct'un hangi tur oldugunu (Vector, Rotator, UniqueNetId,
            # MatchGuid, vb.) belirten kendi kendini sinirlayan bir isim
            # string'i geliyor; iceriginin geri kalani bizim ilgilendigimiz
            # istatistiklerin disinda oldugu icin parse etmiyoruz - finally
            # blogundaki seek zaten dogru konuma tasiyacak.
            struct_type = _read_string(f)
            value = {"_struct_type": struct_type}
        else:
            # Bilmedigimiz/gelecekte cikabilecek herhangi bir tip: icerigini
            # hic okumaya calismadan, sadece bildirilen boyut kadar atliyoruz.
            value = None
    except (struct.error, UnicodeDecodeError, OSError):
        value = None
    finally:
        # Guvenlik agi: yukarida ne kadar (dogru ya da yanlis) byte
        # okunmus olursa olsun, bu property'nin degeri icin ayrilan alanin
        # tam sonuna atla. Format dokumantasyonuna gore "size" alani zaten
        # bu degerin tam byte uzunlugunu belirtiyor.
        f.seek(start + size)
    return value


def _read_property_dict(f):
    """Reads properties until 'None' key is hit. Returns a dict.

    ONEMLI DUZELTME: Her property'nin adindan/tipinden sonra gelen 8 byte,
    TEK bir 64-bit sayi ("boyut") degil - Unreal Engine'in property tag
    formatinda bu aslinda IKI AYRI 32-bit alan: gercek "Size" (deger kac
    byte) ve "ArrayIndex" (bu property bir dizinin N'inci elemaniysa
    kacinci oldugu, degilse 0). Bunlari tek u64 gibi okumak, ArrayIndex
    sifir olmadigi durumlarda (ozellikle PlayerStats gibi dizilerin
    ICINDEKI alanlarda) devasa/yanlis bir "boyut" hesaplanmasina ve
    parser'in hizasini tamamen kaybetmesine yol aciyordu - PlayerStats'in
    bos gorunmesinin asil sebebi buydu.

    Ayrica: herhangi bir noktada okuma sirasinda beklenmedik bir hata
    olursa (bozuk/eksik dosya, taninmayan bir kenar durumu), o ana kadar
    toplanan alanlarla birlikte sessizce durup geri donuyoruz - boylece
    tek bir alandaki sorun butun dosyayi (ya da butun oyuncu listesini)
    degil, sadece o alani etkiliyor."""
    result = {}
    while True:
        try:
            key = _read_string(f)
        except (struct.error, MemoryError, OSError, UnicodeDecodeError):
            break
        if key == "None" or key == "":
            break
        try:
            type_name = _read_string(f)
            size = _read_u32(f)       # gercek deger boyutu (byte)
            _array_index = _read_u32(f)  # kullanmiyoruz, sadece pozisyonu ilerletmek icin okunuyor
        except (struct.error, MemoryError, OSError, UnicodeDecodeError):
            break
        value = _read_property_value(f, type_name, size)
        result[key] = value
    return result


def parse_replay_header_raw(filepath):
    """Debug amaçli: hicbir sadelestirme yapmadan, header'daki TUM property'lerin
    ham dict'ini dondurur (anahtar isimlerini gormek icin)."""
    with open(filepath, "rb") as f:
        header_size = _read_u32(f)
        _header_crc = _read_u32(f)
        header_bytes = f.read(header_size)

    hf = io.BytesIO(header_bytes)
    _engine_version = _read_u32(hf)
    licensee_version = _read_u32(hf)
    if licensee_version >= 18:
        _net_version = _read_u32(hf)
    _replay_class = _read_string(hf)

    return _read_property_dict(hf)


def parse_replay_header(filepath):
    """
    Verilen .replay dosyasının header'ını parse eder ve maç istatistiklerini
    içeren bir dict döndürür.
    """
    with open(filepath, "rb") as f:
        header_size = _read_u32(f)
        _header_crc = _read_u32(f)

        header_bytes = f.read(header_size)

    hf = io.BytesIO(header_bytes)

    engine_version = _read_u32(hf)
    licensee_version = _read_u32(hf)

    net_version = 0
    # Net version field exists from licensee version >= 18 onward (documented behavior)
    if licensee_version >= 18:
        net_version = _read_u32(hf)

    replay_class = _read_string(hf)

    props = _read_property_dict(hf)

    return _extract_stats(props)


def _extract_stats(props):
    """Ham property dict'ini bizim işimize yarayan sade bir yapıya indirger."""
    team0 = props.get("Team0Score", 0) or 0
    team1 = props.get("Team1Score", 0) or 0
    match_type = props.get("MatchType", "Unknown")
    map_name = props.get("MapName", "Unknown")
    date = props.get("Date", "")
    team_size = props.get("TeamSize", None)
    playlist = props.get("MatchType", "Unknown")

    player_stats_raw = props.get("PlayerStats", []) or []
    players = []
    for p in player_stats_raw:
        if not isinstance(p, dict):
            continue
        platform_val = p.get("Platform")
        if isinstance(platform_val, dict):
            platform_val = platform_val.get("value", "Unknown")
        players.append({
            "name": p.get("Name", "?") or "?",
            "team": p.get("Team", 0) or 0,
            "score": p.get("Score", 0) or 0,
            "goals": p.get("Goals", 0) or 0,
            "assists": p.get("Assists", 0) or 0,
            "saves": p.get("Saves", 0) or 0,
            "shots": p.get("Shots", 0) or 0,
            "is_bot": bool(p.get("bBot", False)),
            "platform": platform_val or "Unknown",
        })

    mvp_name = None
    if players:
        mvp_name = max(players, key=lambda pl: pl["score"])["name"]

    return {
        "team0_score": team0,
        "team1_score": team1,
        "match_type": match_type,
        "map_name": map_name,
        "date": date,
        "team_size": team_size,
        "playlist": playlist,
        "players": players,
        "mvp_name_guess": mvp_name,
    }


def find_user_result(stats, player_name):
    """
    Verilen oyuncu adına göre bu maçta kazanıp kaybetmediğini ve
    kişisel istatistiklerini çıkarır.
    """
    me = None
    for p in stats["players"]:
        if p["name"].strip().lower() == player_name.strip().lower():
            me = p
            break
    if me is None:
        return None

    my_team_score = stats["team0_score"] if me["team"] == 0 else stats["team1_score"]
    opp_team_score = stats["team1_score"] if me["team"] == 0 else stats["team0_score"]
    result = "win" if my_team_score > opp_team_score else "loss"

    return {
        "result": result,
        "goals": me["goals"],
        "assists": me["assists"],
        "saves": me["saves"],
        "shots": me["shots"],
        "score": me["score"],
        "mvp": stats["mvp_name_guess"] == me["name"],
        "map": stats["map_name"],
        "playlist": stats["playlist"],
        "date": stats["date"],
        "team_score": my_team_score,
        "opp_team_score": opp_team_score,
    }
