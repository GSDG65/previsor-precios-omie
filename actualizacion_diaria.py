# -*- coding: utf-8 -*-
"""
actualizacion_diaria.py  ·  Fase 2 del previsor de precios OMIE
-----------------------------------------------------------------
Pensado para ejecutarse una vez al día por la tarde (Programador de tareas),
cuando OMIE ya ha publicado el precio de mañana (D+1). Hace:

  1. Actualiza los datos: precios OMIE (hasta D+1), demanda y generación REE,
     embalses, gas MIBGAS y meteo (reanálisis + previsión a 7 días).
     Si el PC estuvo apagado varios días, recupera los que falten.
  2. Emite la previsión de D+1 a D+5 y la CONGELA en datos/previsiones/
     (nunca se sobrescribe: es la base honesta para comparar con la realidad).
  3. Verifica las previsiones anteriores contra el precio real y recalibra
     el sesgo y la anchura de la horquilla por horizonte y franja.
  4. Escribe datos/previsor_datos.js, que lee la herramienta HTML.

Uso:
  python actualizacion_diaria.py             actualización normal
  python actualizacion_diaria.py --sin-descarga   solo recalcula con los datos en disco
  python actualizacion_diaria.py --forzar         re-emite la previsión de hoy aunque ya exista
"""
import sys, json, glob, time
import datetime as dt
from previsor_comun import *
from previsor_modelo import *
import carga_historico as ch

HOY = dt.date.today()
PREV_DIR = os.path.join(DATOS, "previsiones")
os.makedirs(PREV_DIR, exist_ok=True)
REGISTRO = "registro_previsiones.csv"
CAB_REG = ["clave", "emitida", "objetivo", "horizonte", "franja", "p10", "p50", "p90",
           "coste", "p50_bruto", "semi_bruta", "sesgo_cal", "ensanche"]
HORIZONTES = [1, 2, 3, 4, 5]
# Desde esta fecha la horquilla diaria se calcula con el precio medio de los análogos;
# solo las previsiones posteriores sirven para calibrar su anchura.
CAMBIO_HORQUILLA_DIA = "2026-10-26"
# Calibración del sesgo: últimos SESGO_VENTANA casos por franja, ponderados con decaimiento geométrico.
# 0.93 → 0.85 el 2-nov-2026 para recoger antes los cambios de régimen (parada nuclear, salto del gas).
SESGO_DECAIMIENTO = 0.85
SESGO_VENTANA = 30
# Sesgo por tramo de viento (23-nov-2026): el error del modelo depende del viento previsto (con mucho
# viento sobrestima el precio bastante más que con poco), y un único sesgo por franja no corrige los dos casos.
# Las previsiones verificadas de cada franja se parten por la mediana de la eólica prevista del día y el
# sesgo se calcula solo con las del mismo tramo (todos los horizontes juntos). Si un tramo tiene menos de
# 10 casos se usa el sesgo de siempre. Prueba con las previsiones congeladas (24-oct a 22-nov): error del
# precio diario −1 %, de las franjas −5 % y horquilla algo más fiable; mejor en D+5, algo peor en D+2–D+4.
# Se desactiva con "sesgo_por_viento": false en config_previsor.json.
SESGO_POR_VIENTO = True

# ============================================================================
# Meteo prevista → índices horarios
# ============================================================================
def indices_previstos(cfg, futuro, analisis):
    """Índices horarios para hoy+ (previsión) y registro de índices de análisis para corregir sesgo modelo/reanálisis."""
    fechas = sorted({f for d in futuro.values() for f in d})
    prev = {}
    for f in fechas:
        r = indices_dia(cfg, futuro, f)
        if all(x is not None for x in r["eol"] + r["sol"] + r["temp"]):
            prev[f] = r
    # registro de los últimos días según la API de previsión (para comparar con el reanálisis)
    cab, reg = leer_tabla("meteo_prevision_vs_reanalisis.csv")
    for f in sorted({f for d in analisis.values() for f in d}):
        r = indices_dia(cfg, analisis, f)
        if all(x is not None for x in r["eol"] + r["sol"]):
            reg[f] = {"fecha": f, "eol_prev": round(media(r["eol"]), 5), "sol_prev": round(media(r["sol"]), 5)}
    escribir_tabla("meteo_prevision_vs_reanalisis.csv", ["fecha", "eol_prev", "sol_prev"], reg)
    return prev

def correccion_meteo(base):
    """Cociente reanálisis / modelo de previsión en los últimos 30 días con ambos datos (1 si no hay)."""
    _, reg = leer_tabla("meteo_prevision_vs_reanalisis.csv")
    fin_archivo = (HOY - dt.timedelta(days=6)).isoformat()
    pares = [(f, r) for f, r in reg.items() if f <= fin_archivo and f in base.eolI][-30:]
    if len(pares) < 7:
        return 1.0, 1.0, len(pares)
    se = sum(media(base.eolI[f]) for f, _ in pares); pe = sum(float(r["eol_prev"]) for _, r in pares)
    ss = sum(media(base.solI[f]) for f, _ in pares); ps = sum(float(r["sol_prev"]) for _, r in pares)
    ce = max(0.7, min(1.4, se / pe)) if pe else 1.0
    cs = max(0.7, min(1.4, ss / ps)) if ps else 1.0
    return ce, cs, len(pares)

# ============================================================================
# Calibración a partir de previsiones verificadas
# ============================================================================
def publicados_al_emitir():
    """{(emitida, objetivo)} cuyo precio real OMIE ya estaba publicado al emitir (real_horas en el JSON congelado).
    Esas filas no son previsiones: se excluyen de la calibración y de las métricas."""
    pub = set()
    for a in glob.glob(os.path.join(PREV_DIR, "prevision_*.json")):
        try:
            with open(a, encoding="utf-8") as f:
                p = json.load(f)
        except Exception:
            continue
        for d in p.get("dias", []):
            if d.get("real_horas"):
                pub.add((p["emitida"], d["fecha"]))
    return pub

def eolica_prevista_por_dia():
    """{(emitida, objetivo): eólica media prevista en MW} leída de las previsiones congeladas."""
    eol = {}
    for a in glob.glob(os.path.join(PREV_DIR, "prevision_*.json")):
        try:
            with open(a, encoding="utf-8") as f:
                p = json.load(f)
        except Exception:
            continue
        for d in p.get("dias", []):
            v = (d.get("entradas") or {}).get("eolica_mw")
            if v and None not in v:
                eol[(p["emitida"], d["fecha"])] = media(v)
    return eol

def calibrar(base, cfg=None):
    """Devuelve {horizonte: {"sesgo": {franja: ratio}, "ensanche": f, "n": n}} y la tabla de verificación."""
    _, reg = leer_tabla(REGISTRO)
    pub = publicados_al_emitir()
    eol_prev = eolica_prevista_por_dia()
    verif = {}
    por_h = {}
    por_franja_eol = {}          # franja → [(emitida, eólica prevista MW, (real − p50 bruto)/coste)]
    for k, r in reg.items():
        obj = r["objetivo"]
        p = base.precio.get(obj)
        if not p or any(x is None for x in p):
            continue
        fr = r["franja"]
        if fr == "dia":
            real = media(p)
        else:
            a, b = next((a, b) for n, a, b in FRANJAS if n == fr)
            real = media(p[a:b])
        p10, p50, p90, coste = (float(r[x]) for x in ("p10", "p50", "p90", "coste"))
        publicado = int((r["emitida"], obj) in pub)
        verif[k] = {**r, "real": round(real, 2), "error": round(p50 - real, 2),
                    "dentro": int(p10 <= real <= p90), "publicado": publicado}
        if publicado:
            continue          # el real ya se conocía al emitir: no enseña nada al calibrador
        por_h.setdefault(int(r["horizonte"]), []).append((r["emitida"], fr, real, coste,
                                                          float(r["p50_bruto"]), float(r["semi_bruta"])))
        e = eol_prev.get((r["emitida"], obj))
        if fr != "dia" and e is not None:
            por_franja_eol.setdefault(fr, []).append((r["emitida"], obj, e, (real - float(r["p50_bruto"])) / coste))
    # Mientras un horizonte no tenga casos suficientes, se usan los de todos los horizontes juntos
    # (cada franja tiene un comportamiento parecido a 2, 3, 4 o 5 días vista).
    todas = sorted(f for h in HORIZONTES for f in por_h.get(h, []))

    def media_decaida(xs):
        xs = xs[-SESGO_VENTANA:]
        if len(xs) < 10:
            return None
        pesos = [SESGO_DECAIMIENTO ** (len(xs) - 1 - i) for i in range(len(xs))]
        s = sum(x * w for x, w in zip(xs, pesos)) / sum(pesos)
        return round(max(-0.4, min(0.4, s)), 4)

    def sesgo_de(filas_sel, n):
        return media_decaida([(real - p50b) / coste for _, fr, real, coste, p50b, _ in filas_sel if fr == n])

    # sesgo por tramo de viento: {franja: {"umbral_mw": mediana, "bajo": s | None, "alto": s | None}}
    por_viento = {}
    if (cfg or {}).get("sesgo_por_viento", SESGO_POR_VIENTO):
        for n, _, _ in FRANJAS:
            filas_n = sorted(por_franja_eol.get(n, []))
            if len(filas_n) < 20:
                continue
            eols = sorted(e for _, _, e, _ in filas_n)
            umbral = eols[len(eols) // 2]
            por_viento[n] = {"umbral_mw": round(umbral),
                             "bajo": media_decaida([x for _, _, e, x in filas_n if e < umbral]),
                             "alto": media_decaida([x for _, _, e, x in filas_n if e >= umbral])}

    # ensanche: el que habría hecho falta para que el 80 % de los reales cayera dentro
    def ens_de(filas_sel, minimo):
        rel = [abs(real - p50b) / semi for _, _, real, _, p50b, semi in filas_sel if semi > 0.5][-60:]
        if len(rel) < minimo:
            return None
        rel.sort()
        return round(max(0.4, min(3.0, rel[int(0.8 * (len(rel) - 1))])), 3)

    def primero(*vals):
        return next((v for v in vals if v is not None), None)

    cal = {}
    for h in HORIZONTES:
        filas = sorted(por_h.get(h, []))
        sesgo, origen = {}, {}
        for n, _, _ in FRANJAS:
            propio, comun = sesgo_de(filas, n), sesgo_de(todas, n)
            v = primero(propio, comun)
            if v is not None:
                sesgo[n] = v
                origen[n] = "horizonte" if propio is not None else "todos"
        sin_dia = lambda fs: [f for f in fs if f[1] != "dia"]
        ens_global = primero(ens_de(sin_dia(filas), 30), ens_de(sin_dia(todas), 30), 1.0)
        ens_f = {"_global": ens_global}
        for n, _, _ in FRANJAS:
            ens_f[n] = primero(ens_de([f for f in filas if f[1] == n], 15),
                               ens_de([f for f in todas if f[1] == n], 15), ens_global)
        dia_ok = lambda fs: [f for f in fs if f[1] == "dia" and f[0] >= CAMBIO_HORQUILLA_DIA]
        ens_f["dia"] = primero(ens_de(dia_ok(filas), 10), ens_de(dia_ok(todas), 10), 1.0)
        cal[h] = {"sesgo": sesgo, "origen_sesgo": origen, "ensanche": ens_global,
                  "ensanche_franja": ens_f, "n": len(filas), "sesgo_viento": por_viento}
    return cal, verif

def _fmt(s):
    return "–" if s is None else f"{s:+.3f}"

def sesgo_para_dia(c_h, eol_mw):
    """Sesgo por franja que se aplica a un día: el de su tramo de viento si existe; si no, el de siempre.
    Devuelve (sesgo, {franja: "bajo" | "alto" | "general"})."""
    sesgo, tramo = dict(c_h.get("sesgo", {})), {}
    for n, pv in (c_h.get("sesgo_viento") or {}).items():
        t = "alto" if eol_mw >= pv["umbral_mw"] else "bajo"
        if pv.get(t) is not None:
            sesgo[n] = pv[t]
            tramo[n] = t
    for n, _, _ in FRANJAS:
        tramo.setdefault(n, "general")
    return sesgo, tramo

# ============================================================================
# Emisión
# ============================================================================
# ============================================================================
# Meteo de varios modelos (ECMWF, ICON, Météo-France, GFS, UKMO) para los horizontes lejanos
# ============================================================================
# Prueba con el último año (nov-2026): con la media de modelos el error del precio baja ~7 % en D+4
# y ~8 % en D+5; en D+1–D+3 no mejora a la previsión por defecto. Se puede cambiar en config.
MEDIA_MODELOS_HORIZONTES = [4, 5]
CSV_MEDIA = "meteo_media_prevista.csv"

def media_modelos(cfg, fecha):
    """Índices eol/sol medios de los modelos con el día completo (None si hay menos de 2)."""
    res, usados = {}, {}
    for var in ("eol", "sol"):
        vals = {}
        for m, pts in ch.FUTURO_MODELOS.items():
            v = indices_dia(cfg, {n: {fecha: {**{k: [None] * 24 for k in VARS_HORARIAS.split(",")}, **d[fecha]}}
                                  for n, d in pts.items() if fecha in d}, fecha)[var]
            if all(x is not None for x in v):
                vals[m] = v
        if len(vals) < 2:
            return None, {}
        res[var] = [sum(v[h] for v in vals.values()) / len(vals) for h in range(24)]
        usados[var] = {m: round(media(v), 4) for m, v in vals.items()}
    return res, usados

def correccion_media(base, hz, emitida):
    """Ratio reanálisis/media de modelos para el horizonte hz: con las medias guardadas de los 30 días
    previos si hay al menos 15; si no, con el sesgo medido en evaluacion_meteo_modelos.csv."""
    _, reg = leer_tabla(CSV_MEDIA)
    e = dt.date.fromisoformat(emitida)
    ventana = {(e - dt.timedelta(days=k)).isoformat() for k in range(7, 37)}
    out = {}
    for var, ref in (("eol", base.eolI), ("sol", base.solI)):
        a = b = 0.0; n = 0
        for r in reg.values():
            if int(r["horizonte"]) == hz and r["fecha"] in ventana and r.get(f"{var}_media") and ref.get(r["fecha"]) \
                    and None not in ref[r["fecha"]]:
                a += media(ref[r["fecha"]]); b += float(r[f"{var}_media"]); n += 1
        if n >= 15 and b:
            out[var] = (max(0.7, min(1.4, a / b)), f"30 días ({n})")
            continue
        _, ev = leer_tabla("evaluacion_meteo_modelos.csv")
        fila = ev.get(f"{var}|media_modelos|{hz}")
        if fila and fila.get("sesgo_pct") not in (None, ""):
            out[var] = (1 / (1 + float(fila["sesgo_pct"]) / 100), "evaluación anual")
        else:
            out[var] = (1.0, "sin corrección")
    return out

def guardar_medias(emitida, medias):
    cab, reg = leer_tabla(CSV_MEDIA)
    for (fecha, hz), v in medias.items():
        k = f"{emitida}|{fecha}"
        reg[k] = {"clave": k, "emitida": emitida, "fecha": fecha, "horizonte": hz,
                  "eol_media": round(media(v["eol"]), 5), "sol_media": round(media(v["sol"]), 5)}
    escribir_tabla(CSV_MEDIA, ["clave", "emitida", "fecha", "horizonte", "eol_media", "sol_media"], reg)

def emitir(cfg, base, an, prev_meteo, cal, forzar=False):
    emitida = HOY.isoformat()
    ruta = os.path.join(PREV_DIR, f"prevision_{emitida}.json")
    if os.path.exists(ruta) and not forzar:
        log(f"   la previsión de {emitida} ya está congelada; no se re-emite (usa --forzar para rehacerla)")
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)

    ce_corr, cs_corr, n_corr = correccion_meteo(base)
    log(f"   corrección meteo previsión→reanálisis: eólica ×{ce_corr:.2f}, solar ×{cs_corr:.2f} ({n_corr} días)")
    ult_gas = max(f for f, v in base.gas.items() if v is not None)
    gas_act = base.gas[ult_gas]
    nuc_f = base.ultimo_con({f: base.nuclear_mw(f) for f in base.gen}, emitida)
    nuc = base.nuclear_mw(nuc_f)
    llen = base.llenado(emitida)
    cap = base.cap_actual
    f_dem = factor_nivel_demanda(base, emitida)
    s_rec = sesgo_reciente(base, an, emitida)
    log(f"   gas {gas_act:.2f} €/MWh ({ult_gas}) · nuclear {nuc:.0f} MW ({nuc_f}) · embalses {llen} % · "
        f"pot. efectiva eól {cap[0]:.0f} / FV {cap[1]:.0f} MW · nivel demanda ×{f_dem:.3f}")

    # días de referencia para la explicación: últimas 4 semanas reales
    refs = [h for h in an.dias if h["fecha"] < emitida][-28:]
    precio_ref = media([media(base.precio[h["fecha"]]) for h in refs])
    coste_ref = media([base.coste_ccgt(h["fecha"]) for h in refs])
    ratio_ref = media([h["ratio_dia"] for h in refs])
    beta = ajustar_explicacion(an, emitida)

    dias = []
    medias_dia = {}
    for hz in HORIZONTES:
        obj = (HOY + dt.timedelta(days=hz)).isoformat()
        m = prev_meteo.get(obj)
        if not m:
            log(f"   ! sin meteo prevista para {obj}")
            continue
        temp_m = media(m["temp"])
        dem = prever_demanda(base, obj, temp_m, emitida)
        if not dem:
            log(f"   ! sin previsión de demanda para {obj}")
            continue
        dem = [x * f_dem for x in dem]
        eolI = [x * ce_corr for x in m["eol"]]
        solI = [x * cs_corr for x in m["sol"]]
        info_meteo = {"fuente": "por defecto (best_match)", "eol_defecto": round(media(m["eol"]), 4),
                      "sol_defecto": round(media(m["sol"]), 4)}
        mm, usados = media_modelos(cfg, obj) if ch.FUTURO_MODELOS else (None, {})
        if mm:
            medias_dia[(obj, hz)] = mm
            info_meteo.update({"eol_media": round(media(mm["eol"]), 4), "sol_media": round(media(mm["sol"]), 4),
                               "modelos_eol": usados["eol"], "modelos_sol": usados["sol"]})
            if hz in cfg.get("media_modelos_horizontes", MEDIA_MODELOS_HORIZONTES):
                cm = correccion_media(base, hz, emitida)
                eolI = [x * cm["eol"][0] for x in mm["eol"]]
                solI = [x * cm["sol"][0] for x in mm["sol"]]
                info_meteo.update({"fuente": f"media de {len(usados['eol'])} modelos",
                                   "correccion": {k: [round(v[0], 3), v[1]] for k, v in cm.items()}})
        d = {"fecha": obj, "dem": dem, "eolI": eolI, "solI": solI, "nuc": nuc, "cap": cap, "temp": m["temp"],
             "llenado": llen, "tipo": tipo_dia(dt.date.fromisoformat(obj))}
        d["R"] = base.residual(dem, eolI, solI, nuc, cap)
        coste = base.coste_ccgt(obj, gas=base.gas.get(obj, gas_act))
        c_h = cal.get(hz, {"sesgo": {}, "ensanche": 1.0, "ensanche_franja": {}})
        decaimiento = max(0.4, 1.0 - 0.12 * (hz - 1))          # el sesgo reciente pesa menos a más horizonte
        ajuste_bruto = {n: s_rec[n] * decaimiento for n, _, _ in FRANJAS}
        bruto = prever_dia(an, d, emitida, coste, ajuste=ajuste_bruto)
        sesgo_dia, tramo_viento = sesgo_para_dia(c_h, media(eolI) * cap[0])
        ajuste = {n: ajuste_bruto[n] + sesgo_dia.get(n, 0.0) for n, _, _ in FRANJAS}
        pr = prever_dia(an, d, emitida, coste, ajuste=ajuste,
                        ensanche=c_h.get("ensanche_franja") or c_h["ensanche"])
        real_h = base.precio.get(obj) if hz == 1 else None
        dias.append({
            "fecha": obj, "horizonte": hz, "tipo_dia": d["tipo"],
            "franjas": pr["franjas"], "horas": pr["horas"], "dia": pr["dia"],
            "bruto": {"franjas": bruto["franjas"], "dia": bruto["dia"]},
            "real_horas": [round(x, 2) for x in real_h] if real_h and all(x is not None for x in real_h) else None,
            "semaforo": semaforo(pr, coste, precio_ref),
            "factores": explicar(beta, d, refs, coste, coste_ref, ratio_ref),
            "meteo": info_meteo,
            "analogos": pr["analogos"],
            "entradas": {"coste_ccgt": round(coste, 2), "temp_media": round(temp_m, 1),
                         "demanda_mw": [round(x) for x in dem],
                         "eolica_mw": [round(x * cap[0]) for x in eolI],
                         "solar_mw": [round(x * cap[1]) for x in solI],
                         "nuclear_mw": round(nuc), "residual_mw": [round(x) for x in d["R"]],
                         "llenado_pct": llen,
                         # Portugal: índices previstos guardados para evaluarlos (el modelo aún no los usa)
                         "portugal": {k: [None if x is None else round(x, 4) for x in m[k]]
                                      for k in ("eol_pt", "sol_pt", "temp_pt") if m.get(k)}},
            "sesgo_cal": sesgo_dia, "tramo_viento": tramo_viento, "ensanche": c_h["ensanche"],
            "ensanche_franja": c_h.get("ensanche_franja", {}),
        })
    if medias_dia:
        try:
            guardar_medias(emitida, medias_dia)
        except Exception as e:
            log(f"   ! no se pudo guardar la media de modelos: {e}")
    usa = [d["fecha"] for d in dias if d["meteo"]["fuente"].startswith("media")]
    log(f"   meteo: media de varios modelos en {len(usa)} días ({', '.join(usa) or 'ninguno'})")
    salida = {"emitida": emitida, "generada": dt.datetime.now().isoformat(timespec="seconds"),
              "gas": gas_act, "gas_fecha": ult_gas, "precio_ref_28d": round(precio_ref, 2),
              "coste_ref_28d": round(coste_ref, 2), "cap_efectiva_mw": [round(cap[0]), round(cap[1])],
              "correccion_meteo": [round(ce_corr, 3), round(cs_corr, 3)], "sesgo_reciente": s_rec,
              "dias": dias}
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False)

    # registro plano (una fila por día objetivo y franja)
    _, reg = leer_tabla(REGISTRO)
    for dd in dias:
        filas = [("dia", dd["dia"], dd["bruto"]["dia"])] + \
                [(n, dd["franjas"][n], dd["bruto"]["franjas"][n]) for n, _, _ in FRANJAS]
        for fr, (p10, p50, p90), (b10, b50, b90) in filas:
            clave = f"{emitida}|{dd['fecha']}|{fr}"
            reg[clave] = {"clave": clave, "emitida": emitida, "objetivo": dd["fecha"], "horizonte": dd["horizonte"],
                          "franja": fr, "p10": p10, "p50": p50, "p90": p90, "coste": dd["entradas"]["coste_ccgt"],
                          "p50_bruto": b50, "semi_bruta": round((b90 - b10) / 2, 2),
                          "sesgo_cal": dd["sesgo_cal"].get(fr, 0.0) if fr != "dia" else "",
                          "ensanche": dd.get("ensanche_franja", {}).get(fr, dd["ensanche"])}
    escribir_tabla(REGISTRO, CAB_REG, reg)
    log(f"   previsión congelada: {os.path.basename(ruta)} ({len(dias)} días)")
    return salida

# ============================================================================
# Exportación para la herramienta HTML
# ============================================================================
def exportar(base, prevision, cal, verif):
    # previsiones congeladas de los últimos 75 días (para comparar previsión y realidad día a día)
    archivos = sorted(glob.glob(os.path.join(PREV_DIR, "prevision_*.json")))[-80:]
    prev_por_dia = {}
    for a in archivos:
        try:
            with open(a, encoding="utf-8") as f:
                p = json.load(f)
        except Exception:
            continue
        for d in p.get("dias", []):
            prev_por_dia.setdefault(d["fecha"], {})[str(d["horizonte"])] = {
                "emitida": p["emitida"], "dia": d["dia"], "franjas": d["franjas"], "p50_horas": d["horas"]["p50"],
                "p10_horas": d["horas"]["p10"], "p90_horas": d["horas"]["p90"], "semaforo": d["semaforo"]}
    desde = (HOY - dt.timedelta(days=75)).isoformat()
    cuartos = leer_cuartos()
    if prevision:                       # copia para no tocar la previsión congelada
        prevision = {**prevision, "dias": [{**d, "real_cuartos": cuartos.get(d["fecha"]) if d.get("real_horas") else None}
                                           for d in prevision["dias"]]}
    historico = []
    for f in sorted(base.precio):
        if f < desde or f > HOY.isoformat():
            continue
        p = base.precio[f]
        if not p or any(x is None for x in p):
            continue
        historico.append({"fecha": f, "tipo_dia": tipo_dia(dt.date.fromisoformat(f)),
                          "real_horas": [round(x, 2) for x in p], "real_dia": round(media(p), 2),
                          "real_cuartos": cuartos.get(f),
                          "real_franjas": {n: round(media(p[a:b]), 2) for n, a, b in FRANJAS},
                          "coste_ccgt": round(base.coste_ccgt(f) or 0, 2),
                          "previsiones": prev_por_dia.get(f, {})})
    # métricas por horizonte y franja
    met = {}
    for r in verif.values():
        if r.get("publicado"):
            continue
        h, fr = r["horizonte"], r["franja"]
        m = met.setdefault(h, {}).setdefault(fr, {"n": 0, "ae": 0.0, "e": 0.0, "dentro": 0})
        m["n"] += 1; m["ae"] += abs(r["error"]); m["e"] += r["error"]; m["dentro"] += r["dentro"]
    metricas = {h: {fr: {"n": m["n"], "mae": round(m["ae"] / m["n"], 2), "sesgo": round(m["e"] / m["n"], 2),
                         "cobertura": round(m["dentro"] / m["n"], 3)} for fr, m in d.items()} for h, d in met.items()}
    # persistencia como referencia: repetir el último precio conocido al emitir (D+1, ya publicado por OMIE
    # a las 16:30). Para D+h el desfase es h-1 días; en D+1 no hay referencia (el precio ya se conoce).
    pers = {}
    fechas = sorted(f for f in base.precio if f >= (HOY - dt.timedelta(days=90)).isoformat())
    for h in HORIZONTES:
        if h == 1:
            pers[str(h)] = None
            continue
        errs = []
        for f in fechas:
            o = (dt.date.fromisoformat(f) + dt.timedelta(days=h - 1)).isoformat()
            if o in base.precio and base.precio[o] and base.precio[f] and None not in base.precio[o] + base.precio[f]:
                errs.append(abs(media(base.precio[o]) - media(base.precio[f])))
        pers[str(h)] = round(sum(errs) / len(errs), 2) if errs else None

    datos = {"generado": dt.datetime.now().isoformat(timespec="seconds"),
             "franjas": [{"id": n, "nombre": NOMBRES_FRANJA[n], "desde": a, "hasta": b} for n, a, b in FRANJAS],
             "prevision": prevision, "historico": historico, "metricas": metricas,
             "persistencia_mae_90d": pers,
             "calibracion": {str(h): c for h, c in cal.items()},
             "verificadas": len(verif)}
    ruta = os.path.join(DATOS, "previsor_datos.js")
    with open(ruta + ".tmp", "w", encoding="utf-8") as f:
        f.write("window.PREVISOR_DATOS = ")
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    os.replace(ruta + ".tmp", ruta)
    log(f"   {os.path.basename(ruta)} actualizado ({os.path.getsize(ruta) // 1024} kB)")

# ============================================================================
def ejecucion_reciente(minutos=60):
    """True si la previsión de hoy ya existe y otra ejecución terminó hace menos de 'minutos'.
       Evita la doble ejecución cuando coinciden el disparador de las 16:30 y el de inicio de sesión."""
    if not os.path.exists(os.path.join(PREV_DIR, f"prevision_{HOY.isoformat()}.json")):
        return False
    logs = glob.glob(os.path.join(LOGS, f"actualizacion_diaria_{HOY:%Y%m%d}_*.log"))
    if not logs:
        return False
    return time.time() - max(os.path.getmtime(f) for f in logs) < minutos * 60

def main():
    args_previos = [a.lower() for a in sys.argv[1:]]
    if not args_previos and ejecucion_reciente():
        print("La previsión de hoy ya está emitida y hubo una actualización hace menos de una hora: no se repite.")
        return
    iniciar_log("actualizacion_diaria")
    cfg = cargar_config()
    args = [a.lower() for a in sys.argv[1:]]
    t0 = time.time()
    log(f"Actualización diaria · {HOY} · carpeta: {BASE}")

    futuro, analisis = {}, {}
    if "--sin-descarga" not in args:
        for paso in (ch.cargar_omie, lambda: ch.cargar_ree(rapido=True), lambda: ch.cargar_gas(rapido=True), ch.cargar_entsoe):
            try:
                paso()
            except Exception as e:
                log(f"   ! error en la descarga: {e}")
        try:
            futuro, analisis = ch.cargar_meteo(cfg)
        except Exception as e:
            log(f"   ! error en la meteo: {e}")
        ch.consolidar(cfg)
    else:
        log("   (sin descarga: se usan los datos en disco)")

    log("== Modelo ==")
    base = Base(cfg)
    an = Analogos(base)
    log(f"   {len(an.dias)} días históricos utilizables como análogos")

    prev_meteo = indices_previstos(cfg, futuro, analisis) if futuro else {}
    if not prev_meteo:
        # sin descarga: reutiliza la meteo de la última previsión guardada si es de hoy
        log("   ! sin meteo prevista nueva: no se puede emitir previsión hoy")
    cal, verif = calibrar(base, cfg)
    log(f"   previsiones verificadas: {len(verif)} filas ({sum(v['publicado'] for v in verif.values())} con el real ya publicado al emitir, excluidas) · ensanche por horizonte: "
        + ", ".join(f"D+{h} ×{c['ensanche']}" for h, c in cal.items()))
    pv = next(iter(cal.values()), {}).get("sesgo_viento") or {}
    if pv:
        log("   sesgo por tramo de viento (poco / mucho): "
            + ", ".join(f"{n} {_fmt(v['bajo'])}/{_fmt(v['alto'])}" for n, v in pv.items())
            + f" · umbral ≈ {media([v['umbral_mw'] for v in pv.values()]):.0f} MW")
    else:
        log("   sesgo por tramo de viento: sin casos suficientes o desactivado; se usa el sesgo general")
    escribir_tabla("verificacion.csv", CAB_REG + ["real", "error", "dentro", "publicado"], verif)

    prevision = None
    if prev_meteo:
        prevision = emitir(cfg, base, an, prev_meteo, cal, forzar="--forzar" in args)
    else:
        ruta = os.path.join(PREV_DIR, f"prevision_{HOY.isoformat()}.json")
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                prevision = json.load(f)
        else:
            ult = sorted(glob.glob(os.path.join(PREV_DIR, "prevision_*.json")))
            if ult:
                with open(ult[-1], encoding="utf-8") as f:
                    prevision = json.load(f)
                log(f"   se muestra la última previsión disponible ({os.path.basename(ult[-1])})")

    if prevision:
        for d in prevision["dias"]:
            s = d["semaforo"]
            log(f"   {d['fecha']} D+{d['horizonte']}: P50 {d['dia'][1]:6.1f} €/MWh  [{d['dia'][0]:.0f}–{d['dia'][2]:.0f}]  "
                f"{s['nivel']}{'  · riesgo precio cero' if s['riesgo_cero'] else ''}"
                + (f"  · real {media(d['real_horas']):.1f}" if d.get("real_horas") else ""))
    exportar(base, prevision, cal, verif)
    log(f"Terminado en {(time.time() - t0) / 60:.1f} min")

if __name__ == "__main__":
    main()
