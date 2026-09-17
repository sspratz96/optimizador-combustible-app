# ⛽ BencinApp

Encuentra la bencina más barata en Santiago considerando:
- Precios reales de cada estación (fuente: CNE)
- Descuentos por tarjeta bancaria según el día
- Distancia real por calles (OSRM)
- Costo de traslado según el rendimiento de tu auto

## Setup

### 1. Crear el repositorio en GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/bencina-app.git
git push -u origin main
```

### 2. Activar GitHub Pages

1. Ve a **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: **main** / **(root)**
4. Save

Tu sitio estará en `https://TU_USUARIO.github.io/bencina-app/`

### 3. Conectar precios reales (CNE API)

1. Regístrate en [api.cne.cl](https://api.cne.cl/) (gratuito)
2. En tu repo de GitHub: **Settings** → **Secrets and variables** → **Actions**
3. Crea dos secrets:
   - `CNE_EMAIL` → tu correo registrado
   - `CNE_PASSWORD` → tu contraseña
4. Ve a **Actions** → **Actualizar precios CNE** → **Run workflow**

El Action hace login automático, obtiene un token fresco, descarga todas las estaciones de la RM y genera `data/stations.json`. Se ejecuta cada 6 horas.

### 4. Actualizar descuentos mensuales

Edita `data/discounts.json` cada mes con los nuevos descuentos. La estructura es:

```json
{
  "card": "Nombre del banco/tarjeta",
  "brand": "Copec|Shell|Aramco",
  "discount": 150,
  "days": [1],
  "app": "App Copec"
}
```

Los días van de 0 (domingo) a 6 (sábado).

Fuentes para actualizar:
- [Chócale - Descuentos bencina](https://chocale.cl/temas/descuentos-bencinas/)
- [Autofact - Calendario descuentos](https://www.autofact.cl/blog/mi-auto/conduccion/descuento-bencina)

## Tecnologías

- **Mapa:** Leaflet + OpenStreetMap (sin API key)
- **Distancias:** OSRM público (distancia real por calles)
- **Precios:** CNE API via GitHub Actions
- **Hosting:** GitHub Pages (estático, gratuito)

## Limitaciones

- OSRM público puede tener rate limits en uso intensivo
- Los descuentos de tarjetas cambian mensualmente y deben actualizarse a mano
- Algunos descuentos tienen topes mensuales que la app no rastrea
- El script de la CNE API puede necesitar ajustes si la API cambia su formato
