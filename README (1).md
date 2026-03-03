# Funding Rate Portfolio Manager

Bot que escanea funding rates de Binance y Bybit, gestiona tu capital (80% seguro / 20% agresivo), y te dice exactamente que comprar y cuando salir.

## Deploy en Railway (5 min)

1. Sube este repo a tu GitHub
2. Ve a railway.app y crea cuenta
3. New Project > Deploy from GitHub > selecciona este repo
4. En Variables agrega:
   - CAPITAL = 1000 (tu capital en USD)
   - SCAN_MINUTES = 5
   - BOT_PASSWORD = tuclave123 (opcional, protege confirmaciones)
5. Railway te da un link publico. Compartelo con tu operador.

## Variables de entorno

| Variable | Default | Descripcion |
|----------|---------|-------------|
| CAPITAL | 1000 | Capital total en USD |
| SCAN_MINUTES | 5 | Minutos entre scans |
| MIN_VOLUME | 5000000 | Volumen minimo 24h |
| SAFE_PCT | 80 | Porcentaje para seguras |
| AGGR_PCT | 20 | Porcentaje para agresivas |
| BOT_PASSWORD | (vacio) | Password para confirmar acciones |

## Local (para probar)

```
pip install flask requests gunicorn
python app.py
```

Abre http://localhost:5000
