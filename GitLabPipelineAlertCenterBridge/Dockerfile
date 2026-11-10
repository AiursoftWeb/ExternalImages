FROM hub.aiursoft.com/aiursoft/internalimages/dotnet AS install
WORKDIR /app
COPY nuget.config /tmp/nuget.config
RUN dotnet tool install Aiursoft.PipelineWatchDog --version '10.*' --tool-path /app --configfile /tmp/nuget.config

FROM hub.aiursoft.com/aiursoft/internalimages/dotnetonlyruntime
WORKDIR /app
COPY --from=install /app ./
RUN mkdir -p /data && chown 1654:1654 /data && chmod 700 /data
USER 1654:1654
HEALTHCHECK --interval=60s --timeout=10s --start-period=15m --retries=3 CMD ["/app/pipeline-watchdog", "--healthcheck"]
ENTRYPOINT ["/app/pipeline-watchdog"]
