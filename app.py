import os
import sqlite3
from flask import Flask, render_template, request, jsonify, redirect, url_for, g
from datetime import datetime
from fatigue_engine import (
    calculate_fatigue, apply_recovery, normalize_score,
    format_score, fatigue_color, sort_pilots, mlc_check,
    job_contrib, MAX_FATIGUE, K_RECOVERY
)

app = Flask(__name__)

DATABASE = os.environ.get('DATABASE_PATH', 'pilotops.db')

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db_dir = os.path.dirname(DATABASE)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    # Pilots tablosu yoksa schema'yı çalıştır (ilk kurulum)
    tablo_var = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pilots'"
    ).fetchone()
    if not tablo_var:
        with open('pilotops_schema.sql', 'r', encoding='utf-8') as f:
            db.executescript(f.read())
    # v1.1 — eksik kolon/tablo varsa ekle (mevcut veriyi silmez)
    try:
        db.execute("ALTER TABLE pilots ADD COLUMN watch_id INTEGER REFERENCES watches(id)")
        db.commit()
    except Exception:
        pass
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS pilot_izin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pilot_id INTEGER NOT NULL REFERENCES pilots(id),
            watch_id INTEGER REFERENCES watches(id),
            baslangic TEXT, bitis TEXT, aktif INTEGER DEFAULT 1
        )""")
        db.commit()
    except Exception:
        pass
    # v1.2 — vessels yeni alanlar
    for col in ["acenta TEXT", "tug_var INTEGER DEFAULT 0", "tug_adet INTEGER DEFAULT 0", "process TEXT"]:
        try:
            db.execute(f"ALTER TABLE vessels ADD COLUMN {col}")
            db.commit()
        except Exception:
            pass
    db.close()

def migrate_vessels():
    db = sqlite3.connect(DATABASE)
    cols = [r[1] for r in db.execute("PRAGMA table_info(vessels)").fetchall()]
    if 'draft_bas' not in cols:
        db.execute("ALTER TABLE vessels ADD COLUMN draft_bas REAL DEFAULT 0")
    if 'draft_kic' not in cols:
        db.execute("ALTER TABLE vessels ADD COLUMN draft_kic REAL DEFAULT 0")
    db.commit()
    db.close()    

SAMANDIRALAR = ['wimba','g.nato','k.nato','sa/sa','petgaz','b.aygaz','k.aygaz','milangaz']

def detect_is_tipi(from_nokta, to_nokta):
    f = from_nokta.lower().strip()
    t = to_nokta.lower().strip()
    def is_sam(s): return any(sam in s for sam in SAMANDIRALAR)
    if is_sam(f): return ('buoy_kalkis', 0.7)
    if is_sam(t): return ('buoy_yanasma', 1.2)
    if f in ('pilot position', 'demir'): return ('yanasma', 1.0)
    if t in ('pilot position', 'demir'): return ('kalkis', 0.7)
    return ('yanasma', 1.0)

def dt_to_abs_hour(dt_str, base_date=None):
    dt = datetime.fromisoformat(dt_str)
    if base_date:
        base = datetime.fromisoformat(base_date)
        return (dt - base).total_seconds() / 3600
    return dt.hour + dt.minute / 60

# ════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════



@app.route('/')
def index():
    db = get_db()
    watch = db.execute(
        "SELECT * FROM watches WHERE aktif=1 ORDER BY baslangic DESC LIMIT 1"
    ).fetchone()
    all_watches = db.execute("SELECT * FROM watches ORDER BY id").fetchall()

    if watch:
        pilots_raw = db.execute("""
            SELECT p.id AS pilot_id, p.ad_soyad, p.telefon, p.watch_id,
                   w.watch_no,
                   COALESCE(
                     (SELECT fatigue_toplam FROM operations
                      WHERE pilot_id=p.id ORDER BY olusturma DESC LIMIT 1),0
                   ) AS son_fatigue_ham,
                   COALESCE(
                     (SELECT fatigue_norm FROM operations
                      WHERE pilot_id=p.id ORDER BY olusturma DESC LIMIT 1),0
                   ) AS son_fatigue_norm,
                   COALESCE(
                     (SELECT fatigue_durum FROM operations
                      WHERE pilot_id=p.id ORDER BY olusturma DESC LIMIT 1),'FIT'
                   ) AS fatigue_durum,
                   COALESCE(
                     (SELECT COUNT(*) FROM operations
                      WHERE pilot_id=p.id AND watch_id=?),0
                   ) AS is_sayisi,
                   COALESCE(
                     (SELECT ROUND(SUM((strftime('%s',on_station)-strftime('%s',off_station))/3600.0),2)
                      FROM operations WHERE pilot_id=p.id AND watch_id=?),0
                   ) AS calisma_saati,
                   -- Aktif gemi + detaylar (son 2 saat içinde biten iş)
                   (SELECT v.gemi_adi FROM operations op2
                    JOIN vessels v ON v.id=op2.vessel_id
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_gemi,
                   (SELECT v.loa FROM operations op2
                    JOIN vessels v ON v.id=op2.vessel_id
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_loa,
                   (SELECT v.grt FROM operations op2
                    JOIN vessels v ON v.id=op2.vessel_id
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_grt,
                   (SELECT op2.from_nokta FROM operations op2
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_from,
                   (SELECT op2.to_nokta FROM operations op2
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_to,
                    (SELECT op2.off_station FROM operations op2
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_off_station,
 
                    (SELECT op2.pob FROM operations op2
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_pob,
 
                    (SELECT op2.poff FROM operations op2
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_poff,
 
                    (SELECT op2.on_station FROM operations op2
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_on_station,
 
                   -- BT: thruster_bas veya thruster_kic varsa 1
                   (SELECT (CASE WHEN (v.thruster_bas > 0 OR v.thruster_kic > 0) THEN 1 ELSE 0 END)
                    FROM operations op2
                    JOIN vessels v ON v.id=op2.vessel_id
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_thruster,
                    (SELECT v.tug_adet FROM operations op2
                    JOIN vessels v ON v.id=op2.vessel_id
                    WHERE op2.pilot_id=p.id
                    AND (op2.on_station IS NULL OR op2.on_station = '')
                    ORDER BY op2.olusturma DESC LIMIT 1) AS aktif_tug,
                   -- MLC kontrol için toplam calisma
                   COALESCE(
                     (SELECT ROUND(SUM((strftime('%s',on_station)-strftime('%s',off_station))/3600.0),2)
                      FROM operations WHERE pilot_id=p.id
                      AND off_station >= datetime('now','-24 hours')),0
                   ) AS calisma_24h
            FROM pilots p
            LEFT JOIN watches w ON w.id=p.watch_id
            WHERE p.aktif=1
              AND p.watch_id=?
              AND NOT EXISTS (
                SELECT 1 FROM pilot_izin pi
                WHERE pi.pilot_id=p.id AND pi.aktif=1
              )
            ORDER BY son_fatigue_norm DESC
        """, (watch['id'], watch['id'], watch['id'])).fetchall()

   # Dinlenme sonrası fatigue güncelle
        now = datetime.now()
        pilots_list = []
        for p in pilots_raw:
            p = dict(p)
            last_op = db.execute(
                "SELECT on_station, fatigue_toplam FROM operations WHERE pilot_id=? ORDER BY olusturma DESC LIMIT 1",
                (p['pilot_id'],)
            ).fetchone()
            if last_op and last_op['on_station']:
                try:
                    last_time = datetime.fromisoformat(last_op['on_station'])
                    rest_h = (now - last_time).total_seconds() / 3600
                    if rest_h > 0:
                        recovered = apply_recovery(last_op['fatigue_toplam'], rest_h)
                        p['son_fatigue_norm'] = normalize_score(recovered)
                        _, p['fatigue_durum'] = fatigue_color(recovered)
                    # R/H hesapla
                    total_h = round(rest_h, 1)
                    days = int(total_h // 24)
                    hours = round(total_h % 24, 1)
                    if days > 0:
                        p['rest_hours'] = f"{days}g {int(hours)}s"
                    else:
                        p['rest_hours'] = f"{hours}s"
                except:
                    p['rest_hours'] = None
            else:
                p['rest_hours'] = None
            # Aktif iş varsa (on_station boş) R/H sıfırla
            aktif_op = db.execute(
                "SELECT id FROM operations WHERE pilot_id=? AND (on_station IS NULL OR on_station='')",
                (p['pilot_id'],)
            ).fetchone()
            if aktif_op:
                p['rest_hours'] = '0s'
                p['son_fatigue_norm'] = p.get('son_fatigue_norm', 0)
            pilots_list.append(p)
        pilots_raw = sorted(pilots_list, key=lambda x: x['son_fatigue_norm'], reverse=True)

        # Her pilot için iş listesini çek (accordion için)
        pilot_jobs = {}
        for p in pilots_raw:
            jobs = db.execute("""
                SELECT o.id, o.from_nokta, o.to_nokta, o.is_tipi,
                       o.off_station, o.pob, o.poff, o.on_station,
                       o.fatigue_norm,
                       v.gemi_adi, v.tip, v.grt, v.loa,
                       v.thruster_bas, v.thruster_kic
                FROM operations o
                JOIN vessels v ON v.id=o.vessel_id
                WHERE o.pilot_id=?
                ORDER BY o.off_station DESC
            """, (p['pilot_id'],)).fetchall()
            pilot_jobs[p['pilot_id']] = jobs
    else:
        pilots_raw = []
        pilot_jobs = {}

    return render_template('index.html',
                           watch=watch,
                           pilots=pilots_raw,
                           pilot_jobs=pilot_jobs,
                           all_watches=all_watches)

# ── Kaptanlar ────────────────────────────────────────────────
@app.route('/pilots')
def pilots():
    db = get_db()
    watches = db.execute("SELECT * FROM watches ORDER BY id").fetchall()
    aktif_watch = db.execute("SELECT * FROM watches WHERE aktif=1 LIMIT 1").fetchone()
    pilots_raw = db.execute("""
        SELECT p.*,
               w.watch_no,
               COALESCE(
                   (SELECT 1 FROM pilot_izin pi
                    WHERE pi.pilot_id=p.id AND pi.aktif=1 LIMIT 1),0
               ) AS izinde
        FROM pilots p
        LEFT JOIN watches w ON w.id=p.watch_id
        WHERE p.aktif=1
        ORDER BY COALESCE(p.watch_id,9999), p.ad_soyad
    """).fetchall()
    return render_template('pilots.html',
                           pilots=pilots_raw,
                           watches=watches,
                           aktif_watch=aktif_watch,
                           watch=aktif_watch,
                           all_watches=watches)

# ── Inline Pilot Ekle ────────────────────────────────────────
@app.route('/pilots/add-inline', methods=['POST'])
def pilot_add_inline():
    db = get_db()
    ad_soyad = request.form.get('ad_soyad','').strip()
    telefon  = request.form.get('telefon','').strip()
    watch_id = request.form.get('watch_id',None)
    if not ad_soyad:
        return jsonify({'ok':False,'hata':'Ad soyad boş olamaz'})
    watch_id = int(watch_id) if watch_id else None
    db.execute(
        "INSERT INTO pilots (port_id,ad_soyad,telefon,watch_id) VALUES (?,?,?,?)",
        (1,ad_soyad,telefon,watch_id)
    )
    db.commit()
    return jsonify({'ok':True})

# ── Pilot Güncelle ───────────────────────────────────────────
@app.route('/pilots/<int:pilot_id>/guncelle', methods=['POST'])
def pilot_guncelle(pilot_id):
    db = get_db()
    ad_soyad = request.form.get('ad_soyad','').strip()
    telefon  = request.form.get('telefon','').strip()
    watch_id = request.form.get('watch_id',None)
    if not ad_soyad:
        return jsonify({'ok':False,'hata':'Ad soyad boş olamaz'})
    watch_id = int(watch_id) if watch_id else None
    db.execute(
        "UPDATE pilots SET ad_soyad=?,telefon=?,watch_id=? WHERE id=?",
        (ad_soyad,telefon,watch_id,pilot_id)
    )
    db.commit()
    return jsonify({'ok':True})

# ── Pilot Sil ────────────────────────────────────────────────
@app.route('/pilots/<int:pilot_id>/sil', methods=['POST'])
def pilot_sil(pilot_id):
    db = get_db()
    db.execute("UPDATE pilots SET aktif=0 WHERE id=?", (pilot_id,))
    db.commit()
    return redirect(url_for('pilots'))

# ── İzin Toggle ──────────────────────────────────────────────
@app.route('/pilots/<int:pilot_id>/izin-toggle', methods=['POST'])
def pilot_izin_toggle(pilot_id):
    db = get_db()
    watch = db.execute("SELECT id FROM watches WHERE aktif=1 LIMIT 1").fetchone()
    watch_id = watch['id'] if watch else 1
    mevcut = db.execute(
        "SELECT id FROM pilot_izin WHERE pilot_id=? AND aktif=1 LIMIT 1",(pilot_id,)
    ).fetchone()
    if mevcut:
        db.execute(
            "UPDATE pilot_izin SET aktif=0,bitis=? WHERE id=?",
            (datetime.now().isoformat(timespec='minutes'),mevcut['id'])
        )
    else:
        db.execute(
            "INSERT INTO pilot_izin (pilot_id,watch_id,baslangic,aktif) VALUES (?,?,?,1)",
            (pilot_id,watch_id,datetime.now().isoformat(timespec='minutes'))
        )
    db.commit()
    return redirect(url_for('pilots'))

# ── Watch Geçişi ─────────────────────────────────────────────
@app.route('/watches/set-active', methods=['POST'])
def watch_set_active():
    db = get_db()
    watch_id  = int(request.form['watch_id'])
    baslangic = request.form.get('baslangic','')
    bitis     = request.form.get('bitis','')
    db.execute("UPDATE watches SET aktif=0")
    if baslangic and bitis:
        db.execute(
            "UPDATE watches SET aktif=1,baslangic=?,bitis=? WHERE id=?",
            (baslangic,bitis,watch_id)
        )
    else:
        db.execute("UPDATE watches SET aktif=1 WHERE id=?",(watch_id,))
    db.commit()
    return redirect(url_for('index'))

# ── Eski pilot_add (geriye uyumluluk) ────────────────────────
@app.route('/pilots/add', methods=['GET','POST'])
def pilot_add():
    db = get_db()
    if request.method == 'POST':
        ad_soyad = request.form['ad_soyad']
        telefon  = request.form.get('telefon','')
        watch_id = request.form.get('watch_id',None)
        watch_id = int(watch_id) if watch_id else None
        db.execute(
            "INSERT INTO pilots (port_id,ad_soyad,telefon,watch_id) VALUES (?,?,?,?)",
            (1,ad_soyad,telefon,watch_id)
        )
        db.commit()
        return redirect(url_for('pilots'))
    watches = db.execute("SELECT * FROM watches ORDER BY id").fetchall()
    return render_template('pilot_add.html', watches=watches)

# ── Gemiler ──────────────────────────────────────────────────
@app.route('/vessels')
def vessels():
    db = get_db()
    vessels = db.execute("SELECT * FROM vessels ORDER BY gelis_zamani DESC").fetchall()
    return render_template('vessels.html', vessels=vessels)

@app.route('/vessels/add', methods=['GET','POST'])
def vessel_add():
    if request.method == 'POST':
        db = get_db()
        imo = request.form.get('imo_no', '').strip()
        if not imo:
            return render_template('vessel_add.html', hata='IMO numarası boş bırakılamaz.')
        mevcut = db.execute("SELECT id FROM vessels WHERE imo_no=?", (imo,)).fetchone()
        if mevcut:
            return render_template('vessel_add.html', hata='Bu IMO numarası zaten kayıtlı.')
        db.execute("""
            INSERT INTO vessels
            (imo_no,gemi_adi,tip,bayrak,grt,loa,
             thruster_bas,thruster_kic,tehlikeli_yuk,not_alani,
             from_liman,to_liman,gelis_zamani,durum,
             acenta,tug_var,tug_adet,process)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            request.form.get('imo_no',''),
            request.form['gemi_adi'],
            request.form.get('tip',''),
            request.form.get('bayrak',''),
            float(request.form.get('grt',0) or 0),
            float(request.form.get('loa',0) or 0),
            int(request.form.get('thruster_bas',0) or 0),
            int(request.form.get('thruster_kic',0) or 0),
            1 if request.form.get('tehlikeli_yuk') else 0,
            request.form.get('not_alani',''),
            request.form.get('from_liman',''),
            request.form.get('to_liman',''),
            request.form.get('gelis_zamani',''),
            request.form.get('durum','gelecek'),
            request.form.get('acenta',''),
            int(request.form.get('tug_var',0) or 0),
            int(request.form.get('tug_adet',0) or 0),
            request.form.get('process',''),
        ))
        db.commit()
        return redirect(url_for('vessels') + '?seg=' + request.form.get('durum', 'gelecek'))
    return render_template('vessel_add.html')

@app.route('/vessels/<int:vessel_id>/edit', methods=['GET','POST'])
def vessel_edit(vessel_id):
    db = get_db()
    vessel = db.execute("SELECT * FROM vessels WHERE id=?", (vessel_id,)).fetchone()
    if not vessel:
        return redirect(url_for('vessels'))
    if request.method == 'POST':
        db.execute("""
            UPDATE vessels SET
            imo_no=?, gemi_adi=?, tip=?, bayrak=?, grt=?, loa=?,
            thruster_bas=?, thruster_kic=?, tehlikeli_yuk=?, not_alani=?,
            from_liman=?, to_liman=?, gelis_zamani=?, durum=?,
            acenta=?, tug_var=?, tug_adet=?, process=?,
            draft_bas=?, draft_kic=?
            WHERE id=?
        """, (
            request.form.get('imo_no',''),
            request.form['gemi_adi'],
            request.form.get('tip',''),
            request.form.get('bayrak',''),
            float(request.form.get('grt',0) or 0),
            float(request.form.get('loa',0) or 0),
            int(request.form.get('thruster_bas',0) or 0),
            int(request.form.get('thruster_kic',0) or 0),
            1 if request.form.get('tehlikeli_yuk') else 0,
            request.form.get('not_alani',''),
            request.form.get('from_liman',''),
            request.form.get('to_liman',''),
            request.form.get('gelis_zamani',''),
            request.form.get('durum','gelecek'),
            request.form.get('acenta',''),
            int(request.form.get('tug_var',0) or 0),
            int(request.form.get('tug_adet',0) or 0),
            request.form.get('process',''),
            float(request.form.get('draft_bas',0) or 0),
            float(request.form.get('draft_kic',0) or 0),
            vessel_id
        ))
        db.commit()
        return redirect(url_for('vessels') + '?seg=' + request.form.get('durum', 'gelecek'))
    return render_template('vessel_edit.html', vessel=vessel)


# ── Gemi Durum Güncelle ──────────────────────────────────────
@app.route('/vessels/<int:vessel_id>/durum', methods=['POST'])
def vessel_durum(vessel_id):
    db = get_db()
    durum = request.form.get('durum','')
    db.execute("UPDATE vessels SET durum=? WHERE id=?", (durum, vessel_id))
    db.commit()
    return jsonify({'ok': True, 'durum': durum})

# ── İş Girişi ────────────────────────────────────────────────# ── İş Girişi ────────────────────────────────────────────────
@app.route('/operations/add', methods=['GET','POST'])
def operation_add():
    db = get_db()
    pilots  = db.execute("""
        SELECT p.* FROM pilots p
        JOIN watches w ON w.id = p.watch_id
        WHERE p.aktif=1 AND w.aktif=1
        ORDER BY p.ad_soyad
    """).fetchall()
    vessels = db.execute("SELECT * FROM vessels WHERE durum='manevrada' ORDER BY gemi_adi").fetchall()

    if request.method == 'POST':
        pilot_id   = int(request.form['pilot_id'])
        vessel_id  = int(request.form['vessel_id'])
        from_nokta = request.form['from_nokta']
        to_nokta   = request.form['to_nokta']
        off_st     = request.form['off_station']
        pob        = request.form['pob']
        poff       = request.form['poff']
        on_st      = request.form['on_station']
        draft_bas  = float(request.form.get('draft_bas',0) or 0)
        draft_kic  = float(request.form.get('draft_kic',0) or 0)

        # Tarih sınırı kontrolü — 1 gün öncesi / 1 gün sonrası
        now = datetime.now()
        min_dt = datetime(now.year, now.month, now.day) - __import__('datetime').timedelta(days=1)
        max_dt = datetime(now.year, now.month, now.day) + __import__('datetime').timedelta(days=2)
        for dt_str in [x for x in [off_st, pob, poff, on_st] if x]:
            try:
                dt_val = datetime.fromisoformat(dt_str)
                if dt_val < min_dt or dt_val > max_dt:
                    return render_template('operation_add.html', pilots=pilots, vessels=vessels,
                                           samandiralar=SAMANDIRALAR,
                                           hata='Geçersiz tarih — en fazla 1 gün öncesi veya 1 gün sonrası girilebilir.')
            except Exception:
                pass

        # Önceki iş bitiş kontrolü
        prev_bitis = db.execute(
            "SELECT on_station FROM operations WHERE pilot_id=? AND on_station IS NOT NULL AND on_station != '' ORDER BY on_station DESC LIMIT 1",
            (pilot_id,)
        ).fetchone()
        if prev_bitis:
            if datetime.fromisoformat(off_st) < datetime.fromisoformat(prev_bitis['on_station']):
                return render_template('operation_add.html', pilots=pilots, vessels=vessels,
                    samandiralar=SAMANDIRALAR,
                    hata='Off Station, önceki işin bitiş saatinden (' + prev_bitis['on_station'][11:16] + ') önce olamaz.')
        
        is_tipi, k = detect_is_tipi(from_nokta, to_nokta)
        watch = db.execute(
            "SELECT id FROM watches WHERE aktif=1 ORDER BY baslangic DESC LIMIT 1"
        ).fetchone()
        watch_id = watch['id'] if watch else 1
        base   = off_st[:10]+'T00:00:00'
        off_h  = dt_to_abs_hour(off_st,base)
        pob_h  = dt_to_abs_hour(pob,  base) if pob  else off_h
        poff_h = dt_to_abs_hour(poff, base) if poff else off_h
        on_h   = dt_to_abs_hour(on_st,base) if on_st else off_h
        if pob_h  < off_h:  pob_h  += 24
        if poff_h < pob_h:  poff_h += 24
        if on_h   < poff_h: on_h   += 24
        vessel = db.execute("SELECT grt FROM vessels WHERE id=?",(vessel_id,)).fetchone()
        grt = vessel['grt'] if vessel else 8000
        katki = job_contrib(off_h,pob_h,poff_h,on_h,is_tipi,grt)
        prev = db.execute(
            "SELECT fatigue_toplam,on_station FROM operations WHERE pilot_id=? ORDER BY olusturma DESC LIMIT 1",
            (pilot_id,)
        ).fetchone()
        if prev:
            prev_on = prev['on_station'] if prev['on_station'] else off_st
            rest_h = (datetime.fromisoformat(off_st) - datetime.fromisoformat(prev_on)).total_seconds() / 3600
            if rest_h < 0: rest_h = 0
            prev_score = apply_recovery(prev['fatigue_toplam'], rest_h)
        else:
            prev_score = 0.0
        toplam = prev_score + katki
        norm   = normalize_score(toplam)
        color, durum = fatigue_color(toplam)
        zorunlu  = 1 if norm >= 90 else 0
        onaylayan = request.form.get('onaylayan','') if norm >= 75 else ''
        db.execute("""
            INSERT INTO operations
            (pilot_id,vessel_id,watch_id,from_nokta,to_nokta,
             is_tipi,k_carpan,off_station,pob,poff,on_station,
             draft_bas,draft_kic,fatigue_katki,fatigue_toplam,
             fatigue_norm,fatigue_durum,zorunlu_atama,onaylayan)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pilot_id,vessel_id,watch_id,from_nokta,to_nokta,
            is_tipi,k,off_st,pob,poff,on_st,
            draft_bas,draft_kic,katki,toplam,
            norm,durum,zorunlu,onaylayan
        ))
        db.commit()
        return redirect(url_for('index'))

    return render_template('operation_add.html', pilots=pilots, vessels=vessels,
                           samandiralar=SAMANDIRALAR)

# ── API: Kaptan fatigue ───────────────────────────────────────
@app.route('/api/pilot/<int:pilot_id>/fatigue')
def api_pilot_fatigue(pilot_id):
    db = get_db()
    row = db.execute(
        "SELECT fatigue_toplam,fatigue_norm,fatigue_durum,on_station FROM operations WHERE pilot_id=? ORDER BY olusturma DESC LIMIT 1",
        (pilot_id,)
    ).fetchone()
    if not row:
        return jsonify({'ham':0,'norm':0,'durum':'FIT','score_fmt':'0'})
    return jsonify({
        'ham':       round(row['fatigue_toplam'],3),
        'norm':      row['fatigue_norm'],
        'durum':     row['fatigue_durum'],
        'score_fmt': format_score(row['fatigue_toplam'])
    })

# ── API: İş tipi tespiti ─────────────────────────────────────
@app.route('/api/detect-tip')
def api_detect_tip():
    from_n = request.args.get('from','')
    to_n   = request.args.get('to','')
    tip, k = detect_is_tipi(from_n,to_n)
    labels = {
        'yanasma':      ('Yanaşma',           '#27500A','#EAF3DE'),
        'kalkis':       ('Kalkış',             '#0C447C','#E6F1FB'),
        'buoy_yanasma': ('Şamandıra yanaşma',  '#633806','#FAEEDA'),
        'buoy_kalkis':  ('Şamandıra kalkış',   '#3C3489','#EEEDFE'),
    }
    label,color,bg = labels.get(tip,('Belirsiz','#888','#eee'))
    return jsonify({'tip':tip,'k':k,'label':label,'color':color,'bg':bg})

# ── Pilot Jobs (ayrı sayfa — geriye uyumluluk) ───────────────
@app.route('/pilots/<int:pilot_id>/jobs')
def pilot_jobs(pilot_id):
    db = get_db()
    pilot = db.execute("SELECT * FROM pilots WHERE id=?",(pilot_id,)).fetchone()
    jobs  = db.execute("""
        SELECT o.*, v.gemi_adi, v.tip, v.grt
        FROM operations o
        JOIN vessels v ON v.id=o.vessel_id
        WHERE o.pilot_id=?
        ORDER BY o.off_station DESC
    """,(pilot_id,)).fetchall()
    return render_template('pilot_jobs.html',pilot=pilot,jobs=jobs)

# ── Operation Edit ───────────────────────────────────────────
@app.route('/operations/<int:op_id>/edit', methods=['POST'])
def operation_edit(op_id):
    db = get_db()
    op = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
    if not op:
        return 'İş bulunamadı', 404

    off_st = request.form.get('off_station', '').strip()
    pob    = request.form.get('pob', '').strip()
    poff   = request.form.get('poff', '').strip()
    on_st  = request.form.get('on_station', '').strip()

    if not off_st:
        return 'Off Station zorunludur', 400

    from_nokta = op['from_nokta']
    to_nokta   = op['to_nokta']
    pob   = pob   if pob   else (op['pob']        or '')
    poff  = poff  if poff  else (op['poff']       or '')
    on_st = on_st if on_st else (op['on_station'] or '')

    # Önceki iş bitiş kontrolü
    prev_bitis = db.execute(
        "SELECT on_station FROM operations WHERE pilot_id=? AND id!=? AND on_station IS NOT NULL AND on_station != '' ORDER BY on_station DESC LIMIT 1",
        (op['pilot_id'], op_id)
    ).fetchone()
    if prev_bitis:
        if datetime.fromisoformat(off_st) < datetime.fromisoformat(prev_bitis['on_station']):
            return 'Off Station önceki işin bitiş saatinden (' + prev_bitis['on_station'][11:16] + ') önce olamaz', 400
    # Saat sırası kontrolü — sadece dolu alanlar
        if pob and datetime.fromisoformat(pob) < datetime.fromisoformat(off_st):
            return 'POB, Off Station\'dan önce olamaz', 400
        if poff and pob and datetime.fromisoformat(poff) < datetime.fromisoformat(pob):
            return 'P.Off, POB\'dan önce olamaz', 400
        if poff and not pob and datetime.fromisoformat(poff) < datetime.fromisoformat(off_st):
            return 'P.Off, Off Station\'dan önce olamaz', 400
        if on_st and poff and datetime.fromisoformat(on_st) < datetime.fromisoformat(poff):
            return 'On Station, P.Off\'dan önce olamaz', 400
        if on_st and not poff and pob and datetime.fromisoformat(on_st) < datetime.fromisoformat(pob):
            return 'On Station, POB\'dan önce olamaz', 400
        if on_st and not poff and not pob and datetime.fromisoformat(on_st) < datetime.fromisoformat(off_st):
            return 'On Station, Off Station\'dan önce olamaz', 400
        is_tipi, k = detect_is_tipi(from_nokta, to_nokta)

    if off_st and pob and poff and on_st:
        base   = off_st[:10]+'T00:00:00'
        off_h  = dt_to_abs_hour(off_st, base)
        pob_h  = dt_to_abs_hour(pob,   base)
        poff_h = dt_to_abs_hour(poff,  base)
        on_h   = dt_to_abs_hour(on_st, base)
        if pob_h  < off_h:  pob_h  += 24
        if poff_h < pob_h:  poff_h += 24
        if on_h   < poff_h: on_h   += 24
        vessel = db.execute("SELECT grt FROM vessels WHERE id=?", (op['vessel_id'],)).fetchone()
        grt = vessel['grt'] if vessel else 8000
        katki = job_contrib(off_h, pob_h, poff_h, on_h, is_tipi, grt)
        prev = db.execute(
            "SELECT fatigue_toplam, on_station FROM operations WHERE pilot_id=? AND id!=? ORDER BY olusturma DESC LIMIT 1",
            (op['pilot_id'], op_id)
        ).fetchone()
        if prev:
            prev_on = prev['on_station'] if prev['on_station'] else off_st
            rest_h = (datetime.fromisoformat(off_st) - datetime.fromisoformat(prev_on)).total_seconds() / 3600
            if rest_h < 0: rest_h = 0
            prev_score = apply_recovery(prev['fatigue_toplam'], rest_h)
        else:
            prev_score = 0.0
        toplam = prev_score + katki
        norm   = normalize_score(toplam)
        _, durum = fatigue_color(toplam)
    else:
        katki  = op['fatigue_katki']  or 0
        toplam = op['fatigue_toplam'] or 0
        norm   = op['fatigue_norm']   or 0
        durum  = op['fatigue_durum']  or 'FIT'

    db.execute("""
        UPDATE operations SET
            from_nokta=?, to_nokta=?,
            off_station=?, pob=?, poff=?, on_station=?,
            is_tipi=?, k_carpan=?,
            fatigue_katki=?, fatigue_toplam=?, fatigue_norm=?, fatigue_durum=?
        WHERE id=?
    """, (
        from_nokta, to_nokta,
        off_st, pob, poff, on_st,
        is_tipi, k,
        katki, toplam, norm, durum,
        op_id
    ))
    db.commit()
    return 'ok', 200
    db.execute("""
        UPDATE operations SET
            from_nokta=?, to_nokta=?,
            off_station=?, pob=?, poff=?, on_station=?,
            is_tipi=?, k_carpan=?,
            fatigue_katki=?, fatigue_toplam=?, fatigue_norm=?, fatigue_durum=?
        WHERE id=?
    """, (
        from_nokta, to_nokta,
        off_st, pob, poff, on_st,
        is_tipi, k,
        katki, toplam, norm, durum,
        op_id
    ))
    db.commit()
    return 'ok', 200


# ── Operation Sil ─────────────────────────────────────────────
@app.route('/operations/<int:op_id>/sil', methods=['POST'])
def operation_sil(op_id):
    db = get_db()
    db.execute("DELETE FROM operations WHERE id=?", (op_id,))
    db.commit()
    return 'ok', 200