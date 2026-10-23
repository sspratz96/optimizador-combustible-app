#!/usr/bin/env bash
set -e
source .env.local

echo "— Probando login…"
CODE=$(curl -s -o resp.json -w '%{http_code}' -X POST "https://api.cne.cl/api/login" \
  --data-urlencode "email=$CNE_EMAIL" \
  --data-urlencode "password=$CNE_PASSWORD")
echo "HTTP $CODE"
head -c 200 resp.json; echo

if [ "$CODE" = "200" ]; then
  TOKEN=$(sed -E 's/.*"token" *: *"([^"]*)".*/\1/' resp.json)
  echo "— Login OK. Probando descarga…"
  CODE2=$(curl -s --compressed -o raw_stations.json -w '%{http_code}' \
    "https://api.cne.cl/api/v4/estaciones" -H "Authorization: Bearer $TOKEN")
  echo "HTTP $CODE2"
  echo "Tamaño: $(wc -c < raw_stations.json) bytes — primer caracter: $(head -c 1 raw_stations.json)"
  echo "Primer codigo_region: $(grep -o '"codigo_region"[^,}]*' raw_stations.json | head -1)"
fi
