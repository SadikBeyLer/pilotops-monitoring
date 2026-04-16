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

# ── Veritabanı ───────────────────────────────────────────────
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
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    with open('pilotops_schema.sql', 'r', encoding='utf-8') as f:
        db.executescript(f.read())
    db.commit()
    db.close()

# ── Şamandıra listesi ────────────────────────────────────────
SAMANDIRALAR = ['wimba','g.nato','k.nato','sa/sa','petgaz','b.aygaz','k.aygaz','milangaz']

def detect_is_tipi(from_nokta: str, to_nokta: str) -> tuple:
    """
    From/To'ya göre iş tipini ve k çarpanını belirler.
    Döner: (is_tipi, k_carpan)
    """
    f = from_nokta.lower().strip()
    t = to_nokta.lower().strip()

    def is_sam(s):
        return any(sam in s for sam in SAMANDIRALAR)

    if is_sam(f):
        return ('buoy_kalkis', 0.7)
    if is_sam(t):
        return ('buoy_yanasma', 1.2)
    if f in ('pilot position', 'demir'):
        return ('yanasma', 1.0)
    if t in ('pilot position', 'demir'):
        return ('kalkis', 0.7)
    return ('yanasma', 1.0)

# ── Yardımcı: datetime string → float saat ──────────────────
def dt_to_abs_hour(dt_str: str, base_date: str = None) -> float:
    """ISO datetime string'i mutlak saate çevirir."""
    dt = datetime.fromisoformat(dt_str)
    if base_date:
        base = datetime.fromisoformat(base_date)
        delta = dt - base
        return delta.total_seconds() / 3600
    return dt.hour + dt.minute / 60

# ════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════

# ── Ana Sayfa: Kaptan listesi ────────────────────────────────
@app.route('/')
def index():
    db = get_db()
    watch = db.execute(
        "SELECT * FROM watches WHERE aktif=1 ORDER BY baslangic DESC LIMIT 1"
    ).fetchone()
    pilots = db.execute("SELECT * FROM v_pilot_current").fetchall()
    return render_template('index.html', watch=watch, pilots=pilots)

# ── Kaptanlar ────────────────────────────────────────────────
@app.route('/pilots')
def pilots():
    db = get_db()
    pilots = db.execute(
        "SELECT * FROM pilots WHERE aktif=1 ORDER BY ad_soyad"
    ).fetchall()
    return render_template('pilots.html', pilots=pilots)

@app.route('/pilots/add', methods=['GET','POST'])
def pilot_add():
    db = get_db()
    if request.method == 'POST':
        db.execute(
            "INSERT INTO pilots (port_id, ad_soyad, telefon) VALUES (?,?,?)",
            (1, request.form['ad_soyad'], request.form.get('telefon',''))
        )
        watch_id = int(request.form.get('watch_id', 2))
        db.execute("UPDATE watches SET aktif=0")
        db.execute("UPDATE watches SET aktif=1 WHERE id=?", (watch_id,))
        db.commit()
        return redirect(url_for('index'))
    watches = db.execute("SELECT * FROM watches ORDER BY id").fetchall()
    return render_template('pilot_add.html', watches=watches)

# ── Gemiler ──────────────────────────────────────────────────
@app.route('/vessels')
def vessels():
    db = get_db()
    vessels = db.execute(
        "SELECT * FROM vessels ORDER BY gelis_zamani DESC"
    ).fetchall()
    return render_template('vessels.html', vessels=vessels)

@app.route('/vessels/add', methods=['GET','POST'])
def vessel_add():
    if request.method == 'POST':
        db = get_db()
        db.execute("""
            INSERT INTO vessels
            (imo_no, gemi_adi, tip, bayrak, grt, loa,
             thruster_bas, thruster_kic, tehlikeli_yuk, not_alani,
             from_liman, to_liman, gelis_zamani, durum)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            request.form.get('durum','yolda'),
        ))
        db.commit()
        return redirect(url_for('vessels'))
    return render_template('vessel_add.html')

# ── İş Girişi ────────────────────────────────────────────────
@app.route('/operations/add', methods=['GET','POST'])
def operation_add():
    db = get_db()
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

        # İş tipini belirle
        is_tipi, k = detect_is_tipi(from_nokta, to_nokta)

        # Aktif nöbet
        watch = db.execute(
            "SELECT id FROM watches WHERE aktif=1 ORDER BY baslangic DESC LIMIT 1"
        ).fetchone()
        watch_id = watch['id'] if watch else 1

        # Fatigue hesapla
        base = off_st[:10] + 'T00:00:00'
        off_h  = dt_to_abs_hour(off_st, base)
        pob_h  = dt_to_abs_hour(pob,    base)
        poff_h = dt_to_abs_hour(poff,   base)
        on_h   = dt_to_abs_hour(on_st,  base)
        if pob_h  < off_h:  pob_h  += 24
        if poff_h < pob_h:  poff_h += 24
        if on_h   < poff_h: on_h   += 24

        # Geminin GRT'si
        vessel = db.execute("SELECT grt FROM vessels WHERE id=?", (vessel_id,)).fetchone()
        grt = vessel['grt'] if vessel else 8000

        katki = job_contrib(off_h, pob_h, poff_h, on_h, is_tipi, grt)

        # Önceki toplam skoru al
        prev = db.execute(
            "SELECT fatigue_toplam, on_station FROM operations WHERE pilot_id=? ORDER BY olusturma DESC LIMIT 1",
            (pilot_id,)
        ).fetchone()

        if prev:
            rest_h = (datetime.fromisoformat(off_st) - datetime.fromisoformat(prev['on_station'])).total_seconds() / 3600
            if rest_h < 0: rest_h = 0
            prev_score = apply_recovery(prev['fatigue_toplam'], rest_h)
        else:
            prev_score = 0.0

        toplam = prev_score + katki
        norm   = normalize_score(toplam)
        color, durum = fatigue_color(toplam)
        score_fmt = format_score(toplam)

        # Zorunlu atama kontrolü
        zorunlu = 1 if norm >= 90 else 0
        onaylayan = request.form.get('onaylayan', '') if norm >= 75 else ''

        db.execute("""
            INSERT INTO operations
            (pilot_id, vessel_id, watch_id, from_nokta, to_nokta,
             is_tipi, k_carpan, off_station, pob, poff, on_station,
             draft_bas, draft_kic, fatigue_katki, fatigue_toplam,
             fatigue_norm, fatigue_durum, zorunlu_atama, onaylayan)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pilot_id, vessel_id, watch_id, from_nokta, to_nokta,
            is_tipi, k, off_st, pob, poff, on_st,
            draft_bas, draft_kic, katki, toplam,
            norm, durum, zorunlu, onaylayan
        ))
        db.commit()
        return redirect(url_for('index'))

    pilots  = db.execute("SELECT * FROM pilots WHERE aktif=1 ORDER BY ad_soyad").fetchall()
    vessels = db.execute("SELECT * FROM vessels ORDER BY gemi_adi").fetchall()
    return render_template('operation_add.html', pilots=pilots, vessels=vessels,
                           samandiralar=SAMANDIRALAR)

# ── API: Kaptan fatigue skoru (AJAX için) ────────────────────
@app.route('/api/pilot/<int:pilot_id>/fatigue')
def api_pilot_fatigue(pilot_id):
    db = get_db()
    row = db.execute(
        "SELECT fatigue_toplam, fatigue_norm, fatigue_durum, on_station FROM operations WHERE pilot_id=? ORDER BY olusturma DESC LIMIT 1",
        (pilot_id,)
    ).fetchone()
    if not row:
        return jsonify({'ham': 0, 'norm': 0, 'durum': 'FIT', 'score_fmt': '0'})
    return jsonify({
        'ham':       round(row['fatigue_toplam'], 3),
        'norm':      row['fatigue_norm'],
        'durum':     row['fatigue_durum'],
        'score_fmt': format_score(row['fatigue_toplam'])
    })

# ── API: İş tipi tespiti (AJAX için) ────────────────────────
@app.route('/api/detect-tip')
def api_detect_tip():
    from_n = request.args.get('from','')
    to_n   = request.args.get('to','')
    tip, k = detect_is_tipi(from_n, to_n)
    labels = {
        'yanasma':      ('Yanaşma',            '#27500A', '#EAF3DE'),
        'kalkis':       ('Kalkış',              '#0C447C', '#E6F1FB'),
        'buoy_yanasma': ('Şamandıra yanaşma',   '#633806', '#FAEEDA'),
        'buoy_kalkis':  ('Şamandıra kalkış',    '#3C3489', '#EEEDFE'),
    }
    label, color, bg = labels.get(tip, ('Belirsiz', '#888', '#eee'))
    return jsonify({'tip': tip, 'k': k, 'label': label, 'color': color, 'bg': bg})

# ── Uygulama başlangıcı ──────────────────────────────────────
# ── Pilot Jobs ───────────────────────────────────────────────
@app.route('/pilots/<int:pilot_id>/jobs')
def pilot_jobs(pilot_id):
    db = get_db()
    pilot = db.execute("SELECT * FROM pilots WHERE id=?", (pilot_id,)).fetchone()
    jobs = db.execute("""
        SELECT o.*, v.gemi_adi, v.tip, v.grt
        FROM operations o
        JOIN vessels v ON v.id = o.vessel_id
        WHERE o.pilot_id = ?
        ORDER BY o.off_station DESC
    """, (pilot_id,)).fetchall()
    return render_template('pilot_jobs.html', pilot=pilot, jobs=jobs)
if __name__ == '__main__':
    if not os.path.exists(DATABASE):
        init_db()
        print(f"Veritabanı oluşturuldu: {DATABASE}")
    app.run(debug=True, port=5001)

# Railway için — her başlangıçta DB yoksa oluştur
with app.app_context():
    if not os.path.exists(DATABASE):
        init_db()
