FROM hub.aiursoft.com/teddysun/xray

RUN mkdir -p /etc/xray
RUN mkdir -p /var/log/xray
RUN mkdir -p /etc/xray/cert

COPY ./config.json /etc/xray/config.json
COPY ./entry.sh /entry.sh

RUN apk add jq


EXPOSE 10000
VOLUME /var/log/xray

ENTRYPOINT [ "/bin/sh", "/entry.sh" ]