import math

# ============================================================
# PilotOps Fatigue Engine v2.0
# ============================================================
# Kalibrasyon: Sadık Kılkış 14–15 Nisan 12 iş = 100
# MAX_FATIGUE = 10.1389
# K_RECOVERY  = 0.1097
# PREP_TIME   = 1.0 saat (yemek/duş — uyku başlamadan önce)
# ============================================================

MAX_FATIGUE = 10.1389
K_RECOVERY  = 0.1097
PREP_TIME   = 1.0

# ── Operasyon tipi çarpanları ────────────────────────────────
# berthing   = yanaşma        k = 1.00
# unberthing = kalkış         k = 0.70
# transfer   = araç/motor     k = 0.50
# buoy       = şamandıra      k = 1.20

OP_WEIGHTS = {
    "berthing":   1.00,
    "unberthing": 0.70,
    "transfer":   0.50,
    "buoy":       1.20,
}

# ── GRT büyüklük çarpanı ─────────────────────────────────────
# < 5.000 GRT  → ×1.00
# 5–20k GRT    → ×1.05
# > 20.000 GRT → ×1.10  (max %10 fark)

def grt_factor(grt: float) -> float:
    if grt < 5_000:  return 1.00
    if grt < 20_000: return 1.05
    return 1.10

# ── Gece çarpanı ─────────────────────────────────────────────
# 00:00–05:00 → ×1.30  (circadian trough)
# 05:00–07:00 → ×1.20  (erken sabah)
# 07:00–24:00 → ×1.00  (gündüz)

def night_factor(hour: float) -> float:
    h = hour % 24
    if 0 <= h < 5: return 1.30
    if 5 <= h < 7: return 1.20
    return 1.00

# ── Operasyon katkısı ────────────────────────────────────────
# start_hour : mutlak saat (gün*24 + saat, örn. 15/02:30 → 26.5)
# duration_h : süre (saat cinsinden)
# op_type    : "berthing" | "unberthing" | "transfer" | "buoy"
# grt        : geminin GRT değeri

def operation_contrib(start_hour: float, duration_h: float,
                       op_type: str, grt: float) -> float:
    kt    = OP_WEIGHTS.get(op_type, 1.0)
    kg    = grt_factor(grt)
    total = 0.0
    cur   = start_hour
    end   = start_hour + duration_h
    while cur < end:
        nx     = min(cur + 0.25, end)
        total += kt * kg * (nx - cur) * night_factor(cur)
        cur    = nx
    return total

# ── Bir operasyonun toplam katkısı (transfer + gemi + dönüş) ─
# Her iş 4 zaman damgasından oluşur:
#   OffStation → POB → POff → OnStation
#
# OffStation→POB : transfer gidiş  (k = transfer = 0.50)
# POB→POff       : gemideki süre   (k = op_type)
# POff→OnStation : transfer dönüş  (k = transfer = 0.50)

def job_contrib(off_station: float, pob: float,
                poff: float, on_station: float,
                op_type: str, grt: float) -> float:
    t_out    = pob - off_station
    on_board = poff - pob
    t_in     = on_station - poff
    return (
        operation_contrib(off_station, t_out,    "transfer", grt) +
        operation_contrib(pob,         on_board, op_type,    grt) +
        operation_contrib(poff,        t_in,     "transfer", grt)
    )

# ── Dinlenme sonrası iyileşme ────────────────────────────────
# rest_hours : istasyona dönüşten sonraki toplam ara (saat)
# İlk PREP_TIME (1 saat) yemek/duş → uyku sayılmaz
# Formül: F_yeni = F * exp(−K * max(0, rest − PREP_TIME))
# Örnek: 12 saat ara → 11 saat uyku → skor ~%70 düşer → Fit üst sınırı

def apply_recovery(fatigue: float, rest_hours: float) -> float:
    sleep_h = max(0.0, rest_hours - PREP_TIME)
    return fatigue * math.exp(-K_RECOVERY * sleep_h)

# ── Operasyon listesinden kümülatif fatigue ──────────────────
# operations: liste, her eleman dict:
#   {
#     "off_station": float,  # mutlak saat
#     "pob":         float,
#     "poff":        float,
#     "on_station":  float,
#     "op_type":     str,
#     "grt":         float,
#   }
# İşler arasındaki dinlenme otomatik hesaplanır.

def calculate_fatigue(operations: list) -> float:
    score    = 0.0
    prev_on  = None
    for op in operations:
        if prev_on is not None:
            rest = max(0.0, op["off_station"] - prev_on)
            score = apply_recovery(score, rest)
        score += job_contrib(
            op["off_station"], op["pob"],
            op["poff"],        op["on_station"],
            op["op_type"],     op["grt"]
        )
        prev_on = op["on_station"]
    return score

# ── Normalize (0–100, üstü "100+") ──────────────────────────
# ham > MAX_FATIGUE → "123+" gibi gösterilir, sistem engellemez

def normalize_score(ham: float) -> int:
    return round(ham / MAX_FATIGUE * 100)

def format_score(ham: float) -> str:
    n = normalize_score(ham)
    return f"{n}+" if n > 100 else str(n)

# ── Renk ve durum ────────────────────────────────────────────
# Döner: (renk, durum_etiketi)
# Renkler: "GREEN" | "BLUE" | "ORANGE" | "RED" | "RED+"
#
# FİT      0–29   → GREEN
# YORGUN  30–49   → BLUE
# DİKKAT  50–74   → ORANGE
# BLOK    75–89   → RED
# KRİTİK  90–100  → RED
# KRİTİK+ >100    → RED+  (sistem engellemez, log yazar)

def fatigue_color(ham: float) -> tuple:
    n = normalize_score(ham)
    if n > 100: return ("RED+",   "KRİTİK+")
    if n >= 90: return ("RED",    "KRİTİK")
    if n >= 75: return ("RED",    "BLOK")
    if n >= 50: return ("ORANGE", "DİKKAT")
    if n >= 30: return ("BLUE",   "YORGUN")
    return             ("GREEN",  "FİT")

# ── Pilot listesini en yorgundan en fite sırala ──────────────
# pilot_list: her eleman en az {"name": str, "fatigue": float}
# Döner: aynı liste + "color", "status", "score_fmt" alanları eklenir
# En yorgun (yüksek ham skor) → üstte, kırmızı

def sort_pilots(pilot_list: list) -> list:
    for p in pilot_list:
        color, status = fatigue_color(p["fatigue"])
        p["color"]      = color
        p["status"]     = status
        p["score_norm"] = normalize_score(p["fatigue"])
        p["score_fmt"]  = format_score(p["fatigue"])
    return sorted(pilot_list, key=lambda x: x["fatigue"], reverse=True)

# ── Kümülatif birikim (günler arası) ────────────────────────
# Önceki günün %75'i + bugünkü (MLC 2006 uyumlu)

def cumulative_fatigue(prev: float, today: float) -> float:
    return 0.75 * prev + today

# ── MLC 2006 kontrol ─────────────────────────────────────────
# Döner: {"daily_ok": bool, "rest_ok": bool,
#          "weekly_ok": bool, "violations": list}

def mlc_check(work_hours: float, min_rest: float,
              weekly_hours: float = None) -> dict:
    violations = []
    daily_ok  = work_hours <= 14
    rest_ok   = min_rest >= 10
    weekly_ok = True
    if not daily_ok:
        violations.append(f"Günlük 14s aşıldı: {work_hours:.1f}s")
    if not rest_ok:
        violations.append(f"Min 10s dinlenme sağlanamadı: {min_rest:.1f}s")
    if weekly_hours is not None:
        weekly_ok = weekly_hours <= 72
        if not weekly_ok:
            violations.append(f"Haftalık 72s aşıldı: {weekly_hours:.1f}s")
    return {
        "daily_ok":   daily_ok,
        "rest_ok":    rest_ok,
        "weekly_ok":  weekly_ok,
        "violations": violations,
    }
