# -*- coding: utf-8 -*-
"""
evaluar_meteo.py  ·  ¿Qué modelo meteorológico acierta mejor el viento y el sol a 1–5 días?
-----------------------------------------------------------------------------------------
Descarga de Open-Meteo (Previous Runs API) lo que cada modelo previó 1, 2, 3, 4 y 5 días antes
para los puntos eólicos y solares de config_previsor.json, construye con ellos los mismos
índices peninsulares que usa el previsor y los compara con:
  · el reanálisis (meteo_indice_eolico.csv / meteo_indice_solar.csv), la "verdad" meteorológica
  · la generación real de REE (generacion_diaria.csv), la verdad eléctrica
También evalúa dos combinaciones: la media de todos los modelos y una media ponderada por el
acierto de cada modelo en los 30 días anteriores (sin mirar el futuro).

No modifica ningún fichero del previsor. Escribe solo:
  datos/meteo_modelos/<punto>.json.gz      caché de descargas (permite reanudar)
  datos/meteo_modelos_indices.csv          índices horarios por fecha, modelo y horizonte
  datos/evaluacion_meteo_modelos.csv       tabla de acierto
  logs/evaluar_meteo_*.log

Uso:  python evaluar_meteo.py            (último año)
      python evaluar_meteo.py 2025-06-01 (desde esa fecha)
"""
import sys, json, gzip, time, math
import datetime as dt
from previsor_comun import *

PR = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODELOS = ["best_match", "ecmwf_ifs025", "ecmwf_aifs025_single", "icon_seamless",
           "meteofrance_seamless", "gfs_seamless", "ukmo_seamless"]
LEADS = [1, 2, 3, 4, 5]
DIR_CACHE = os.path.join(DATOS, "meteo_modelos")
os.makedirs(DIR_CACHE, exist_ok=True)
PAUSA = 8          # segundos entre peticiones (límite gratuito de Open-Meteo)

def nombre_cache(punto, grupo):
    limpio = re.sub(r"[^A-Za-z0-9]+", "_", punto).strip("_").lower()
    return os.path.join(DIR_CACHE, f"{grupo}_{limpio}.json.gz")

def tramos(d0, d1, dias=180):
    a = d0
    while a <= d1:
        b = min(d1, a + dt.timedelta(days=dias - 1))
        yield a, b
        a = b + dt.timedelta(days=1)

def pedir(lat, lon, vars_base, d0, d1):
    hourly = ",".join(f"{v}_previous_day{n}" for v in vars_base for n in LEADS)
    url = (f"{PR}?latitude={lat}&longitude={lon}&hourly={hourly}&models={','.join(MODELOS)}"
           f"&start_date={d0.isoformat()}&end_date={d1.isoformat()}&wind_speed_unit=ms&timezone=Europe%2FMadrid")
    j = json.loads(descargar(url, intentos=4, timeout=240))
    if isinstance(j, dict) and j.get("error"):
        raise Exception(j.get("reason"))
    return j

def parsear(j, vars_base):
    """→ {modelo: {lead: {fecha: {var: [24]}}}}"""
    hr = j["hourly"]
    t = hr["time"]
    out = {}
    for k, serie in hr.items():
        if k == "time":
            continue
        m = next((m for m in sorted(MODELOS, key=len, reverse=True) if k.endswith("_" + m)), None)
        if m is None:
            continue
        base = k[: -len(m) - 1]
        if "_previous_day" not in base:
            continue
        var, n = base.rsplit("_previous_day", 1)
        if var not in vars_base:
            continue
        acum = {}
        for i, ti in enumerate(t):
            v = serie[i]
            if v is None:
                continue
            acum.setdefault(ti[:10], {}).setdefault(int(ti[11:13]), []).append(v)
        dest = out.setdefault(m, {}).setdefault(int(n), {})
        for fecha, hs in acum.items():
            fila = [sum(hs[h]) / len(hs[h]) if h in hs else None for h in range(24)]
            dest.setdefault(fecha, {})[var] = fila
    return out

def descargar_grupo(cfg, grupo, vars_base, d0, d1):
    """Descarga (o lee de la caché) todos los puntos del grupo. {punto: {modelo: {lead: {fecha: {var:[24]}}}}}"""
    res = {}
    puntos = cfg[grupo]
    for i, (nombre, lat, lon, _) in enumerate(puntos, 1):
        ruta = nombre_cache(nombre, grupo)
        datos = {}
        if os.path.exists(ruta):
            with gzip.open(ruta, "rt", encoding="utf-8") as f:
                c = json.load(f)
            if c.get("desde") <= d0.isoformat() and c.get("hasta") >= d1.isoformat() and c.get("modelos") == MODELOS:
                res[nombre] = {m: {int(n): v for n, v in ls.items()} for m, ls in c["datos"].items()}
                log(f"   [{i}/{len(puntos)}] {nombre}: en caché")
                continue
        try:
            for a, b in tramos(d0, d1):
                p = parsear(pedir(lat, lon, vars_base, a, b), vars_base)
                for m, ls in p.items():
                    for n, fs in ls.items():
                        datos.setdefault(m, {}).setdefault(n, {}).update(fs)
                time.sleep(PAUSA)
        except Exception as e:
            log(f"   ! {nombre}: {e}")
            continue
        modelos_ok = sorted(datos)
        log(f"   [{i}/{len(puntos)}] {nombre}: {len(modelos_ok)} modelos ({', '.join(modelos_ok)})")
        with gzip.open(ruta + ".tmp", "wt", encoding="utf-8") as f:
            json.dump({"desde": d0.isoformat(), "hasta": d1.isoformat(), "modelos": MODELOS,
                       "datos": {m: {str(n): v for n, v in ls.items()} for m, ls in datos.items()}}, f)
        os.replace(ruta + ".tmp", ruta)
        res[nombre] = datos
    return res

VACIO = [None] * 24
VARS = ("eol", "sol", "eol_pt", "sol_pt")

def indices(cfg, eol_pts, sol_pts, fechas):
    """{(modelo, lead): {fecha: {"eol":[24], "sol":[24]}}} con la misma construcción que el previsor."""
    out = {}
    for m in MODELOS:
        for n in LEADS:
            pts = {}
            for nombre, dm in list(eol_pts.items()) + list(sol_pts.items()):
                for fecha, vs in dm.get(m, {}).get(n, {}).items():
                    d = pts.setdefault(nombre, {}).setdefault(fecha, {})
                    d.update(vs)
            if not pts:
                continue
            for d in pts.values():
                for vs in d.values():
                    for var in ("wind_speed_100m", "shortwave_radiation", "temperature_2m", "precipitation"):
                        vs.setdefault(var, VACIO)
            r = {}
            for f in fechas:
                ix = indices_dia(cfg, pts, f)
                if any(x is not None for x in ix["eol"]) or any(x is not None for x in ix["sol"]):
                    r[f] = {k: ix[k] for k in VARS if k in ix}
            if r:
                out[(m, n)] = r
    return out

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def completo(v):
    return v is not None and all(x is not None for x in v)

def main():
    iniciar_log("evaluar_meteo")
    cfg = cargar_config()
    hoy = dt.date.today()
    d1 = hoy - dt.timedelta(days=7)                      # el reanálisis llega con ~6 días de retraso
    d0 = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else d1 - dt.timedelta(days=364)
    t0 = time.time()
    log(f"Evaluación de modelos meteorológicos · {d0} → {d1} · modelos: {', '.join(MODELOS)}")

    log("== Viento a 100 m (puntos eólicos) ==")
    eol_pts = descargar_grupo(cfg, "puntos_eolicos", ["wind_speed_100m"], d0, d1)
    log("== Radiación y temperatura (puntos solares) ==")
    sol_pts = descargar_grupo(cfg, "puntos_solares", ["shortwave_radiation", "temperature_2m"], d0, d1)
    if cfg.get("puntos_eolicos_pt"):
        log("== Portugal: viento y sol ==")
        eol_pts.update(descargar_grupo(cfg, "puntos_eolicos_pt", ["wind_speed_100m"], d0, d1))
        sol_pts.update(descargar_grupo(cfg, "puntos_solares_pt", ["shortwave_radiation", "temperature_2m"], d0, d1))

    fechas = [d.isoformat() for d in rango_fechas(d0, d1)]
    idx = indices(cfg, eol_pts, sol_pts, fechas)
    log(f"   índices calculados: {len(idx)} combinaciones modelo × horizonte")

    # guardar índices horarios
    filas = {}
    for (m, n), r in idx.items():
        for f, v in r.items():
            for var in VARS:
                if var in v and any(x is not None for x in v[var]):
                    filas[f"{f}|{m}|{n}|{var}"] = {"clave": f"{f}|{m}|{n}|{var}", "fecha": f, "modelo": m, "horizonte": n,
                                                  "variable": var, **{HORAS[h]: "" if v[var][h] is None else round(v[var][h], 4) for h in range(24)}}
    escribir_tabla("meteo_modelos_indices.csv", ["clave", "fecha", "modelo", "horizonte", "variable"] + HORAS, filas)

    # referencias
    ref = {"eol": leer_ancho("meteo_indice_eolico.csv"), "sol": leer_ancho("meteo_indice_solar.csv"),
           "eol_pt": leer_ancho("meteo_indice_eolico_pt.csv"), "sol_pt": leer_ancho("meteo_indice_solar_pt.csv")}
    _, gen = leer_tabla("generacion_diaria.csv")
    gen_real = {"eol": {f: float(r["Eólica"]) for f, r in gen.items() if r.get("Eólica")},
                "sol": {f: float(r["Solar fotovoltaica"]) for f, r in gen.items() if r.get("Solar fotovoltaica")}}
    for var, fich in (("eol_pt", "portugal_eolica_horaria.csv"), ("sol_pt", "portugal_solar_horaria.csv")):
        gen_real[var] = {f: sum(v) for f, v in leer_ancho(fich).items() if v and None not in v}

    # combinaciones: media simple y ponderada por acierto de los 30 días previos (por horizonte y variable)
    for n in LEADS:
        comps = [m for m in MODELOS if m != "best_match" and (m, n) in idx]
        if len(comps) < 2:
            continue
        media_r, pond_r = {}, {}
        for f in fechas:
            d = {}
            for var in VARS:
                vals = {m: idx[(m, n)][f][var] for m in comps if f in idx[(m, n)] and completo(idx[(m, n)][f].get(var))}
                if len(vals) < 2:
                    continue
                d.setdefault("media", {})[var] = [mean([v[h] for v in vals.values()]) for h in range(24)]
                # pesos: 1 / MAE diario de cada modelo en los 30 días anteriores con referencia disponible
                # (el reanálisis de un día solo se conoce ~6 días después: se usa la ventana [f-36, f-6])
                fd = dt.date.fromisoformat(f)
                pesos = {}
                for m in vals:
                    errs = []
                    for k in range(6, 37):
                        g = (fd - dt.timedelta(days=k)).isoformat()
                        p = idx[(m, n)].get(g, {}).get(var)
                        r_ = ref[var].get(g)
                        if completo(p) and completo(r_):
                            errs.append(abs(mean(p) - mean(r_)))
                    if len(errs) >= 10:
                        pesos[m] = 1 / max(mean(errs), 1e-4) ** 2
                if len(pesos) >= 2:
                    tot = sum(pesos.values())
                    d.setdefault("pond", {})[var] = [sum(pesos[m] * vals[m][h] for m in pesos) / tot for h in range(24)]
            if "media" in d:
                media_r[f] = d["media"]
            if "pond" in d:
                pond_r[f] = d["pond"]
        idx[("media_modelos", n)] = media_r
        idx[("ponderada_30d", n)] = pond_r

    # tabla de acierto
    filas = {}
    resumen = {}
    for (m, n), r in idx.items():
        for var in VARS:
            pares = [(mean(v[var]), mean(ref[var][f]), v[var], ref[var][f], gen_real[var].get(f))
                     for f, v in r.items() if completo(v.get(var)) and completo(ref[var].get(f))]
            if len(pares) < 20:
                continue
            p = [a for a, *_ in pares]; o = [b for _, b, *_ in pares]
            mae = mean([abs(a - b) for a, b in zip(p, o)])
            sesgo = mean(p) / mean(o) - 1 if mean(o) else None
            mae_h = mean([mean([abs(x - y) for x, y in zip(hp, ho)]) for _, _, hp, ho, _ in pares])
            fuerte = [(a, b) for a, b in zip(p, o) if b > (0.25 if var.startswith("eol") else 0.15)]
            mae_f = mean([abs(a - b) for a, b in fuerte]) if len(fuerte) >= 5 else None
            # frente a REE: correlación del índice diario con la generación real
            g = [(a, x) for a, _, _, _, x in pares if x is not None]
            corr = None
            if len(g) > 20:
                ma, mx = mean([a for a, _ in g]), mean([x for _, x in g])
                sa = math.sqrt(sum((a - ma) ** 2 for a, _ in g)); sx = math.sqrt(sum((x - mx) ** 2 for _, x in g))
                corr = sum((a - ma) * (x - mx) for a, x in g) / (sa * sx) if sa and sx else None
            k = f"{var}|{m}|{n}"
            filas[k] = {"clave": k, "variable": var, "modelo": m, "horizonte": n, "dias": len(pares),
                        "mae_rel_pct": round(100 * mae / mean(o), 1), "sesgo_pct": round(100 * sesgo, 1) if sesgo is not None else "",
                        "mae_horario_rel_pct": round(100 * mae_h / mean(o), 1),
                        "mae_rel_pct_dias_fuertes": round(100 * mae_f / mean(o), 1) if mae_f is not None else "",
                        "corr_generacion_ree": round(corr, 3) if corr is not None else ""}
            resumen.setdefault((var, n), []).append((filas[k]["mae_rel_pct"], m, filas[k]))
    escribir_tabla("evaluacion_meteo_modelos.csv",
                   ["clave", "variable", "modelo", "horizonte", "dias", "mae_rel_pct", "sesgo_pct",
                    "mae_horario_rel_pct", "mae_rel_pct_dias_fuertes", "corr_generacion_ree"], filas)
    for (var, n), lst in sorted(resumen.items()):
        lst.sort(key=lambda t: t[0])
        log(f"   {var.upper()} D+{n}: " + " · ".join(f"{m} {e:.1f}%" for e, m, _ in lst))
    log(f"Terminado en {(time.time() - t0) / 60:.1f} min · resultados en datos/evaluacion_meteo_modelos.csv")

if __name__ == "__main__":
    main()
