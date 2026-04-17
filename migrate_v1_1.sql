-- ============================================================
-- PilotOps v1.0 → v1.1 Migration
-- Mevcut veritabanına çalıştır, var olan veriler silinmez.
-- ============================================================

PRAGMA foreign_keys = ON;

-- pilots tablosuna watch_id kolonu ekle (yoksa)
ALTER TABLE pilots ADD COLUMN watch_id INTEGER REFERENCES watches(id);

-- pilot_izin tablosunu oluştur (yoksa)
CREATE TABLE IF NOT EXISTS pilot_izin (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pilot_id    INTEGER NOT NULL REFERENCES pilots(id),
    watch_id    INTEGER NOT NULL REFERENCES watches(id),
    baslangic   TEXT    NOT NULL,
    bitis       TEXT,
    aktif       INTEGER DEFAULT 1,
    olusturma   TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pilot_izin_pilot ON pilot_izin(pilot_id);

-- v_pilot_current view'ı güncelle (watch_id ve izin bilgisiyle)
DROP VIEW IF EXISTS v_pilot_current;
CREATE VIEW v_pilot_current AS
SELECT
    p.id            AS pilot_id,
    p.ad_soyad,
    p.telefon,
    p.watch_id,
    w_assigned.watch_no AS pilot_watch_no,
    w_aktif.watch_no    AS aktif_watch_no,
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
    )               AS aktif_gemi,
    COALESCE(
        (SELECT 1 FROM pilot_izin pi
         WHERE pi.pilot_id = p.id AND pi.aktif = 1 LIMIT 1), 0
    )               AS izinde
FROM pilots p
LEFT JOIN watches w_assigned ON w_assigned.id = p.watch_id
JOIN watches w_aktif ON w_aktif.port_id = p.port_id AND w_aktif.aktif = 1
LEFT JOIN operations o ON o.pilot_id = p.id AND o.watch_id = w_aktif.id
WHERE p.aktif = 1
GROUP BY p.id
ORDER BY p.watch_id, p.ad_soyad;
