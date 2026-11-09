#!/bin/sh
# 按 IP 签发 TLS 证书（无域名部署用）：第一次运行生成自建 CA，之后每次只重签服务器证书。
#
#   ./gen-certs.sh <服务器公网IP> [更多 IP ...]
#
# 产物（certs/ 目录）：
#   ca.pem       CA 证书 —— 发给每个终端（[monitor] amqp_ca_file），也可导入运维电脑让浏览器不报警
#   ca.key       CA 私钥 —— 只留在本机、妥善保管，重签证书时才用；绝不外发
#   cert.pem     服务器证书（SAN 含上面的 IP），RabbitMQ 与面板共用
#   chain.pem    同 ca.pem（RabbitMQ 的 cacertfile）
#   privkey.pem  服务器私钥
# 服务器 IP 变了：重新运行本脚本（CA 不变，终端不用动），再重启 RabbitMQ 与面板。
set -eu
[ $# -ge 1 ] || { echo "用法: $0 <服务器公网IP> [更多 IP ...]" >&2; exit 1; }
for ip in "$@"; do
  case "$ip" in *[!0-9.:a-fA-F]*|"") echo "不是 IP 地址: $ip" >&2; exit 1;; esac
done
cd "$(dirname "$0")"
mkdir -p certs && cd certs
umask 077

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
    -keyout ca.key -out ca.pem -subj "/CN=PaperLite Monitor CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  echo "已生成 CA：certs/ca.pem（发给终端）、certs/ca.key（妥善保管）"
fi

SAN=""
for ip in "$@"; do SAN="${SAN:+$SAN,}IP:$ip"; done
cat > server.ext <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
EOF
openssl req -newkey rsa:2048 -nodes -sha256 -keyout privkey.pem -out server.csr -subj "/CN=$1" 2>/dev/null
# 825 天：苹果/Chrome 对服务器证书有效期的上限，超过浏览器会拒绝
openssl x509 -req -sha256 -days 825 -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -extfile server.ext -out cert.pem 2>/dev/null
cp ca.pem chain.pem
rm -f server.csr server.ext
chmod 600 ca.key privkey.pem
chmod 644 ca.pem chain.pem cert.pem
echo "已签发服务器证书 certs/cert.pem（${SAN}，825 天有效，到期前重新运行本脚本）"
openssl x509 -in cert.pem -noout -enddate
