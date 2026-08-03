token=$(echo $XRAY_UUID)

# If token is empty, exit
if [ -z "$token" ]; then
    echo "No token provided."
    exit 1
fi

echo "Setting up xray..."
jq ".inbounds[0].settings.clients[0].id = \"$token\"" /etc/xray/config.json > /etc/xray/config.json.tmp

echo "Replacing config file..."
mv /etc/xray/config.json.tmp /etc/xray/config.json

echo "Starting xray..."
/usr/bin/xray -config /etc/xray/config.json