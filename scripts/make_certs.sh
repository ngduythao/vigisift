#!/bin/sh
# Dev certificates: a private CA, and server certificates for Mosquitto, Frigate and the console.
# Writes deploy/certs/ and deploy/mosquitto/conf.d/tls.conf (both gitignored), which turns on
# Mosquitto's TLS listener on 8883. Re-running keeps an existing CA (clients that trust it keep
# working) and renews the server certificates.
#
# A real deployment uses certificates from its own CA or a public one: put them in the same
# file names, or point the settings at them (README, "Running it for real").
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CERTS="$ROOT/deploy/certs"
DAYS=825
mkdir -p "$CERTS" "$ROOT/deploy/mosquitto/conf.d"
cd "$CERTS"

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=VigiSift dev CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout ca.key -out ca.crt 2>/dev/null
fi

# name, then the names and addresses clients use to reach it
server() {
  name=$1
  shift
  san=""
  for host in "$@"; do
    case $host in
      *[!0-9.]*) san="$san,DNS:$host" ;;
      *) san="$san,IP:$host" ;;
    esac
  done
  openssl req -newkey rsa:3072 -nodes -subj "/CN=$name" \
    -keyout "$name.key" -out "$name.csr" 2>/dev/null
  # Strict verifiers (Python 3.13+, recent browsers) also want the key identifiers and usage.
  printf '%s\n' "subjectAltName=${san#,}" "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" "extendedKeyUsage=serverAuth" \
    "subjectKeyIdentifier=hash" "authorityKeyIdentifier=keyid,issuer" > "$name.ext"
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "$DAYS" -extfile "$name.ext" -out "$name.crt" 2>/dev/null
  rm "$name.csr" "$name.ext"
  # Readable by the containers' own users (mosquitto runs as uid 1883). Dev keys only:
  # a real key stays readable by its service alone.
  chmod 644 "$name.key"
}

server mosquitto mosquitto localhost 127.0.0.1
server frigate frigate localhost 127.0.0.1
server console localhost 127.0.0.1

cat > "$ROOT/deploy/mosquitto/conf.d/tls.conf" <<'CONF'
# Written by scripts/make_certs.sh. Same accounts and ACL as port 1883.
listener 8883
cafile /mosquitto/certs/ca.crt
certfile /mosquitto/certs/mosquitto.crt
keyfile /mosquitto/certs/mosquitto.key
tls_version tlsv1.2
CONF

echo "certificates in deploy/certs (CA: deploy/certs/ca.crt); Mosquitto TLS listener on 8883"
