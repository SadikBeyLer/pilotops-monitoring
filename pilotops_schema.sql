-- ============================================================
-- PilotOps Veritabanı Şeması v1.0
-- SQLite uyumlu
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ── 1. PORTS ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ad          TEXT    NOT NULL,
    bolge       TEXT,
    ulke        TEXT    DEFAULT 'TR',
    aktif       INTEGER DEFAULT 1,
    olusturma   TEXT    DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO ports (id, ad, bolge) VALUES
    (1, 'İskenderun', 'İskenderun Körfezi');

-- ── 2. PILOTS ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pilots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    port_id     INTEGER NOT NULL REFERENCES ports(id),
    ad_soyad    TEXT    NOT NULL,
    telefon     TEXT,
    aktif       INTEGER DEFAULT 1,
    olusturma   TEXT    DEFAULT (datetime('now'))
);

-- ── 3. WATCHES ──────────────────────────────────────────────
-- Her nöbet dönemi (Watch:1, Watch:2 gibi)
CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    port_id     INTEGER NOT NULL REFERENCES ports(id),
    watch_no    TEXT    NOT NULL,        -- örn. "Watch:2"
    baslangic   TEXT    NOT NULL,        -- ISO datetime
    bitis       TEXT    NOT NULL,
    aktif       INTEGER DEFAULT 1,
    olusturma   TEXT    DEFAULT (datetime('now'))
);

-- ── 4. VESSELS ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vessels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imo_no          TEXT    UNIQUE,
    gemi_adi        TEXT    NOT NULL,
    tip             TEXT,               -- Gnr, Tan, Con, Blk, Ro, DIY, D5a
    bayrak          TEXT,
    grt             REAL,
    loa             REAL,               -- metre
    thruster_bas    INTEGER DEFAULT 0,
    thruster_kic    INTEGER DEFAULT 0,
    tehlikeli_yuk   INTEGER DEFAULT 0,  -- 0/1 checkbox
    not_alani       TEXT,
    from_liman      TEXT,               -- kalkış limanı
    to_liman        TEXT,               -- varış limanı
    gelis_zamani    TEXT,               -- ETA (ISO datetime)
    durum           TEXT    DEFAULT 'yolda', -- limanda / demirde / yolda
    olusturma       TEXT    DEFAULT (datetime('now')),
    guncelleme      TEXT    DEFAULT (datetime('now'))
);

-- ── 5. OPERATIONS ───────────────────────────────────────────
-- Her iş girişi: kaptan + gemi + saatler + fatigue
CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pilot_id        INTEGER NOT NULL REFERENCES pilots(id),
    vessel_id       INTEGER NOT NULL REFERENCES vessels(id),
    watch_id        INTEGER NOT NULL REFERENCES watches(id),

    -- From / To (iş tipini belirler)
    from_nokta      TEXT    NOT NULL,   -- Pilot Position / Demir / rıhtım adı / şamandıra adı
    to_nokta        TEXT    NOT NULL,

    -- Sistem tarafından otomatik belirlenir
    is_tipi         TEXT    NOT NULL,   -- yanasma / kalkis / buoy_yanasma / buoy_kalkis
    k_carpan        REAL    NOT NULL,   -- 1.0 / 0.7 / 1.2

    -- 4 zaman damgası (ISO datetime)
    off_station     TEXT    NOT NULL,
    pob             TEXT    NOT NULL,   -- Pilot On Board
    poff            TEXT    NOT NULL,   -- Pilot Off
    on_station      TEXT    NOT NULL,

    -- Draft (her gelişte farklı)
    draft_bas       REAL,               -- metre
    draft_kic       REAL,

    -- Fatigue (engine tarafından hesaplanır)
    fatigue_katki   REAL    DEFAULT 0,  -- bu işin ham katkısı
    fatigue_toplam  REAL    DEFAULT 0,  -- iş sonrası toplam ham skor
    fatigue_norm    INTEGER DEFAULT 0,  -- normalize skor (0-100, üstü 100+)
    fatigue_durum   TEXT    DEFAULT 'FIT', -- FIT/YORGUN/DIKKAT/BLOK/KRITIK/KRITIK+

    -- Zorunlu atama logu (90+ için)
    zorunlu_atama   INTEGER DEFAULT 0,
    onaylayan       TEXT,               -- müdür adı (75+ için)

    olusturma       TEXT    DEFAULT (datetime('now'))
);

-- ── INDEXLER ────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_operations_pilot   ON operations(pilot_id);
CREATE INDEX IF NOT EXISTS idx_operations_vessel  ON operations(vessel_id);
CREATE INDEX IF NOT EXISTS idx_operations_watch   ON operations(watch_id);
CREATE INDEX IF NOT EXISTS idx_operations_zaman   ON operations(off_station);
CREATE INDEX IF NOT EXISTS idx_vessels_imo        ON vessels(imo_no);
CREATE INDEX IF NOT EXISTS idx_pilots_port        ON pilots(port_id);

-- ── GÖRÜNÜMLER (VIEW) ────────────────────────────────────────
-- Kaptan bazlı aktif nöbet özeti
CREATE VIEW IF NOT EXISTS v_pilot_watch_summary AS
SELECT
    p.id            AS pilot_id,
    p.ad_soyad,
    p.telefon,
    w.watch_no,
    w.baslangic,
    w.bitis,
    COUNT(o.id)                     AS is_sayisi,
    ROUND(SUM(
        (strftime('%s', o.on_station) - strftime('%s', o.off_station)) / 3600.0
    ), 2)                           AS toplam_calisma_saati,
    MAX(o.fatigue_norm)             AS max_fatigue_norm,
    MAX(o.fatigue_toplam)           AS son_fatigue_ham,
    MAX(o.fatigue_durum)            AS fatigue_durum
FROM pilots p
JOIN watches w  ON w.port_id = p.port_id AND w.aktif = 1
LEFT JOIN operations o ON o.pilot_id = p.id AND o.watch_id = w.id
WHERE p.aktif = 1
GROUP BY p.id, w.id
ORDER BY MAX(o.fatigue_norm) DESC;  -- en yorgun üstte

-- Son iş bazlı kaptan listesi (operatör ana ekranı)
CREATE VIEW IF NOT EXISTS v_pilot_current AS
SELECT
    p.id            AS pilot_id,
    p.ad_soyad,
    p.telefon,
    w.watch_no,
    COUNT(o.id)     AS is_sayisi,
    ROUND(SUM(
        (strftime('%s', o.on_station) - strftime('%s', o.off_station)) / 3600.0
    ), 2)           AS calisma_saati,
    COALESCE(
        (SELECT fatigue_toplam FROM operations
         WHERE pilot_id = p.id ORDER BY olusturma DESC LIMIT 1), 0
    )               AS son_fatigue_ham,
    COALESCE(
        (SELECT fatigue_norm FROM operations
         WHERE pilot_id = p.id ORDER BY olusturma DESC LIMIT 1), 0
    )               AS son_fatigue_norm,
    COALESCE(
        (SELECT fatigue_durum FROM operations
         WHERE pilot_id = p.id ORDER BY olusturma DESC LIMIT 1), 'FIT'
    )               AS fatigue_durum,
    COALESCE(
        (SELECT v.gemi_adi FROM operations op2
         JOIN vessels v ON v.id = op2.vessel_id
         WHERE op2.pilot_id = p.id
         AND op2.on_station > datetime('now','-2 hours')
         ORDER BY op2.olusturma DESC LIMIT 1), NULL
    )               AS aktif_gemi
FROM pilots p
JOIN watches w ON w.port_id = p.port_id AND w.aktif = 1
LEFT JOIN operations o ON o.pilot_id = p.id AND o.watch_id = w.id
WHERE p.aktif = 1
GROUP BY p.id
ORDER BY son_fatigue_norm DESC;  -- en yorgun üstte

-- ── VARSAYILAN VERİLER ───────────────────────────────────────
INSERT OR IGNORE INTO watches (id, port_id, watch_no, baslangic, bitis, aktif) VALUES
    (1, 1, 'Watch:1', datetime('now','-14 days'), datetime('now','-7 days'), 0),
    (2, 1, 'Watch:2', datetime('now','-7 days'),  datetime('now'),           1),
    (3, 1, 'Watch:3', datetime('now'),             datetime('now','+7 days'), 0);

