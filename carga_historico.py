# -*- coding: utf-8 -*-
"""
carga_historico.py  ·  Fase 1 del previsor de precios OMIE
-------------------------------------------------------------
Descarga y consolida el histórico desde 2021 en la carpeta datos/:

  precios_omie_horario.csv   precio marginal ES, media horaria (OMIE marginalpdbc)
  demanda_horaria.csv        demanda peninsular horaria, MW (REE REData)
  generacion_diaria.csv      generación peninsular diaria por tecnología, MWh (REE)
  hidraulica_mensual.csv     reservas de embalses y producible hidráulico (REE)
  gas_mibgas.csv             precio gas day-ahead PVB, €/MWh (MIBGAS)
  meteo_puntos/*.csv.gz      meteo horaria por punto (Open-Meteo, reanálisis + últimos días)
  meteo_indices_horario.csv  índices peninsulares horarios: eólico, solar, temperatura
  dataset_diario.csv         tabla diaria consolidada para el modelo

Es reanudable: si se corta, al relanzarlo continúa donde lo dejó.
La parte meteorológica es la lenta (~1 h 20 min) por el límite gratuito de Open-Meteo.

Uso:
  python carga_historico.py                 todo
  python carga_historico.py omie ree gas    solo esas partes
  (partes: omie, ree, gas, meteo, consolidar, entsoe)
"""
import sys, time, json
import datetime as dt
from previsor_comun import *

HOY = dt.date.today()
AYER = HOY - dt.timedelta(days=1)

# ============================================================================
# 1. OMIE
# ============================================================================
def cargar_omie():
    log("== OMIE · precios del mercado diario ==")
    precios = leer_ancho("precios_omie_horario.csv")
    log(f"   ya en disco: {len(precios)} días")

    for anio in (2021, 2022):
        faltan = [d for d in rango_fechas(dt.date(anio, 1, 1), dt.date(anio, 12, 31)) if d.isoformat() not in precios]
        if not faltan:
            continue
        log(f"   zip anual {anio}…")
        try:
            precios.update(omie_anual_zip(anio))
            escribir_ancho("precios_omie_horario.csv", precios)
        except Exception as e:
            log(f"   ! zip {anio} no disponible ({e}); se descargará día a día")

    try:
        versiones = omie_listado_diario()
        log(f"   listado diario: {len(versiones)} ficheros")
    except Exception as e:
        log(f"   ! no se pudo leer el listado ({e}); se probará la versión 1 de cada día")
        versiones = {}

    hasta = HOY + dt.timedelta(days=1)          # incluye D+1 si ya está publicado
    pendientes = [d for d in rango_fechas(FECHA_INICIO, hasta) if d.isoformat() not in precios]
    log(f"   días pendientes: {len(pendientes)}")
    for i, d in enumerate(pendientes, 1):
        try:
            r = omie_dia(d, versiones.get(d.strftime("%Y%m%d"), 1))
            precios.update(r)
            if not r and d <= HOY:
                log(f"   ! {d} sin datos")
        except Exception as e:
            log(f"   ! {d}: {e}")
        if i % 50 == 0:
            escribir_ancho("precios_omie_horario.csv", precios)
            log(f"   … {i}/{len(pendientes)}")
        time.sleep(0.25)
    escribir_ancho("precios_omie_horario.csv", precios)
    log(f"   OK · {len(precios)} días ({min(precios)} → {max(precios)})")
    cargar_cuartos(precios, versiones, hasta)

def cargar_cuartos(precios, versiones, hasta, dias_atras=90, max_descargas=100):
    """Conserva los precios de 15 min. Los días ya descargados hoy están en CUARTOS; los recientes
    que falten (los anteriores a este cambio) se vuelven a pedir a OMIE, como mucho max_descargas por ejecución."""
    cuartos = leer_cuartos()
    cuartos.update(CUARTOS)
    desde = max(dt.date(2025, 10, 1), HOY - dt.timedelta(days=dias_atras))
    faltan = [d for d in rango_fechas(desde, hasta) if d.isoformat() in precios and d.isoformat() not in cuartos]
    for d in faltan[-max_descargas:]:
        try:
            omie_dia(d, versiones.get(d.strftime("%Y%m%d"), 1))
            if d.isoformat() in CUARTOS:
                cuartos[d.isoformat()] = CUARTOS[d.isoformat()]
        except Exception as e:
            log(f"   ! cuartos {d}: {e}")
        time.sleep(0.25)
    escribir_cuartos(cuartos)
    log(f"   cuartohorarios: {len(cuartos)} días" + (f" ({len(faltan)} recuperados o pedidos)" if faltan else ""))

# ============================================================================
# 2. REE · demanda horaria, generación diaria, hidráulica mensual
# ============================================================================
def meses(desde, hasta):
    d = dt.date(desde.year, desde.month, 1)
    while d <= hasta:
        sig = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        yield d, min(sig - dt.timedelta(days=1), hasta)
        d = sig

def cargar_ree(rapido=False):
    log("== REE · demanda horaria ==")
    dem = leer_ancho("demanda_horaria.csv")
    for ini, fin in meses(FECHA_INICIO, AYER):
        if all(d.isoformat() in dem for d in rango_fechas(ini, fin)):
            continue
        try:
            s = ree("demanda/evolucion", ini, fin, "hour")
            dem.update(parsear_horario_ree(next(iter(s.values()))))
            log(f"   {ini:%Y-%m} ok")
        except Exception as e:
            log(f"   ! {ini:%Y-%m}: {e}")
        time.sleep(0.5)
    escribir_ancho("demanda_horaria.csv", dem, 1)
    log(f"   OK · {len(dem)} días")

    log("== REE · generación diaria por tecnología ==")
    cab, gen = leer_tabla("generacion_diaria.csv")
    for anio in range(FECHA_INICIO.year, HOY.year + 1):
        ini, fin = dt.date(anio, 1, 1), min(dt.date(anio, 12, 31), AYER)
        if all(d.isoformat() in gen for d in rango_fechas(max(ini, fin - dt.timedelta(days=3)), fin)) and anio < HOY.year:
            continue
        try:
            for fecha, vals in parsear_diario_ree(ree("generacion/estructura-generacion", ini, fin, "day")).items():
                fila = gen.setdefault(fecha, {"fecha": fecha})
                fila.update({k: round(v, 1) for k, v in vals.items()})
            log(f"   {anio} ok")
        except Exception as e:
            log(f"   ! {anio}: {e}")
        time.sleep(0.5)
    columnas = ["fecha"] + sorted({k for f in gen.values() for k in f if k != "fecha"})
    escribir_tabla("generacion_diaria.csv", columnas, gen)
    log(f"   OK · {len(gen)} días · {len(columnas) - 1} tecnologías")

    log("== REE · reservas de embalses y producible hidráulico ==")
    _, hid = leer_tabla("hidraulica_mensual.csv")
    for anio in range(HOY.year - 1 if rapido else FECHA_INICIO.year, HOY.year + 1):
        ini, fin = dt.date(anio, 1, 1), dt.date(anio, 12, 31)
        for widget in ("generacion/reservas-hidroelectricas", "generacion/producible-hidraulico"):
            try:
                for titulo, vals in ree(widget, ini, fin, "month").items():
                    for v in vals:
                        fila = hid.setdefault(v["datetime"][:7], {"mes": v["datetime"][:7]})
                        t = titulo.lower()
                        if t.startswith("total reservas"):
                            fila["reservas_total_gwh"] = v["value"] / 1000
                            fila["llenado_total_pct"] = round(v.get("percentage", 0) * 100, 1)
                        elif t.startswith("reservas embalses anuales"):
                            fila["reservas_anuales_gwh"] = v["value"] / 1000
                            fila["llenado_anuales_pct"] = round(v.get("percentage", 0) * 100, 1)
                        elif t.startswith("reservas embalses hiperanuales"):
                            fila["reservas_hiperanuales_gwh"] = v["value"] / 1000
                            fila["llenado_hiperanuales_pct"] = round(v.get("percentage", 0) * 100, 1)
                        elif t.startswith("producible"):
                            fila["producible_gwh"] = v["value"] / 1000
                        elif t.startswith("índice") or t.startswith("indice"):
                            fila["indice_producible"] = v["value"] / 1000
                        elif t.startswith("probabilidad"):
                            fila["prob_superado_pct"] = v["value"] / 1000
            except Exception as e:
                log(f"   ! {widget.split('/')[-1]} {anio}: {e}")
            time.sleep(0.5)
    escribir_tabla("hidraulica_mensual.csv",
                   ["mes", "reservas_total_gwh", "llenado_total_pct", "reservas_anuales_gwh", "llenado_anuales_pct",
                    "reservas_hiperanuales_gwh", "llenado_hiperanuales_pct", "producible_gwh", "indice_producible",
                    "prob_superado_pct"], hid)
    log(f"   OK · {len(hid)} meses")

# ============================================================================
# ENTSO-E · Francia (precio day-ahead) e intercambio físico España↔Francia
# ============================================================================
def cargar_entsoe(rapido=False):
    """Series de ENTSO-E en hora local (CSV ancho fecha;h00..h23). Desde 2024; después solo lo reciente.
    Francia: precio day-ahead, demanda real, saldo físico España→Francia (MW, + = España exporta).
    Portugal (mismo mercado MIBEL): demanda real, eólica y solar reales, saldo físico España→Portugal."""
    log("== ENTSO-E · Francia y Portugal ==")
    tok = entsoe_token()
    if not tok:
        log("   sin clave (entsoe_token.txt): se omite")
        return
    def desde(serie):
        ok = [f for f, v in serie.items() if v and None not in v]
        return dt.date.fromisoformat(max(ok)) - dt.timedelta(days=7) if ok else dt.date(2024, 1, 1)
    def tramos(d0, d1):
        a = d0
        while a <= d1:
            b = min(d1, a + dt.timedelta(days=300))
            # de las 22:00 UTC del día anterior a las 00:00 UTC del siguiente: cubre el día local entero
            yield (dt.datetime.combine(a - dt.timedelta(days=1), dt.time(22)),
                   dt.datetime.combine(b + dt.timedelta(days=1), dt.time(0)))
            if b >= d1:
                break
            a = b - dt.timedelta(days=1)          # solape de un día entre tramos
    def consulta(a, b, campo, gen=False, **p):
        r = parsear_entsoe(descargar(entsoe_url(tok, a, b, **p), intentos=3), campo, solo_generacion=gen)
        time.sleep(1)
        return r
    flujo = lambda a, b, de, a_: consulta(a, b, "quantity", documentType="A11", out_Domain=de, in_Domain=a_)
    SERIES = [
        # fichero, etiqueta, último día, función (a, b) -> {hora UTC: valor}, decimales
        ("francia_precio_horario.csv", "precio Francia", 1,
         lambda a, b: consulta(a, b, "price.amount", documentType="A44", in_Domain=ZONA_FR, out_Domain=ZONA_FR), 2),
        ("francia_demanda_horaria.csv", "demanda Francia", 0,
         lambda a, b: consulta(a, b, "quantity", documentType="A65", processType="A16", outBiddingZone_Domain=ZONA_FR), 0),
        ("intercambio_es_fr_horario.csv", "intercambio ES→FR", 0,
         lambda a, b: (lambda x, y: {h: x[h] - y.get(h, 0.0) for h in x})(flujo(a, b, ZONA_ES, ZONA_FR), flujo(a, b, ZONA_FR, ZONA_ES)), 0),
        ("espana_demanda_prevista_d1.csv", "demanda prevista D+1 (REE)", 1,
         lambda a, b: consulta(a, b, "quantity", documentType="A65", processType="A01", outBiddingZone_Domain=ZONA_ES), 0),
        ("portugal_demanda_horaria.csv", "demanda Portugal", 0,
         lambda a, b: consulta(a, b, "quantity", documentType="A65", processType="A16", outBiddingZone_Domain=ZONA_PT), 0),
        ("portugal_eolica_horaria.csv", "eólica Portugal", 0,
         lambda a, b: consulta(a, b, "quantity", True, documentType="A75", processType="A16", in_Domain=ZONA_PT, psrType="B19"), 0),
        ("portugal_solar_horaria.csv", "solar Portugal", 0,
         lambda a, b: consulta(a, b, "quantity", True, documentType="A75", processType="A16", in_Domain=ZONA_PT, psrType="B16"), 0),
        ("intercambio_es_pt_horario.csv", "intercambio ES→PT", 0,
         lambda a, b: (lambda x, y: {h: x[h] - y.get(h, 0.0) for h in x})(flujo(a, b, ZONA_ES, ZONA_PT), flujo(a, b, ZONA_PT, ZONA_ES)), 0),
    ]
    for fichero, etiqueta, dias_mas, fn, dec in SERIES:
        try:
            serie = leer_ancho(fichero)
            for a, b in tramos(desde(serie), HOY + dt.timedelta(days=dias_mas)):
                serie.update(utc_a_ancho(fn(a, b)))
            escribir_ancho(fichero, serie, decimales=dec)
            log(f"   {etiqueta}: {len(serie)} días" + (f" (último {max(serie)})" if serie else ""))
        except Exception as e:
            log(f"   ! {etiqueta}: {e}")
    try:
        cargar_nuclear_indisponible(tok)
    except Exception as e:
        log(f"   ! nuclear (indisponibilidades): {e}")

def cargar_nuclear_indisponible(tok):
    """espana_nuclear_indisponible.csv: MW nucleares no disponibles por hora (paradas programadas y averías
    comunicadas a ENTSO-E). Incluye los días futuros ya anunciados. Desde 2024; luego, de hoy-45 a hoy+14."""
    serie = leer_ancho("espana_nuclear_indisponible.csv")
    d0 = dt.date(2024, 1, 1) if not serie else HOY - dt.timedelta(days=45)
    d1 = HOY + dt.timedelta(days=14)
    total, n_msg = {}, 0
    a = d0
    while a <= d1:
        b = min(d1, a + dt.timedelta(days=360))
        ini = dt.datetime.combine(a - dt.timedelta(days=1), dt.time(22))
        fin = dt.datetime.combine(b + dt.timedelta(days=1), dt.time(0))
        msgs = []
        for offset in range(0, 5000, 200):
            url = entsoe_url(tok, ini, fin, documentType="A80", biddingZone_Domain=ZONA_ES, offset=offset)
            try:
                lote = parsear_indisponibilidades(descargar(url, intentos=3, timeout=180))
            except ErrorHTTP as e:
                if "No matching data" in str(e) or e.codigo in (400,) and "offset" in str(e).lower():
                    break
                raise
            time.sleep(1)
            msgs += lote
            if len(lote) < 200:
                break
        n_msg += len(msgs)
        total.update(indisponible_horario(msgs))
        if b >= d1:
            break
        a = b + dt.timedelta(days=1)
    # horas sin mensaje = 0 MW indisponibles, para todo el rango consultado
    h = dt.datetime.combine(d0 - dt.timedelta(days=1), dt.time(22))
    while h < dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time(0)):
        total.setdefault(h, 0.0)
        h += dt.timedelta(hours=1)
    serie.update(utc_a_ancho(total))
    escribir_ancho("espana_nuclear_indisponible.csv", serie, decimales=0)
    prox = [f for f in sorted(serie) if HOY.isoformat() <= f <= d1.isoformat()]
    resumen = ", ".join(f"{f[5:]} {sum(serie[f]) / 24 / 1000:.1f} GW" for f in prox[:6])
    log(f"   nuclear indisponible: {len(serie)} días, {n_msg} mensajes · próximos días: {resumen}")

# ============================================================================
# 3. MIBGAS
# ============================================================================
def cargar_gas(rapido=False):
    log("== MIBGAS · gas day-ahead (GDAES_D+1) ==")
    _, previo = leer_tabla("gas_mibgas.csv")
    serie = {k: float(r["gas_eur_mwh"]) for k, r in previo.items() if r.get("origen") == "sesion"}
    for anio in range(HOY.year - 1 if rapido else FECHA_INICIO.year, HOY.year + 1):
        try:
            s = mibgas_anio(anio)
            serie.update(s)
            log(f"   {anio}: {len(s)} sesiones")
        except Exception as e:
            log(f"   ! {anio}: {e}")
        time.sleep(0.5)
    if not serie:
        log("   ! sin datos de gas")
        return
    completo = rellenar_hacia_delante(serie, FECHA_INICIO, max(dt.date.fromisoformat(max(serie)), HOY))
    filas = {k: {"fecha": k, "gas_eur_mwh": v, "origen": "sesion" if k in serie else "relleno"} for k, v in completo.items()}
    escribir_tabla("gas_mibgas.csv", ["fecha", "gas_eur_mwh", "origen"], filas)
    log(f"   OK · {len(filas)} días · último {max(serie)} = {serie[max(serie)]} €/MWh")

# ============================================================================
# 4. Open-Meteo
# ============================================================================
FUTURO_MODELOS = {}      # {modelo: {punto: {fecha: {var: [24]}}}} previsión de varios modelos (hoy en adelante)

def cargar_meteo(cfg):
    log("== Open-Meteo · meteo horaria por punto ==")
    FUTURO_MODELOS.clear()
    grupos_mm = {n for g in ("puntos_eolicos", "puntos_solares", "puntos_eolicos_pt", "puntos_solares_pt")
                 for n, *_ in cfg.get(g, [])}
    n_mm = 0
    puntos = todos_los_puntos(cfg)
    fin_archivo = HOY - dt.timedelta(days=6)        # el reanálisis va ~5 días por detrás
    pausa = cfg["openmeteo_pausa_s"]
    pendientes = []
    for nombre, lat, lon in puntos:
        d = leer_punto(nombre)
        pendientes.append((nombre, lat, lon, d))
    n_desc = sum(1 for p in pendientes if not p[3] or max(p[3]) < fin_archivo.isoformat())
    log(f"   {len(puntos)} puntos · {n_desc} con histórico pendiente · pausa {pausa} s entre descargas "
        f"(≈ {n_desc * pausa // 60} min)")

    primera = True
    futuro, analisis = {}, {}      # previsión (hoy en adelante) y últimos días según el modelo de previsión
    for nombre, lat, lon, datos in pendientes:
        # --- reanálisis: desde el último día guardado (o 2021) hasta hoy-6
        ult = max(datos) if datos else None
        ini = FECHA_INICIO if not ult else dt.date.fromisoformat(ult) + dt.timedelta(days=1)
        # los días que vinieron de la API de previsión se vuelven a pedir al reanálisis
        if datos:
            prev = sorted(k for k in datos if k > (fin_archivo - dt.timedelta(days=12)).isoformat())
            if prev:
                ini = min(ini, dt.date.fromisoformat(prev[0]))
        if ini <= fin_archivo:
            dias = (fin_archivo - ini).days + 1
            if not primera and dias > 60:            # solo las descargas largas consumen cupo
                time.sleep(pausa * min(1.0, dias / 2000))
            primera = False
            url = om_url(OM_ARCHIVO, lat, lon, f"start_date={ini}&end_date={fin_archivo}")
            try:
                datos.update(parsear_om(json.loads(descargar(url, timeout=180))))
                log(f"   {nombre}: reanálisis {ini} → {fin_archivo}")
            except Exception as e:
                log(f"   ! {nombre}: {e}")
                continue
        # --- últimos días y próximos 7: API de previsión
        try:
            url = om_url(OM_PREVISION, lat, lon, "past_days=10&forecast_days=7")
            reciente = parsear_om(json.loads(descargar(url)))
            for k, v in reciente.items():
                if k > fin_archivo.isoformat() and k <= AYER.isoformat():
                    datos[k] = v
                elif k >= HOY.isoformat():
                    futuro.setdefault(nombre, {})[k] = v
                if k >= (HOY - dt.timedelta(days=2)).isoformat():
                    analisis.setdefault(nombre, {})[k] = v
        except Exception as e:
            log(f"   ! {nombre} (días recientes / previsión): {e}")
        # --- previsión de varios modelos (solo puntos eólicos y solares)
        if nombre in grupos_mm:
            try:
                url = om_url(OM_PREVISION, lat, lon, "forecast_days=7&models=" + ",".join(MODELOS_METEO))
                for m, fs in parsear_om_modelos(json.loads(descargar(url)), MODELOS_METEO).items():
                    FUTURO_MODELOS.setdefault(m, {})[nombre] = {k: v for k, v in fs.items() if k >= HOY.isoformat()}
                n_mm += 1
            except Exception as e:
                log(f"   ! {nombre} (varios modelos): {e}")
        escribir_punto(nombre, datos)
    log(f"   OK · previsión de varios modelos en {n_mm} puntos ({len(FUTURO_MODELOS)} modelos)")
    return futuro, analisis

# ============================================================================
# 5. Consolidación
# ============================================================================
FRANJAS = [("madrugada", 0, 7), ("subida", 7, 10), ("valle_solar", 10, 15),
           ("sostenido", 15, 19), ("pico", 19, 23), ("cierre", 23, 24)]

def media(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def consolidar(cfg):
    log("== Consolidación ==")
    puntos = {n: leer_punto(n) for n, _, _ in todos_los_puntos(cfg)}
    fechas = sorted({f for d in puntos.values() for f in d})
    eol, sol, tmp, lluvia = {}, {}, {}, {}
    for f in fechas:
        r = indices_dia(cfg, puntos, f)
        if all(x is not None for x in r["eol"]):
            eol[f], sol[f], tmp[f], lluvia[f] = r["eol"], r["sol"], r["temp"], r["lluvia"]
    escribir_ancho("meteo_indice_eolico.csv", eol, 4)
    escribir_ancho("meteo_indice_solar.csv", sol, 4)
    escribir_ancho("meteo_temperatura.csv", tmp, 2)
    # Portugal: índices propios (aún no los usa el modelo de precios)
    if cfg.get("puntos_eolicos_pt"):
        ept, spt, tpt = {}, {}, {}
        for f in fechas:
            r = indices_dia(cfg, puntos, f)
            if all(x is not None for x in r["eol_pt"]):
                ept[f], spt[f], tpt[f] = r["eol_pt"], r["sol_pt"], r["temp_pt"]
        escribir_ancho("meteo_indice_eolico_pt.csv", ept, 4)
        escribir_ancho("meteo_indice_solar_pt.csv", spt, 4)
        escribir_ancho("meteo_temperatura_pt.csv", tpt, 2)
        log(f"   índices meteo Portugal: {len(ept)} días")
    log(f"   índices meteo: {len(eol)} días")

    precios = leer_ancho("precios_omie_horario.csv")
    dem = leer_ancho("demanda_horaria.csv")
    _, gen = leer_tabla("generacion_diaria.csv")
    _, gas = leer_tabla("gas_mibgas.csv")
    _, hid = leer_tabla("hidraulica_mensual.csv")
    co2 = cfg["co2_anual_eur_t"]
    exc_ini, exc_fin = cfg["excluir_desde"], cfg["excluir_hasta"]

    # último dato hidráulico conocido para meses aún sin publicar
    meses_hid = sorted(hid)
    def hid_mes(mes):
        prev = [m for m in meses_hid if m <= mes]
        return hid[prev[-1]] if prev else {}

    cab = ["fecha", "tipo_dia", "excluido", "precio_medio"] + [f"p_{n}" for n, _, _ in FRANJAS] + \
          ["precio_min", "precio_max", "horas_precio_cero", "gas_eur_mwh", "co2_eur_t", "coste_ccgt",
           "demanda_media_mw", "eol_idx", "sol_idx", "temp_media", "lluvia_cuencas_mm",
           "eolica_real_mwh", "solar_fv_real_mwh", "hidraulica_real_mwh", "nuclear_real_mwh",
           "ciclo_combinado_real_mwh", "llenado_embalses_pct", "indice_producible"]
    filas = {}
    todas = sorted(set(precios) | set(dem) | set(eol))
    for f in todas:
        if f < FECHA_INICIO.isoformat():
            continue
        d = dt.date.fromisoformat(f)
        p = precios.get(f)
        g = gen.get(f, {})
        gs = float(gas[f]["gas_eur_mwh"]) if f in gas else None
        c = co2.get(str(d.year), list(co2.values())[-1])
        h = hid_mes(f[:7])
        fila = {"fecha": f, "tipo_dia": tipo_dia(d), "excluido": int(exc_ini <= f <= exc_fin),
                "gas_eur_mwh": gs, "co2_eur_t": c,
                "coste_ccgt": round(gs / cfg["ccgt_rendimiento"] + c * cfg["ccgt_factor_emision_t_mwh"], 2) if gs else None,
                "demanda_media_mw": round(media(dem[f]), 1) if f in dem else None,
                "eol_idx": round(media(eol[f]), 4) if f in eol else None,
                "sol_idx": round(media(sol[f]), 4) if f in sol else None,
                "temp_media": round(media(tmp[f]), 2) if f in tmp else None,
                "lluvia_cuencas_mm": round(lluvia[f], 2) if lluvia.get(f) is not None else None,
                "eolica_real_mwh": g.get("Eólica"), "solar_fv_real_mwh": g.get("Solar fotovoltaica"),
                "hidraulica_real_mwh": g.get("Hidráulica"), "nuclear_real_mwh": g.get("Nuclear"),
                "ciclo_combinado_real_mwh": g.get("Ciclo combinado"),
                "llenado_embalses_pct": h.get("llenado_total_pct"), "indice_producible": h.get("indice_producible")}
        if p and all(x is not None for x in p):
            fila["precio_medio"] = round(media(p), 2)
            for n, a, b in FRANJAS:
                fila[f"p_{n}"] = round(media(p[a:b]), 2)
            fila["precio_min"], fila["precio_max"] = round(min(p), 2), round(max(p), 2)
            fila["horas_precio_cero"] = sum(1 for x in p if x <= 0.5)
        filas[f] = fila
    escribir_tabla("dataset_diario.csv", cab, filas)
    con_todo = sum(1 for r in filas.values() if r.get("precio_medio") is not None and r.get("eol_idx") is not None
                   and r.get("gas_eur_mwh") is not None and r.get("demanda_media_mw") is not None)
    log(f"   dataset_diario.csv: {len(filas)} días · {con_todo} completos (precio + meteo + gas + demanda)")

    # resumen para comprobar
    resumen = {
        "generado": dt.datetime.now().isoformat(timespec="seconds"),
        "precios": [min(precios), max(precios), len(precios)] if precios else None,
        "demanda": [min(dem), max(dem), len(dem)] if dem else None,
        "generacion": [min(gen), max(gen), len(gen)] if gen else None,
        "gas": [min(gas), max(gas), len(gas)] if gas else None,
        "hidraulica": [min(hid), max(hid), len(hid)] if hid else None,
        "meteo": [min(eol), max(eol), len(eol)] if eol else None,
        "dias_completos": con_todo,
    }
    with open(os.path.join(DATOS, "resumen_carga.json"), "w", encoding="utf-8") as fh:
        json.dump(resumen, fh, ensure_ascii=False, indent=2)
    for k, v in resumen.items():
        log(f"   {k:<12} {v}")

# ============================================================================
if __name__ == "__main__":
    iniciar_log("carga_historico")
    cfg = cargar_config()
    partes = [a.lower() for a in sys.argv[1:]] or ["omie", "ree", "gas", "meteo", "consolidar"]
    t0 = time.time()
    log(f"Carga histórica · partes: {', '.join(partes)} · carpeta: {BASE}")
    if "omie" in partes: cargar_omie()
    if "ree" in partes: cargar_ree()
    if "gas" in partes: cargar_gas()
    if "entsoe" in partes: cargar_entsoe()
    if "meteo" in partes: cargar_meteo(cfg)
    if "consolidar" in partes: consolidar(cfg)
    log(f"Terminado en {(time.time() - t0) / 60:.1f} min")
    if sys.stdin and sys.stdin.isatty():
        input("\nPulsa Intro para cerrar…")
