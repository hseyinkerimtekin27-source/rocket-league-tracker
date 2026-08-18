def detect_replay_dir():
    """Var olan replay klasorunu otomatik bulur (Steam ya da Epic)."""
    for path in CANDIDATE_REPLAY_DIRS:
        if os.path.isdir(path):
            return path
    # Hicbiri henuz olusmadiysa (oyun hic acilmamis / hic online mac oynanmamis),
    # varsayilan olarak ilkini dondur; kullanici en az bir mac oynadiginda
    # bir sonraki acilista dogru klasor otomatik bulunur.
    return CANDIDATE_REPLAY_DIRS[0]
