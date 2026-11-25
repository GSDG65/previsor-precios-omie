# Previsor de precios OMIE

Previsión del precio del mercado diario (OMIE, España) para los próximos 5 días, por día y por franjas,
con horquilla P10–P90. Se ejecuta cada día en GitHub Actions y publica el visor en GitHub Pages.

- **Modelo**: días análogos sobre la demanda residual térmica (demanda − eólica − FV − nuclear),
  con el precio expresado como proporción del coste del ciclo combinado (gas + CO2).
- **Datos**: OMIE (precios, también cuartohorarios), REE (demanda, generación, embalses), MIBGAS (gas),
  Open-Meteo (varios modelos meteorológicos y reanálisis), ENTSO-E (Francia, Portugal, paradas nucleares).
- **Calibración**: el sesgo y la anchura de la horquilla se recalibran solos con las previsiones congeladas.
  El sesgo de cada franja se calcula aparte para días de poco y mucho viento previsto (corte en la mediana);
  se desactiva con `"sesgo_por_viento": false` en `config_previsor.json`.

Ficheros principales: `actualizacion_diaria.py` (ejecución diaria), `previsor_modelo.py` (modelo),
`previsor_comun.py` (descargas y utilidades), `carga_historico.py` (histórico), `evaluar_meteo.py`
(comparación de modelos meteorológicos), `previsor_precios_omie.html` (visor).

Secretos del repositorio: `ENTSOE_TOKEN` (clave de la API de ENTSO-E) y, opcional, `NTFY_TOPIC` (avisos al móvil).
