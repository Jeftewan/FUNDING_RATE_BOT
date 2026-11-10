# Estudio de tramos on-chain — 2026-11-09

Nocional objetivo 2100 USD, funding ≥ 0.05 %/día, hold 7 d, ventana 6 h. % del nocional objetivo. Ejecución y funding: media de los intentos que dispararon. Fuga = costo de ejecución ÷ funding cobrado.

## 1. Cascada sobre el estudio del 2026-11-08

Mismas reglas del estudio (basis a mid ≥ 0.4, 1600 USD, 4 tramos); valor frente a no esperar, sin contar filas sin disparo.

| Paso | Espera 6 h: mediana | p5 | n | Inmediato: mediana | p5 |
|---|---|---|---|---|---|
| original | +0.30 | -0.54 | 12484 | -0.25 | -1.64 |
| + comisión del pool | +0.17 | -0.83 | 12484 | -0.40 | -1.96 |
| + sin velas planas | -0.29 | -1.09 | 4616 | -0.43 | -1.30 |

## 2. Políticas (solo velas con trades, ρ calibrado 0.8)

| Política | n | Dispara | Llenado | Media | Mediana | Mediana disparados | p5 | > 0 | Ejecución | Funding | Fuga |
|---|---|---|---|---|---|---|---|---|---|---|---|
| tramos cada 5 min | 13886 | 76 % | 100 % | +0.33 | +0.12 | +0.35 | -0.59 | 55 % | -0.45 | +0.89 | 51 % |
| reval entrada θ, pausa 30m | 13907 | 76 % | 77 % | +0.29 | +0.10 | +0.24 | -0.33 | 58 % | -0.31 | +0.69 | 44 % |
| reval entrada θ, pausa ventana | 13897 | 76 % | 93 % | +0.31 | +0.09 | +0.31 | -0.47 | 55 % | -0.42 | +0.83 | 51 % |
| reval entrada θ, parar | 13911 | 76 % | 38 % | +0.21 | +0.09 | +0.16 | -0.09 | 63 % | -0.06 | +0.35 | 18 % |
| reval entrada θ−0.15, parar | 13908 | 76 % | 45 % | +0.24 | +0.08 | +0.16 | -0.11 | 62 % | -0.10 | +0.41 | 24 % |
| reval entrada ≥0, parar | 13907 | 76 % | 51 % | +0.25 | +0.08 | +0.17 | -0.13 | 61 % | -0.13 | +0.46 | 29 % |
| reval salida θ, pausa 30m | 13886 | 76 % | 100 % | +0.31 | +0.08 | +0.33 | -0.63 | 54 % | -0.49 | +0.90 | 55 % |
| reval entrada θ−0.15, pausa 30m | 13905 | 76 % | 87 % | +0.29 | +0.08 | +0.27 | -0.42 | 55 % | -0.40 | +0.78 | 51 % |
| reval salida θ, pausa ventana | 13886 | 76 % | 100 % | +0.31 | +0.08 | +0.32 | -0.61 | 54 % | -0.49 | +0.90 | 55 % |
| reval entrada θ−0.15, pausa ventana | 13894 | 76 % | 96 % | +0.30 | +0.06 | +0.30 | -0.53 | 54 % | -0.47 | +0.86 | 55 % |
| reval entrada ≥0, pausa 30m | 13903 | 76 % | 91 % | +0.28 | +0.06 | +0.27 | -0.47 | 54 % | -0.44 | +0.81 | 54 % |
| reval salida θ−0.15, pausa 30m | 13886 | 76 % | 100 % | +0.30 | +0.05 | +0.30 | -0.64 | 53 % | -0.51 | +0.90 | 56 % |
| reval salida θ−0.15, pausa ventana | 13886 | 76 % | 100 % | +0.30 | +0.05 | +0.30 | -0.62 | 52 % | -0.50 | +0.89 | 56 % |
| reval entrada ≥0, pausa ventana | 13892 | 76 % | 97 % | +0.28 | +0.05 | +0.28 | -0.56 | 53 % | -0.49 | +0.87 | 57 % |
| ejecutar ahora | 13955 | 88 % | 100 % | -0.77 | +0.00 | +0.02 | -1.74 | 45 % | -1.78 | +0.91 | 195 % |
| prod actual | 13886 | 76 % | 100 % | +0.18 | +0.00 | +0.18 | -0.91 | 46 % | -0.65 | +0.89 | 73 % |
| confirmación 2 | 13856 | 50 % | 100 % | +0.17 | +0.00 | +0.27 | -0.58 | 33 % | -0.61 | +0.95 | 64 % |
| confirmación 3 | 13916 | 33 % | 100 % | +0.16 | +0.00 | +0.38 | -0.30 | 24 % | -0.53 | +1.01 | 52 % |
| confirmación med3 | 13850 | 55 % | 100 % | +0.17 | +0.00 | +0.24 | -0.66 | 35 % | -0.63 | +0.93 | 67 % |
| tramos cada 2 min | 13886 | 76 % | 100 % | +0.24 | +0.00 | +0.25 | -0.78 | 50 % | -0.57 | +0.89 | 64 % |
| troceo sin contar comisión | 13894 | 74 % | 100 % | +0.17 | +0.00 | +0.17 | -0.92 | 44 % | -0.67 | +0.89 | 75 % |

## 3. Criterio frente a `prod actual`

| Política | Trimestres ρ 0.8 | Trimestres ρ 1 | Δ medio | IC 95 % | Llenado ≥ 80 % | Pasa |
|---|---|---|---|---|---|---|
| ejecutar ahora | 0/5 | 0/5 | -0.947 | -1.970 a -0.168 | 100 % | no |
| reval entrada θ, parar | 5/5 | 5/5 | +0.035 | +0.003 a +0.067 | 38 % | no |
| reval entrada θ, pausa 30m | 4/5 | 4/5 | +0.114 | +0.092 a +0.135 | 77 % | no |
| reval entrada θ, pausa ventana | 4/5 | 1/5 | +0.132 | +0.114 a +0.147 | 93 % | no |
| reval entrada θ−0.15, parar | 5/5 | 4/5 | +0.057 | +0.028 a +0.087 | 45 % | no |
| reval entrada θ−0.15, pausa 30m | 4/5 | 2/5 | +0.112 | +0.093 a +0.130 | 87 % | no |
| reval entrada θ−0.15, pausa ventana | 4/5 | 1/5 | +0.116 | +0.099 a +0.129 | 96 % | no |
| reval entrada ≥0, parar | 5/5 | 4/5 | +0.070 | +0.043 a +0.099 | 51 % | no |
| reval entrada ≥0, pausa 30m | 4/5 | 1/5 | +0.104 | +0.086 a +0.119 | 91 % | no |
| reval entrada ≥0, pausa ventana | 3/5 | 1/5 | +0.103 | +0.089 a +0.116 | 97 % | no |
| reval salida θ, pausa 30m | 4/5 | 0/5 | +0.130 | +0.120 a +0.139 | 100 % | no |
| reval salida θ, pausa ventana | 4/5 | 0/5 | +0.128 | +0.118 a +0.138 | 100 % | no |
| reval salida θ−0.15, pausa 30m | 4/5 | 0/5 | +0.116 | +0.107 a +0.126 | 100 % | no |
| reval salida θ−0.15, pausa ventana | 4/5 | 0/5 | +0.115 | +0.106 a +0.125 | 100 % | no |
| confirmación 2 | 0/5 | 0/5 | -0.008 | -0.032 a +0.013 | 100 % | no |
| confirmación 3 | 0/5 | 0/5 | -0.019 | -0.049 a +0.009 | 100 % | no |
| confirmación med3 | 0/5 | 0/5 | -0.013 | -0.033 a +0.008 | 100 % | no |
| tramos cada 2 min | 2/5 | 0/5 | +0.062 | +0.056 a +0.068 | 100 % | no |
| tramos cada 5 min | 4/5 | 0/5 | +0.152 | +0.139 a +0.165 | 100 % | no |
| troceo sin contar comisión | 0/5 | 0/5 | -0.012 | -0.016 a -0.009 | 100 % | no |

## 4. Walk-forward (elige por mediana de trimestres anteriores, ρ 0.8)

- Trimestre 224: tramos cada 5 min
- Trimestre 225: reval entrada θ, parar
- Trimestre 226: tramos cada 5 min
- Trimestre 227: tramos cada 5 min

Fuera de muestra: mediana +0.10, p5 -0.44; `prod actual` en los mismos trimestres: +0.00, p5 -0.88.

## 5. Sensibilidad

| Escenario de `prod actual` | Mediana | p5 | Fuga |
|---|---|---|---|
| velas con trades, ρ 0 | +0.32 | -0.26 | 24 % |
| velas con trades, ρ 0.8 | +0.00 | -0.91 | 73 % |
| velas con trades, ρ 1 | +0.00 | -2.03 | 124 % |
| todas las velas (planas incluidas), ρ 0.8 | +0.20 | -1.13 | 55 % |

## Veredicto

Ninguna política pasa el criterio: no se cambia la ejecución.

