import threading
import time
from fetchAirbnb import main as run_fetch
from server import app

def worker():
    while True:
        try:
            print("🔄 Ejecutando fetchAirbnb...")
            run_fetch()   # Ejecuta el script de correos
        except Exception as e:
            print("⚠️ Error:", e)
        time.sleep(300)  # Espera 5 minutos antes de volver a ejecutar

# Lanzamos el worker en un hilo aparte
threading.Thread(target=worker, daemon=True).start()

# Iniciamos servidor Flask (Render necesita esto para mantener el proceso activo)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
