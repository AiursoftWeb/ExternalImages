FROM hub.aiursoft.com/containrrr/shepherd:latest AS upstream

FROM upstream AS patched
RUN apk add --no-cache patch
COPY shepherd-140.patch /tmp/shepherd-140.patch
RUN patch --batch --forward --fuzz=0 -p1 -d /usr/local/bin -i /tmp/shepherd-140.patch \
    && bash -n /usr/local/bin/shepherd

FROM upstream
COPY --from=patched /usr/local/bin/shepherd /usr/local/bin/shepherd
COPY LICENSE /usr/share/licenses/shepherd/LICENSE
