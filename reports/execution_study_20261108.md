# Costo de ejecución on-chain — 2026-11-08

21 tokens Alpha, nocional 1600 USD, 4 tramos, funding ≥ 0.05 %/día, hold 7 d. Valores en % del nocional: ejecución ida y vuelta más el funding ganado o perdido por esperar (vs entrar y salir sin esperar).

| Método | n | Media | Mediana | p5 | Veces > 0 |
|---|---|---|---|---|---|
| swap paciente troceado 1440m basis≥0.4 | 12927 | +0.47 | +0.32 | -0.50 | 75 % |
| swap paciente troceado 360m basis≥0.4 | 12484 | +0.44 | +0.30 | -0.54 | 73 % |
| swap paciente troceado 120m basis≥0.4 | 11621 | +0.37 | +0.26 | -0.65 | 70 % |
| límite con gate 360m basis≥0.4 | 12457 | +0.11 | +0.22 | -1.03 | 70 % |
| swap paciente troceado 1440m basis≥0.2 | 13117 | +0.37 | +0.20 | -0.57 | 67 % |
| swap paciente troceado 360m basis≥0.2 | 12945 | +0.33 | +0.18 | -0.62 | 65 % |
| swap paciente troceado 30m basis≥0.4 | 9472 | +0.26 | +0.16 | -0.83 | 61 % |
| swap paciente troceado 120m basis≥0.2 | 12559 | +0.26 | +0.13 | -0.73 | 61 % |
| swap paciente troceado 30m basis≥0.2 | 11584 | +0.13 | +0.02 | -0.91 | 52 % |
| límite en espera 360m | 13035 | -0.18 | -0.01 | -1.48 | 49 % |
| swap paciente 1440m basis≥0.4 | 12927 | -0.12 | -0.19 | -1.67 | 39 % |
| dos límites mismo precio, seguro 30m | 13012 | -0.73 | -0.20 | -2.57 | 0 % |
| swap paciente 360m basis≥0.4 | 12484 | -0.15 | -0.20 | -1.71 | 38 % |
| swap paciente 120m basis≥0.4 | 11621 | -0.21 | -0.25 | -1.77 | 36 % |
| swap inmediato troceado | 13360 | -0.21 | -0.25 | -1.64 | 33 % |
| swap paciente 1440m basis≥0.2 | 13117 | -0.22 | -0.31 | -1.76 | 33 % |
| swap paciente 360m basis≥0.2 | 12945 | -0.26 | -0.33 | -1.81 | 32 % |
| swap paciente 30m basis≥0.4 | 9472 | -0.31 | -0.34 | -1.88 | 32 % |
| swap paciente 120m basis≥0.2 | 12559 | -0.33 | -0.38 | -1.88 | 29 % |
| swap paciente 30m basis≥0.2 | 11584 | -0.45 | -0.48 | -2.00 | 25 % |
| dos límites mismo precio, seguro 5m | 13012 | -0.74 | -0.54 | -2.03 | 0 % |
| dos límites mismo precio, seguro 1m | 13012 | -0.75 | -0.59 | -1.89 | 0 % |
| swap inmediato | 13360 | -0.82 | -0.79 | -2.61 | 16 % |

## Walk-forward (elige por mediana de trimestres anteriores)

- Trimestre 224: swap paciente troceado 1440m basis≥0.4
- Trimestre 225: swap paciente troceado 1440m basis≥0.4
- Trimestre 226: swap paciente troceado 1440m basis≥0.4
- Trimestre 227: swap paciente troceado 1440m basis≥0.4

Fuera de muestra: media +0.48, mediana +0.32, p5 -0.48, veces > 0 76 % (n=12117).

## Veredicto

Política vigente: **swap paciente troceado 1440m basis≥0.4**. Mejora mediana y p5 frente al swap inmediato troceado en 5/5 trimestres → espera por defecto: **SÍ**.
